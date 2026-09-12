import 'dart:async';
import 'dart:io';
import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:image_picker/image_picker.dart';
import 'package:kaede_mobile/l10n/generated/app_localizations.dart';
import 'package:kaede_mobile/src/api/instance_administration_repository.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/domain/application_installations.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/text_to_speech.dart';
import 'package:kaede_mobile/src/features/settings/developer_portal_screen.dart';
import 'package:kaede_mobile/src/features/settings/instance_administration_screen.dart';
import 'package:kaede_mobile/src/features/settings/reports_screen.dart';
import 'package:kaede_mobile/src/features/shared/remote_media.dart';
import 'package:kaede_mobile/src/features/shared/settings_ui.dart';
import 'package:kaede_mobile/src/features/voice/media_quality.dart';
import 'package:kaede_mobile/src/features/voice/voice_session.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:local_auth/local_auth.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:url_launcher/url_launcher.dart';

/// Account settings with visible actions and persistent control states.
final class SettingsScreen extends ConsumerStatefulWidget {
  const SettingsScreen({super.key});

  @override
  ConsumerState<SettingsScreen> createState() => _SettingsScreenState();
}

final class _SettingsScreenState extends ConsumerState<SettingsScreen> {
  final _displayName = TextEditingController();
  final _customStatus = TextEditingController();
  final _bio = TextEditingController();
  Map<String, Object?> _settings = <String, Object?>{};
  List<Map<String, Object?>> _sessions = const [];
  List<UserApplicationInstallation> _applicationInstallations = const [];
  var _loading = true;
  var _saving = false;
  var _enablingPush = false;
  var _initializingEncryption = false;
  String? _pushSetupMessage;
  var _biometricLock = false;
  var _biometricLockTimeout = 30;
  var _opusDtx = true;
  var _adminAvailable = false;
  String? _loadError;
  String? _versionLabel;

  Future<String?> _secretPrompt(String title, String label) async {
    final input = TextEditingController();
    try {
      return showDialog<String>(
        context: context,
        builder: (context) => AlertDialog(
          title: Text(title),
          content: TextField(
            controller: input,
            obscureText: true,
            autofocus: true,
            decoration: InputDecoration(labelText: label),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context),
              child: Text(L10n.of(context).ui_cancel_35afca3b),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, input.text),
              child: Text(L10n.of(context).ui_continue_ab43d664),
            ),
          ],
        ),
      );
    } finally {
      input.dispose();
    }
  }

  Future<void> _initializeEncryption() async {
    if (_initializingEncryption ||
        ref.read(mobileControllerProvider).e2eeReady) {
      return;
    }
    setState(() => _initializingEncryption = true);
    try {
      await ref.read(mobileControllerProvider.notifier).e2eeClient();
      _showSuccess('This device is ready for end-to-end encryption.');
    } on Object catch (error) {
      _showError(error,
          summary: L10n.current.ui_could_not_initialize_encryption_a5f503e0);
    } finally {
      if (mounted) setState(() => _initializingEncryption = false);
    }
  }

  Future<void> _exportEncryptionRecovery() async {
    final passphrase = await _secretPrompt(
      'Create recovery backup',
      'A new passphrase (12+ characters)',
    );
    if (passphrase == null) return;
    try {
      final bundle = await ref
          .read(mobileControllerProvider.notifier)
          .exportE2eeRecovery(passphrase);
      if (!mounted) return;
      await showDialog<void>(
        context: context,
        barrierDismissible: false,
        builder: (context) => AlertDialog(
          title: Text(
              L10n.of(context).ui_save_your_encrypted_recovery_backup_4c2d4594),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  L10n.of(context)
                      .ui_anyone_with_this_backup_and_its_passphrase_ca_164a07d9,
                ),
                SizedBox(height: 12),
                SelectableText(bundle, maxLines: 8),
              ],
            ),
          ),
          actions: [
            TextButton.icon(
              onPressed: () async {
                await Clipboard.setData(ClipboardData(text: bundle));
                if (context.mounted) {
                  ScaffoldMessenger.of(context).showSnackBar(
                    SnackBar(
                        content: Text(L10n.of(context)
                            .ui_recovery_backup_copied_fcef8632)),
                  );
                }
              },
              icon: Icon(Icons.copy_rounded),
              label: Text(L10n.of(context).ui_copy_658f3664),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context),
              child: Text(L10n.of(context).ui_i_saved_it_f0b83580),
            ),
          ],
        ),
      );
    } on Object catch (error) {
      _showError(error,
          summary:
              L10n.current.ui_could_not_create_the_recovery_backup_59c7f841);
    }
  }

  Future<void> _importEncryptionRecovery() async {
    final bundle = TextEditingController();
    final passphrase = TextEditingController();
    try {
      final accepted = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: Text(L10n.of(context).ui_restore_encrypted_history_b4785055),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  L10n.of(context)
                      .ui_use_a_recovery_backup_when_automatic_account__3b508c42,
                ),
                SizedBox(height: 12),
                TextField(
                  controller: bundle,
                  minLines: 4,
                  maxLines: 8,
                  decoration: InputDecoration(
                      labelText: L10n.of(context).ui_recovery_backup_12b7e994),
                ),
                SizedBox(height: 12),
                TextField(
                  controller: passphrase,
                  obscureText: true,
                  decoration: InputDecoration(
                      labelText: L10n.of(context).ui_passphrase_191a2dc5),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: Text(L10n.of(context).ui_cancel_35afca3b),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: Text(L10n.of(context).ui_replace_and_restore_6453a4d4),
            ),
          ],
        ),
      );
      if (accepted != true) return;
      setState(() => _saving = true);
      await ref.read(mobileControllerProvider.notifier).importE2eeRecovery(
            bundle.text.trim(),
            passphrase.text,
          );
      _showSuccess('Encrypted history was restored on this phone.');
    } on Object catch (error) {
      _showError(error,
          summary:
              L10n.current.ui_could_not_restore_encrypted_history_639bcbd5);
    } finally {
      bundle.dispose();
      passphrase.dispose();
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _manageEncryptionDevices() async {
    try {
      final repository = ref.read(mobileControllerProvider.notifier).repository;
      final controller = ref.read(mobileControllerProvider.notifier);
      final currentDeviceId = await controller.currentE2eeDeviceId();
      final response = await repository.e2eeDevices();
      final devices = (response['devices'] as List? ?? const [])
          .whereType<Map<Object?, Object?>>()
          .map((item) => Map<String, Object?>.from(item))
          .toList(growable: false);
      if (!mounted) return;
      await showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        showDragHandle: true,
        builder: (context) => SafeArea(
          child: ListView(
            shrinkWrap: true,
            padding: EdgeInsets.fromLTRB(16, 0, 16, 24),
            children: [
              Text(L10n.of(context).ui_encryption_identity_d4a088ae,
                  style: Theme.of(context).textTheme.titleLarge),
              SizedBox(height: 6),
              Text(
                L10n.of(context)
                    .ui_your_signed_in_clients_share_one_portable_mls_43e16efa,
              ),
              SizedBox(height: 12),
              for (final device in devices)
                ListTile(
                  leading: Icon(device['revoked_at'] == null
                      ? Icons.verified_user_outlined
                      : Icons.phonelink_erase_rounded),
                  title: Text(
                      L10n.of(context).ui_portable_account_identity_9267709d),
                  subtitle: Text(
                    L10n.of(context)
                        .ui_last_enrolled_from_value0_value1_value2_value_e534a826(
                            (device['device_name'] ?? 'Kaede').toString(),
                            (device['platform'] ?? 'unknown').toString(),
                            (device['id']).toString(),
                            (device['id'] == currentDeviceId
                                    ? ' \u00b7 Loaded here'
                                    : '')
                                .toString()),
                  ),
                  trailing: device['revoked_at'] != null
                      ? Text(L10n.of(context).ui_revoked_e58fe295)
                      : IconButton(
                          tooltip: L10n.of(context)
                              .ui_rotate_encryption_identity_4aeaf1f7,
                          onPressed: () async {
                            final accepted = await showDialog<bool>(
                              context: context,
                              builder: (dialogContext) => AlertDialog(
                                title: Text(L10n.of(context)
                                    .ui_start_a_new_encryption_identity_17f1a378),
                                content: Text(
                                  L10n.of(context)
                                      .ui_this_abandons_encrypted_history_that_is_unava_00029ea3,
                                ),
                                actions: [
                                  TextButton(
                                    onPressed: () =>
                                        Navigator.pop(dialogContext, false),
                                    child: Text(
                                        L10n.of(context).ui_cancel_35afca3b),
                                  ),
                                  FilledButton(
                                    onPressed: () =>
                                        Navigator.pop(dialogContext, true),
                                    child: Text(L10n.of(context)
                                        .ui_start_fresh_2eca7af7),
                                  ),
                                ],
                              ),
                            );
                            if (accepted != true) return;
                            try {
                              await controller.resetE2eeIdentity();
                              await controller.e2eeClient();
                              if (context.mounted) Navigator.pop(context);
                              _showSuccess(
                                'A new encryption identity is active. Rotate affected room keys before resuming.',
                              );
                            } on Object catch (error) {
                              _showError(
                                error,
                                summary: L10n.current
                                    .ui_could_not_create_a_new_encryption_identity_12f87665,
                              );
                            }
                          },
                          icon: Icon(Icons.delete_outline_rounded),
                        ),
                ),
            ],
          ),
        ),
      );
    } on Object catch (error) {
      _showError(error,
          summary: L10n.current.ui_could_not_load_encryption_identity_42c0a2c8);
    }
  }

  @override
  void initState() {
    super.initState();
    _load();
    unawaited(_loadVersion());
  }

  Future<void> _loadVersion() async {
    try {
      final info = await PackageInfo.fromPlatform();
      if (mounted) {
        setState(
          () => _versionLabel =
              'Kaede Chat ${info.version} (${info.buildNumber})',
        );
      }
    } on Object {
      // The version footer is cosmetic; a missing platform channel is fine.
    }
  }

  @override
  void dispose() {
    _displayName.dispose();
    _customStatus.dispose();
    _bio.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final repository = ref.read(mobileControllerProvider.notifier).repository;
      final results = await Future.wait<Object>([
        repository.settings(),
        repository.sessions(),
        repository.userApplicationInstallations(),
        _canOpenAdministration(repository),
      ]);
      final user = ref.read(mobileControllerProvider).user;
      final preferences = await SharedPreferences.getInstance();
      final mediaQuality = await MobileMediaQuality.load();
      if (!mounted) return;
      setState(() {
        _settings = Map<String, Object?>.from(results[0] as Map);
        _sessions = results[1] as List<Map<String, Object?>>;
        _applicationInstallations =
            results[2] as List<UserApplicationInstallation>;
        _adminAvailable = results[3] as bool;
        _displayName.text = user?.displayName ?? '';
        _customStatus.text = user?.customStatus ?? '';
        _bio.text = user?.bio ?? '';
        _biometricLock = preferences.getBool('biometric_lock') ?? false;
        _biometricLockTimeout =
            preferences.getInt('biometric_lock_timeout_seconds') ?? 30;
        _opusDtx = mediaQuality.dtx;
        _loadError = null;
        _loading = false;
      });
    } on Object catch (error) {
      if (mounted) {
        setState(() {
          _loadError = userFacingError(
            error,
            summary: L10n.of(context)
                .ui_account_settings_could_not_be_loaded_some_con_84593b02,
          );
          _loading = false;
        });
      }
    }
  }

  Future<bool> _canOpenAdministration(KaedeRepository repository) async {
    try {
      await repository.administrationIdentity();
      return true;
    } on Object {
      return false;
    }
  }

  Future<void> _manageUserApplication(
      UserApplicationInstallation installation) async {
    final grantsEditable = installation.grantsEditable;
    final unavailableReason = installation.unavailableReason;
    var guilds = installation.contexts.contains('guild');
    var privateChannels = installation.contexts.contains('private_channel');
    var botDms = installation.contexts.contains('bot_dm');
    final action = await showModalBottomSheet<String>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (sheetContext) => StatefulBuilder(
        builder: (context, setSheetState) => SafeArea(
          child: Padding(
            padding: EdgeInsets.fromLTRB(
              16,
              0,
              16,
              MediaQuery.viewInsetsOf(context).bottom + 18,
            ),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(installation.applicationName,
                    style: Theme.of(context).textTheme.titleLarge),
                if (!grantsEditable) ...[
                  SizedBox(height: 4),
                  Text(
                    installation.isSuspended
                        ? L10n.of(context).ui_suspended_unavailable_ac856ef7
                        : L10n.of(context).ui_revoked_unavailable_19ed49ea,
                    style: TextStyle(
                      color: context.kaede.warning,
                      fontSize: 11,
                      fontWeight: FontWeight.w800,
                      letterSpacing: .7,
                    ),
                  ),
                ],
                SizedBox(height: 4),
                Text(
                  installation.applicationDescription ??
                      installation.application.wire,
                  style: TextStyle(color: context.kaede.muted),
                ),
                if (unavailableReason != null) ...[
                  SizedBox(height: 12),
                  Container(
                    padding: EdgeInsets.all(12),
                    decoration: BoxDecoration(
                      color: context.kaede.warningSoft,
                      borderRadius: BorderRadius.circular(10),
                    ),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Icon(
                          Icons.pause_circle_outline_rounded,
                          color: context.kaede.warning,
                          size: 20,
                        ),
                        SizedBox(width: 10),
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                L10n.of(context)
                                    .ui_commands_unavailable_38116645,
                                style: TextStyle(
                                  color: context.kaede.warning,
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                              SizedBox(height: 2),
                              Text(
                                unavailableReason,
                                style: TextStyle(
                                  color: context.kaede.warning,
                                  fontSize: 12.5,
                                  height: 1.35,
                                ),
                              ),
                            ],
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
                SizedBox(height: 12),
                CheckboxListTile(
                  contentPadding: EdgeInsets.zero,
                  value: guilds,
                  title: Text(L10n.of(context).ui_guild_channels_fcb4d8c8),
                  onChanged: grantsEditable
                      ? (value) => setSheetState(() => guilds = value ?? false)
                      : null,
                ),
                CheckboxListTile(
                  contentPadding: EdgeInsets.zero,
                  value: privateChannels,
                  title:
                      Text(L10n.of(context).ui_private_conversations_0de4d400),
                  onChanged: grantsEditable
                      ? (value) => setSheetState(
                            () => privateChannels = value ?? false,
                          )
                      : null,
                ),
                CheckboxListTile(
                  contentPadding: EdgeInsets.zero,
                  value: botDms,
                  title: Text(
                      L10n.of(context).ui_direct_messages_with_bots_f733fcd2),
                  onChanged: grantsEditable
                      ? (value) => setSheetState(() => botDms = value ?? false)
                      : null,
                ),
                SizedBox(height: 8),
                FilledButton(
                  onPressed:
                      grantsEditable && (guilds || privateChannels || botDms)
                          ? () => Navigator.pop(sheetContext, 'save')
                          : null,
                  child: Text(L10n.of(context).ui_save_command_access_f73dfe79),
                ),
                TextButton(
                  style: TextButton.styleFrom(
                      foregroundColor: context.kaede.danger),
                  onPressed: () => Navigator.pop(sheetContext, 'revoke'),
                  child: Text(L10n.of(context).ui_revoke_app_d6963c78),
                ),
              ],
            ),
          ),
        ),
      ),
    );
    if (action == null || !mounted) return;
    if (action == 'save' && !installation.grantsEditable) return;
    if (action == 'revoke') {
      final confirmed = await showDialog<bool>(
        context: context,
        builder: (dialogContext) => AlertDialog(
          title: Text(L10n.of(context).ui_revoke_value0_8d1f4335(
              (installation.applicationName).toString())),
          content: Text(
            L10n.of(context)
                .ui_its_user_installed_commands_will_disappear_th_697aae80,
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext, false),
              child: Text(L10n.of(context).ui_cancel_35afca3b),
            ),
            FilledButton(
              style:
                  FilledButton.styleFrom(backgroundColor: context.kaede.danger),
              onPressed: () => Navigator.pop(dialogContext, true),
              child: Text(L10n.of(context).ui_revoke_2219c453),
            ),
          ],
        ),
      );
      if (confirmed != true || !mounted) return;
    }
    setState(() => _saving = true);
    try {
      final repository = ref.read(mobileControllerProvider.notifier).repository;
      if (action == 'revoke') {
        await repository.revokeUserApplicationInstallation(installation.id);
        if (!mounted) return;
        setState(() => _applicationInstallations = _applicationInstallations
            .where((item) => item.id != installation.id)
            .toList(growable: false));
        _showSuccess('${installation.applicationName} was revoked.');
      } else {
        final updated = await repository.updateUserApplicationInstallation(
          installation.id,
          contexts: <String>[
            if (guilds) 'guild',
            if (privateChannels) 'private_channel',
            if (botDms) 'bot_dm',
          ],
        );
        if (!mounted) return;
        setState(() => _applicationInstallations = _applicationInstallations
            .map((item) => item.id == updated.id ? updated : item)
            .toList(growable: false));
        _showSuccess('${installation.applicationName} access updated.');
      }
    } on Object catch (error) {
      _showError(error,
          summary: L10n.of(context)
              .ui_could_not_update_that_authorized_app_4846bf2d);
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  String _userApplicationSummary(UserApplicationInstallation installation) {
    final contexts = <String>[
      if (installation.contexts.contains('guild')) 'Guilds',
      if (installation.contexts.contains('private_channel'))
        'Private conversations',
      if (installation.contexts.contains('bot_dm')) 'Bot DMs',
    ];
    return <String>[
      if (!installation.grantsEditable)
        installation.isSuspended
            ? 'Suspended · Unavailable'
            : 'Revoked · Unavailable',
      ...contexts,
    ].join(' · ');
  }

  @override
  Widget build(BuildContext context) {
    final mobile = ref.watch(mobileControllerProvider);
    final user = mobile.user;
    if (_loading) {
      return ColoredBox(
        color: Theme.of(context).scaffoldBackgroundColor,
        child: Center(child: CircularProgressIndicator()),
      );
    }
    final presence = '${_settings['presence_preference'] ?? 'online'}';
    final usesRelay = ref.read(mobileControllerProvider.notifier).usesPushRelay;
    final pushRelayHost =
        ref.read(mobileControllerProvider.notifier).pushRelayHost;
    final tts = _ttsPreferences;

    return ColoredBox(
      color: Theme.of(context).scaffoldBackgroundColor,
      child: ListView(
        padding: EdgeInsets.fromLTRB(16, 8, 16, 40),
        children: [
          _AccountHero(
            user: user,
            presence: mobile.presencePreference,
            onEditAvatar: _saving ? null : () => _pickAsset('avatar'),
            onEditBanner: _saving ? null : () => _pickAsset('banner'),
            onRemoveAvatar: _saving || user?.avatarHash == null
                ? null
                : () => _removeAsset('avatar'),
            onRemoveBanner: _saving || user?.bannerHash == null
                ? null
                : () => _removeAsset('banner'),
          ),
          if (_loadError case final warning?) ...[
            SizedBox(height: 16),
            Container(
              padding: EdgeInsets.fromLTRB(12, 10, 6, 10),
              decoration: BoxDecoration(
                color: context.kaede.warningSoft,
                borderRadius: BorderRadius.circular(10),
              ),
              child: Row(
                children: [
                  Icon(Icons.warning_amber_rounded,
                      size: 17, color: context.kaede.warning),
                  SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      warning,
                      style: TextStyle(
                        color: context.kaede.warning,
                        fontSize: 12.5,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                  TextButton(
                    onPressed: () {
                      setState(() => _loading = true);
                      _load();
                    },
                    style: TextButton.styleFrom(minimumSize: Size(0, 34)),
                    child: Text(L10n.of(context).ui_retry_8036af59),
                  ),
                ],
              ),
            ),
          ],
          SettingsSectionHeader(L10n.of(context).ui_profile_cd17328e,
              subheading: L10n.of(context)
                  .ui_how_people_see_you_across_the_federation_3df0bdc7),
          Padding(
            padding: EdgeInsets.symmetric(horizontal: 4),
            child: Column(
              children: [
                SettingsField(
                  label: L10n.of(context).ui_display_name_698fa38c,
                  controller: _displayName,
                  enabled: !_saving,
                ),
                SizedBox(height: 16),
                SettingsField(
                  label: L10n.of(context).ui_custom_status_91dc0394,
                  controller: _customStatus,
                  maxLength: 128,
                  enabled: !_saving,
                ),
                SizedBox(height: 16),
                SettingsField(
                  label: L10n.of(context).ui_about_me_974d6f90,
                  controller: _bio,
                  maxLines: 4,
                  maxLength: 500,
                  enabled: !_saving,
                ),
                SizedBox(height: 18),
                FilledButton.icon(
                  onPressed: _saving ? null : _saveProfile,
                  icon: _saving
                      ? SizedBox.square(
                          dimension: 16,
                          child: CircularProgressIndicator(strokeWidth: 2))
                      : Icon(Icons.check_rounded),
                  label: Text(L10n.of(context).ui_save_profile_9bf48ffd),
                ),
              ],
            ),
          ),
          SettingsSectionHeader(L10n.of(context).ui_account_59f8a2fc,
              subheading: L10n.of(context)
                  .ui_the_identity_hosted_by_your_home_instance_a17941b7),
          SettingsRow.chevron(
            title: user?.email ?? 'No email address',
            subtitle: user?.email == null
                ? L10n.of(context)
                    .ui_this_instance_does_not_require_email_d20dc6bb
                : user!.emailVerified
                    ? L10n.of(context).ui_verified_email_51a616ad
                    : L10n.of(context)
                        .ui_email_verification_is_pending_e54cd06d,
            leading: _LeadingIcon(Icons.alternate_email_rounded),
            onTap: _saving ? null : _changeEmail,
          ),
          SettingsRow.chevron(
            divider: true,
            title: L10n.of(context).ui_confirm_email_change_a2e3e285,
            subtitle: L10n.of(context)
                .ui_enter_the_token_from_your_confirmation_email_748811c1,
            leading: _LeadingIcon(Icons.mark_email_read_outlined),
            onTap: _saving ? null : _confirmEmail,
          ),
          SizedBox(height: 16),
          SettingsRow(
            title: L10n.of(context).ui_authenticator_app_744f34d3,
            subtitle: user?.mfaEnabled == true
                ? L10n.of(context)
                    .ui_two_factor_authentication_is_enabled_4de40010
                : L10n.of(context)
                    .ui_require_a_code_in_addition_to_your_password_81056d3c,
            leading: _LeadingIcon(Icons.password_rounded),
            divider: true,
            onTap: user?.mfaEnabled == true ? _disableMfa : _enableMfa,
          ),
          SettingsSectionHeader(L10n.of(context).ui_security_c2fe21db,
              subheading: L10n.of(context)
                  .ui_encryption_keys_unlock_your_account_vault_on__2f7938f9),
          SettingsRow(
            title: mobile.e2eeReady
                ? L10n.of(context).ui_encryption_enabled_71075035
                : _initializingEncryption
                    ? L10n.of(context).ui_setting_up_encryption_dbb11da3
                    : L10n.of(context).ui_set_up_this_device_56bd05ba,
            subtitle: mobile.e2eeReady
                ? L10n.of(context)
                    .ui_this_device_is_ready_for_end_to_end_encryptio_351bb719
                : L10n.of(context)
                    .ui_enable_end_to_end_encryption_on_this_phone_a50338f3,
            leading: _LeadingIcon(Icons.key_rounded),
            enabled: !_saving && !_initializingEncryption && !mobile.e2eeReady,
            trailing: mobile.e2eeReady
                ? const Icon(Icons.check_circle_outline_rounded)
                : _initializingEncryption
                    ? const SizedBox(
                        width: 20,
                        height: 20,
                        child: CircularProgressIndicator(strokeWidth: 2))
                    : const Icon(Icons.add_moderator_outlined),
            onTap: _initializeEncryption,
          ),
          SettingsRow.chevron(
            title: L10n.of(context).ui_encryption_identity_d4a088ae,
            subtitle: L10n.of(context)
                .ui_devices_sharing_this_account_s_mls_identity_c285170f,
            leading: _LeadingIcon(Icons.devices_other_rounded),
            divider: true,
            onTap: _saving ? null : _manageEncryptionDevices,
          ),
          SettingsRow.chevron(
            title: L10n.of(context).ui_create_recovery_backup_42d961da,
            leading: _LeadingIcon(Icons.download_for_offline_outlined),
            divider: true,
            onTap: _saving ? null : _exportEncryptionRecovery,
          ),
          SettingsRow.chevron(
            title: L10n.of(context).ui_restore_recovery_backup_8f057af6,
            leading: _LeadingIcon(Icons.restore_rounded),
            onTap: _saving ? null : _importEncryptionRecovery,
          ),
          SettingsSectionHeader(L10n.of(context).ui_activity_status_6b197d4e,
              subheading: L10n.of(context)
                  .ui_your_availability_follows_you_between_mobile__1aa8ef75),
          SettingsRow(
            title: L10n.of(context).ui_online_bc3b88aa,
            leading: _PresenceIcon(PresenceStatus.online),
            divider: true,
            onTap: () => _saveSetting('presence_preference', 'online'),
            trailing: _presenceCheck(presence == 'online'),
          ),
          SettingsRow(
            title: L10n.of(context).ui_idle_45aa17b3,
            leading: _PresenceIcon(PresenceStatus.idle),
            divider: true,
            onTap: () => _saveSetting('presence_preference', 'idle'),
            trailing: _presenceCheck(presence == 'idle'),
          ),
          SettingsRow(
            title: L10n.of(context).ui_do_not_disturb_816d809a,
            leading: _PresenceIcon(PresenceStatus.dnd),
            divider: true,
            onTap: () => _saveSetting('presence_preference', 'dnd'),
            trailing: _presenceCheck(presence == 'dnd'),
          ),
          SettingsRow(
            title: L10n.of(context).ui_invisible_bab31830,
            leading: _PresenceIcon(PresenceStatus.invisible),
            onTap: () => _saveSetting('presence_preference', 'invisible'),
            trailing: _presenceCheck(presence == 'invisible'),
          ),
          SettingsSectionHeader(
            L10n.of(context).ui_appearance_31018b9f,
            subheading: L10n.of(context)
                .ui_theme_and_regional_formatting_follow_your_acc_81232c35,
          ),
          SettingsChoiceRow(
            title: L10n.of(context).ui_theme_4eaf7c92,
            value: '${_settings['theme'] ?? 'system'}',
            display: switch ('${_settings['theme'] ?? 'system'}') {
              'light' => 'Light',
              'dark' => 'Dark',
              _ => 'Sync with device',
            },
            leading: const _LeadingIcon(Icons.palette_outlined),
            divider: true,
            onSelected: (value) async {
              final chosen = await showSettingsChoiceSheet(
                context,
                title: L10n.of(context).ui_theme_4eaf7c92,
                description: L10n.of(context)
                    .ui_sync_with_device_follows_the_operating_system_d3766500,
                choices: [
                  SettingsChoice(
                      'system', L10n.of(context).ui_sync_with_device_f98594c0),
                  SettingsChoice('light', L10n.of(context).ui_light_8ad2f8cf),
                  SettingsChoice('dark', L10n.of(context).ui_dark_45570065),
                ],
                selected: value,
              );
              if (chosen != null && chosen != value) {
                await _saveSetting('theme', chosen);
              }
            },
          ),
          SettingsChoiceRow(
            title: L10n.of(context).language_settings,
            subtitle: L10n.of(context).language_description,
            value: appLanguage.value,
            display: appLanguage.value == 'system'
                ? L10n.of(context).language_system
                : lookupAppLocalizations(preferredAppLocale(appLanguage.value)!)
                    .language_name,
            leading: const _LeadingIcon(Icons.translate_rounded),
            onSelected: (value) async {
              final chosen = await showSettingsChoiceSheet(
                context,
                title: L10n.of(context).language_settings,
                description: L10n.of(context).language_description,
                choices: [
                  SettingsChoice('system', L10n.of(context).language_system),
                  for (final locale in AppLocalizations.supportedLocales)
                    SettingsChoice(locale.toLanguageTag(),
                        lookupAppLocalizations(locale).language_name),
                ],
                selected:
                    value == 'system' ? value : matchLanguage(value) ?? 'en',
              );
              if (chosen != null) {
                await _saveSetting('locale', chosen);
              }
            },
          ),
          TextButton(
            onPressed: () => launchUrl(Uri.parse('https://weblate.kaede.chat/'),
                mode: LaunchMode.externalApplication),
            child: Text(L10n.of(context).language_contribute),
          ),
          SettingsSectionHeader(L10n.of(context).ui_notifications_381d190f,
              subheading: usesRelay
                  ? L10n.of(context)
                      .ui_closed_app_delivery_runs_through_kaede_push_r_573433c8(
                          (pushRelayHost).toString())
                  : L10n.of(context)
                      .ui_this_community_build_uses_its_own_firebase_pr_192d5ff4),
          FilledButton.icon(
            icon: _enablingPush
                ? const SizedBox.square(
                    dimension: 18,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(Icons.notifications_active_outlined),
            label: Text(_enablingPush
                ? L10n.of(context).ui_enabling_notifications_59c4bd6b
                : L10n.of(context).ui_enable_background_notifications_dfa12bd2),
            onPressed: _enablingPush
                ? null
                : () async {
                    setState(() {
                      _enablingPush = true;
                      _pushSetupMessage = null;
                    });
                    try {
                      final enabled = await ref
                          .read(mobileControllerProvider.notifier)
                          .enablePushNotifications();
                      if (!context.mounted) return;
                      setState(() {
                        _pushSetupMessage = enabled
                            ? L10n.of(context)
                                .ui_background_notifications_are_enabled_697fbaac
                            : L10n.of(context)
                                .ui_notifications_could_not_be_enabled_check_syst_7a08d801;
                      });
                    } on Object catch (error) {
                      if (!mounted) return;
                      setState(() {
                        _pushSetupMessage = userFacingError(error,
                            summary: L10n.of(context)
                                .ui_could_not_enable_background_notifications_76cccb52);
                      });
                    } finally {
                      if (mounted) setState(() => _enablingPush = false);
                    }
                  },
          ),
          if (_pushSetupMessage case final message?)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 8),
              child: Semantics(liveRegion: true, child: Text(message)),
            ),
          SettingsRow.chevron(
            title:
                L10n.of(context).ui_disable_background_notifications_c89499e5,
            leading: _LeadingIcon(Icons.notifications_off_outlined),
            divider: true,
            onTap: _enablingPush
                ? null
                : () async {
                    await ref
                        .read(mobileControllerProvider.notifier)
                        .disablePushNotifications();
                    if (!context.mounted) return;
                    setState(() {
                      _pushSetupMessage = L10n.of(context)
                          .ui_background_notifications_are_disabled_for_thi_40f8fd45;
                    });
                    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
                      content: Text(
                        L10n.of(context)
                            .ui_background_notifications_are_disabled_for_thi_40f8fd45,
                      ),
                    ));
                  },
          ),
          if (mobile.pushWarning case final warning?) ...[
            SizedBox(height: 10),
            DecoratedBox(
              decoration: BoxDecoration(
                color: context.kaede.warning.withValues(alpha: 0.12),
                borderRadius: BorderRadius.circular(10),
                border: Border.all(
                    color: context.kaede.warning.withValues(alpha: .4)),
              ),
              child: Padding(
                padding: EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                child: Row(
                  children: [
                    Icon(Icons.warning_amber_rounded,
                        size: 18, color: context.kaede.warning),
                    SizedBox(width: 10),
                    Expanded(
                      child: Text(
                        L10n.of(context)
                            .ui_notification_delivery_needs_attention_51e3d429,
                        style: TextStyle(
                          color: context.kaede.warning,
                          fontSize: 13,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
            Padding(
              padding: EdgeInsets.fromLTRB(4, 6, 4, 4),
              child: Text(
                warning,
                style: TextStyle(
                    color: context.kaede.textSoft, fontSize: 12.5, height: 1.4),
              ),
            ),
          ],
          if (!ref.read(mobileControllerProvider.notifier).remotePushAvailable)
            SettingsInfo(
              'This build can show alerts while Kaede is running, but it has no compatible closed-app push provider.',
            ),
          SettingsSwitchRow(
            title: L10n.of(context).ui_direct_messages_0db1f240,
            value: _notification('direct_messages', true),
            onChanged: (value) => _saveNotification('direct_messages', value),
            divider: true,
          ),
          SettingsSwitchRow(
            title: L10n.of(context).ui_mentions_and_replies_b5a93c57,
            value: _notification('mentions', true),
            onChanged: (value) => _saveNotification('mentions', value),
            divider: true,
          ),
          SettingsSwitchRow(
            title: L10n.of(context).ui_friend_requests_48c86d61,
            value: _notification('relationships', true),
            onChanged: (value) => _saveNotification('relationships', value),
            divider: true,
          ),
          SettingsSwitchRow(
            title: L10n.of(context).ui_show_message_previews_1c24a210,
            subtitle: L10n.of(context)
                .ui_shows_the_sender_text_and_profile_picture_aft_9bf1a906,
            value: _notification('show_notification_previews', true),
            onChanged: (value) =>
                _saveNotification('show_notification_previews', value),
          ),
          SettingsChoiceRow(
            title: L10n.of(context).ui_text_to_speech_29f0c859,
            subtitle: L10n.of(context)
                .ui_choose_which_incoming_tts_messages_this_phone_146aaf9a,
            value: tts.playback.name,
            display: switch (tts.playback) {
              TtsPlaybackMode.all => 'All channels',
              TtsPlaybackMode.current => 'Current channel',
              TtsPlaybackMode.never => 'Never',
            },
            onSelected: (value) async {
              final chosen = await showSettingsChoiceSheet(
                context,
                title: L10n.of(context).ui_text_to_speech_playback_3a07a07e,
                description: L10n.of(context)
                    .ui_this_only_affects_messages_marked_as_tts_ordi_ee2a809c,
                choices: <SettingsChoice>[
                  SettingsChoice(
                      'all', L10n.of(context).ui_for_all_channels_205ed599),
                  SettingsChoice(
                      'current',
                      L10n.of(context)
                          .ui_for_current_selected_channel_d1ebe50d),
                  SettingsChoice('never', L10n.of(context).ui_never_b2ff2b29),
                ],
                selected: value,
              );
              if (chosen == null || chosen == value) return;
              await _saveTtsPreferences(tts.copyWith(
                playback: TtsPlaybackMode.values.byName(chosen),
              ));
            },
          ),
          SettingsInfo(
            'Do Not Disturb suppresses banners and sounds on every signed-in client.',
          ),
          SettingsSectionHeader(
            L10n.of(context).ui_accessibility_84b3d09f,
            subheading: L10n.of(context)
                .ui_control_text_to_speech_playback_and_reading_s_4a9f8831,
          ),
          SettingsSwitchRow(
            title: L10n.of(context)
                .ui_allow_playback_and_usage_of_tts_command_c481110d,
            subtitle: L10n.of(context)
                .ui_when_off_kaede_will_not_send_or_speak_text_to_a599b70f,
            value: tts.enabled,
            onChanged: (value) =>
                _saveTtsPreferences(tts.copyWith(enabled: value)),
          ),
          Padding(
            padding: EdgeInsets.fromLTRB(4, 8, 4, 2),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  L10n.of(context).ui_text_to_speech_rate_value0_e3574f53(
                      (tts.rate.toStringAsFixed(1)).toString()),
                  style: TextStyle(
                    color: context.kaede.text,
                    fontSize: 14,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                Slider(
                  min: .5,
                  max: 2,
                  divisions: 15,
                  value: tts.rate,
                  onChanged: tts.enabled && !_saving
                      ? (value) => setState(() {
                            final notification =
                                _settings['notification_settings'];
                            _settings['notification_settings'] = tts
                                .copyWith(rate: value)
                                .mergeInto(notification is Map<Object?, Object?>
                                    ? notification
                                    : null);
                          })
                      : null,
                  onChangeEnd: tts.enabled && !_saving
                      ? (value) => _saveTtsPreferences(
                            _ttsPreferences.copyWith(rate: value),
                          )
                      : null,
                ),
              ],
            ),
          ),
          SettingsSectionHeader(L10n.of(context).ui_privacy_ebfa87e7),
          SettingsRow.chevron(
            title: L10n.of(context).ui_my_reports_78d941fc,
            subtitle: L10n.of(context)
                .ui_review_reports_submitted_to_trust_safety_694bd7dc,
            leading: _LeadingIcon(Icons.flag_outlined),
            divider: true,
            onTap: () => Navigator.of(context).push(
              MaterialPageRoute<void>(builder: (_) => MyReportsScreen()),
            ),
          ),
          SettingsChoiceRow(
            title: L10n.of(context).ui_who_can_message_you_823607fd,
            value: '${_settings['dm_privacy'] ?? 'friends'}',
            display: _dmPrivacyLabel('${_settings['dm_privacy'] ?? 'friends'}'),
            leading: _LeadingIcon(Icons.lock_outline_rounded),
            divider: true,
            onSelected: (value) async {
              final chosen = await showSettingsChoiceSheet(
                context,
                title: L10n.of(context).ui_who_can_message_you_823607fd,
                choices: [
                  SettingsChoice(
                      'everyone', L10n.of(context).ui_everyone_25219998,
                      hint: 'Any Kaede account can start a direct message.'),
                  SettingsChoice(
                      'friends', L10n.of(context).ui_friends_only_ca1a5d18,
                      hint: 'Only people you have added as a friend.'),
                  SettingsChoice('shared_guild',
                      L10n.of(context).ui_friends_and_shared_guilds_982b0f0c,
                      hint:
                          'Friends and members of guilds you share with them.'),
                ],
                selected: value,
              );
              if (chosen != null && chosen != value) {
                _saveSetting('dm_privacy', chosen);
              }
            },
          ),
          SettingsRow(
            title: L10n.of(context)
                .ui_age_restricted_commands_in_direct_messages_8d888b5c,
            subtitle: switch (user?.ageAssuranceState) {
              'adult' => L10n.of(context)
                  .ui_allow_age_restricted_application_commands_in__c887d5aa,
              'minor' => L10n.of(context)
                  .ui_unavailable_because_this_account_is_age_assur_427fc165,
              _ => L10n.of(context)
                  .ui_unavailable_until_your_instance_completes_age_406a4411,
            },
            trailing: DiscordSwitch(
              value: _settings['age_restricted_dm_commands_enabled'] == true,
              onChanged: user?.ageAssuranceState == 'adult' && !_saving
                  ? (value) =>
                      _saveSetting('age_restricted_dm_commands_enabled', value)
                  : null,
            ),
            onTap: user?.ageAssuranceState == 'adult' && !_saving
                ? () => _saveSetting(
                      'age_restricted_dm_commands_enabled',
                      _settings['age_restricted_dm_commands_enabled'] != true,
                    )
                : null,
          ),
          SettingsSwitchRow(
            title: L10n.of(context).ui_lock_kaede_when_you_leave_4a987862,
            subtitle: L10n.of(context)
                .ui_unlock_with_biometrics_your_device_passcode_o_7d5ba4f0,
            value: _biometricLock,
            onChanged: _setBiometricLock,
          ),
          if (_biometricLock)
            SettingsChoiceRow(
              title: L10n.of(context).ui_lock_after_leaving_c08241ac,
              value: '$_biometricLockTimeout',
              display: _lockTimeoutLabel(_biometricLockTimeout),
              onSelected: (value) {
                final chosen = int.tryParse(value);
                if (chosen != null) _setBiometricLockTimeout(chosen);
              },
            ),
          SettingsSectionHeader(
            L10n.of(context).ui_authorized_apps_d0c4597c,
            subheading: L10n.of(context)
                .ui_apps_installed_for_your_account_and_the_conte_f8363698,
          ),
          SettingsInfo(
            'Install an app from its reviewed Add App invitation. Kaede shows the app’s supported locations and requested access before authorization.',
          ),
          for (var index = 0; index < _applicationInstallations.length; index++)
            SettingsRow.chevron(
              title: _applicationInstallations[index].applicationName,
              subtitle:
                  _userApplicationSummary(_applicationInstallations[index]),
              leading: _LeadingIcon(
                _applicationInstallations[index].grantsEditable
                    ? Icons.smart_toy_outlined
                    : Icons.pause_circle_outline_rounded,
              ),
              divider: index != _applicationInstallations.length - 1,
              onTap: _saving
                  ? null
                  : () =>
                      _manageUserApplication(_applicationInstallations[index]),
            ),
          SettingsSectionHeader(
            L10n.of(context).ui_advanced_60f3de7b,
            subheading: L10n.of(context)
                .ui_optional_media_and_troubleshooting_controls_f826ddce,
          ),
          SettingsSwitchRow(
            title: L10n.of(context).ui_opus_discontinuous_transmission_d4983843,
            subtitle: L10n.of(context)
                .ui_reduce_outgoing_bandwidth_while_you_are_not_s_05b24382,
            value: _opusDtx,
            onChanged: _setOpusDtx,
          ),
          SettingsSectionHeader(
            L10n.of(context).ui_developer_34a3d9ed,
            subheading: L10n.of(context)
                .ui_build_applications_and_reveal_qualified_techn_7d88b2dd,
          ),
          SettingsSwitchRow(
            title: L10n.of(context).ui_developer_mode_80d8dbd0,
            subtitle: L10n.of(context)
                .ui_adds_copy_id_actions_for_users_servers_channe_bc7fcbd7,
            value: mobile.developerMode,
            onChanged: _saveDeveloperMode,
            divider: true,
          ),
          if (mobile.developerMode && user != null)
            SettingsRow(
              title: L10n.of(context).ui_copy_my_user_id_fc4a926a,
              subtitle: user.ref.wire,
              leading: const _LeadingIcon(Icons.badge_outlined),
              divider: true,
              onTap: () async {
                await Clipboard.setData(ClipboardData(text: user.ref.wire));
                _showSuccess('User ID copied.');
              },
            ),
          SettingsRow.chevron(
            title: L10n.of(context).ui_developer_portal_f5349d3d,
            subtitle: L10n.of(context)
                .ui_applications_teams_commands_credentials_worke_6e318370,
            leading: _LeadingIcon(Icons.developer_board_outlined),
            onTap: () => Navigator.of(context).push(
              MaterialPageRoute<void>(
                builder: (_) => DeveloperPortalScreen(),
              ),
            ),
          ),
          if (_adminAvailable) ...[
            SettingsSectionHeader(
              L10n.of(context).ui_administration_8fd91fef,
              subheading: L10n.of(context)
                  .ui_capability_gated_instance_operations_and_trus_473df8f1,
            ),
            SettingsRow.chevron(
              title: L10n.of(context).ui_instance_administration_9c3dd778,
              subtitle: L10n.of(context)
                  .ui_users_applications_reports_federation_blocks__2241f659,
              leading: const _LeadingIcon(Icons.admin_panel_settings_outlined),
              onTap: () => Navigator.of(context).push(
                MaterialPageRoute<void>(
                  builder: (_) => InstanceAdministrationScreen(),
                ),
              ),
            ),
          ],
          SettingsSectionHeader(L10n.of(context).ui_devices_27acf320,
              subheading: L10n.of(context)
                  .ui_signed_in_devices_on_this_account_1300d8d2),
          SettingsRow(
            title: L10n.of(context).ui_this_device_8fc5c8b9,
            subtitle: L10n.of(context).ui_current_session_d49af382,
            leading: _LeadingIcon(Icons.check_circle_rounded,
                color: context.kaede.mint),
          ),
          for (final session in _sessions)
            SettingsRow(
              title: L10n.of(context).ui_value0_26e9163c(
                  (session['device_name'] ?? 'Kaede client').toString()),
              subtitle: _sessionSubtitle(session),
              leading:
                  _LeadingIcon(_deviceIcon('${session['device_name'] ?? ''}')),
              divider: true,
              trailing: TextButton(
                onPressed: () => _revoke('${session['id']}'),
                style: TextButton.styleFrom(
                  foregroundColor: context.kaede.danger,
                  minimumSize: Size(0, 36),
                  padding: EdgeInsets.symmetric(horizontal: 10),
                ),
                child: Text(L10n.of(context).ui_sign_out_8b4f3c70),
              ),
            ),
          SizedBox(height: 26),
          Center(
            child: ConstrainedBox(
              constraints: BoxConstraints(maxWidth: 320),
              child:
                  SettingsDangerButton('Log out', onPressed: _confirmSignOut),
            ),
          ),
          SizedBox(height: 8),
          Center(
            child: TextButton(
              onPressed: () => showLicensePage(
                context: context,
                applicationName: 'Kaede Chat',
                applicationVersion: _versionLabel,
                applicationIcon: Padding(
                  padding: EdgeInsets.symmetric(vertical: 8),
                  child: Icon(Icons.forum_rounded,
                      color: context.kaede.coral, size: 34),
                ),
              ),
              style: TextButton.styleFrom(
                foregroundColor: context.kaede.muted,
                textStyle: TextStyle(fontSize: 12.5),
              ),
              child: Text(L10n.of(context).ui_open_source_licences_e93031eb),
            ),
          ),
          Center(
            child: Text(
              _versionLabel ?? 'Kaede Chat',
              style: TextStyle(color: context.kaede.muted, fontSize: 11.5),
            ),
          ),
        ],
      ),
    );
  }

  Widget _presenceCheck(bool selected) => Icon(
        selected
            ? Icons.check_circle_rounded
            : Icons.radio_button_unchecked_rounded,
        color: selected
            ? context.kaede.coralText
            : context.kaede.muted.withValues(alpha: .6),
      );

  static String _dmPrivacyLabel(String value) => switch (value) {
        'everyone' => 'Everyone',
        'shared_guild' => 'Friends and shared guilds',
        _ => 'Friends only',
      };

  static String _lockTimeoutLabel(int seconds) => switch (seconds) {
        0 => 'Immediately',
        15 => '15 seconds',
        30 => '30 seconds',
        60 => '1 minute',
        _ => '$seconds seconds',
      };

  Future<void> _confirmSignOut() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(L10n.of(context).ui_log_out_of_kaede_3fa5e517),
        content: Text(
          L10n.of(context)
              .ui_saved_conversations_on_this_device_stay_encry_1d8e1080,
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: Text(L10n.of(context).ui_stay_signed_in_767385f7),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: context.kaede.danger,
            ),
            onPressed: () => Navigator.pop(dialogContext, true),
            child: Text(L10n.of(context).ui_log_out_88c6d8b5),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    await ref.read(mobileControllerProvider.notifier).logout();
  }

  Future<void> _saveProfile() async {
    setState(() => _saving = true);
    try {
      await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .updateProfile(<String, Object?>{
        'display_name':
            _displayName.text.trim().isEmpty ? null : _displayName.text.trim(),
        'custom_status': _customStatus.text.trim().isEmpty
            ? null
            : _customStatus.text.trim(),
        'bio': _bio.text.trim().isEmpty ? null : _bio.text.trim(),
      });
      await ref.read(mobileControllerProvider.notifier).refreshNavigation();
      _showSuccess('Profile saved');
    } on Object catch (error) {
      _showError(error,
          summary: L10n.current.ui_could_not_save_the_profile_01ac475a);
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _pickAsset(String kind) async {
    final image = await ImagePicker().pickImage(
        source: ImageSource.gallery, maxWidth: 4096, maxHeight: 4096);
    if (image == null) return;
    final contentType =
        imageUploadContentType(image.name, reportedType: image.mimeType);
    if (contentType == null) {
      _showError(L10n.current.ui_choose_a_png_jpeg_gif_or_webp_image_07065ce2);
      return;
    }
    setState(() => _saving = true);
    try {
      await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .uploadUserAsset(
            kind: kind,
            filename: image.name,
            contentType: contentType,
            file: File(image.path),
          );
      final controller = ref.read(mobileControllerProvider.notifier);
      controller.applyUserProfile(await controller.repository.me());
      _showSuccess('${kind == 'avatar' ? 'Avatar' : 'Banner'} updated');
    } on Object catch (error) {
      _showError(
        error,
        summary: L10n.current.ui_could_not_update_the_value0_a2c8df24(
            (kind == 'avatar' ? 'avatar' : 'banner').toString()),
      );
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _removeAsset(String kind) async {
    final label = kind == 'avatar' ? 'avatar' : 'banner';
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(L10n.of(context)
            .ui_remove_your_value0_0cb49922((label).toString())),
        content: Text(L10n.of(context)
            .ui_you_can_upload_a_new_value0_at_any_time_818cafff(
                (label).toString())),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: Text(L10n.of(context).ui_cancel_35afca3b),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: context.kaede.danger,
            ),
            onPressed: () => Navigator.pop(dialogContext, true),
            child: Text(L10n.of(context).ui_remove_21a5901d),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    setState(() => _saving = true);
    try {
      final controller = ref.read(mobileControllerProvider.notifier);
      final updated = await controller.repository.removeUserAsset(kind);
      controller.applyUserProfile(updated);
      _showSuccess('${kind == 'avatar' ? 'Avatar' : 'Banner'} removed');
    } on Object catch (error) {
      _showError(error,
          summary: L10n.current
              .ui_could_not_remove_your_value0_cca01e29((label).toString()));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _changeEmail() async {
    final values = await _credentialsDialog(
      title: L10n.of(context).ui_change_email_d4ae86d5,
      fields: const <_DialogField>[
        _DialogField('email', 'New email',
            keyboardType: TextInputType.emailAddress),
        _DialogField('password', 'Current password', obscure: true),
      ],
      action: 'Send confirmation',
    );
    if (values == null) return;
    await _runSecurityAction('Could not start the email change', () async {
      await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .requestEmailChange(values['email']!, values['password']!);
      _showSuccess('Confirmation sent to the new email address');
    });
  }

  Future<void> _confirmEmail() async {
    final values = await _credentialsDialog(
      title: L10n.of(context).ui_confirm_email_change_a2e3e285,
      fields: const <_DialogField>[
        _DialogField('token', 'Confirmation token'),
      ],
      action: 'Confirm email',
    );
    if (values == null) return;
    await _runSecurityAction('Could not confirm the email change', () async {
      await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .confirmEmailChange(values['token']!);
      await ref.read(mobileControllerProvider.notifier).refreshNavigation();
      _showSuccess('Email address updated');
    });
  }

  Future<void> _enableMfa() async {
    final credentials = await _credentialsDialog(
      title: L10n.of(context).ui_set_up_authenticator_663ba1cb,
      fields: const <_DialogField>[
        _DialogField('password', 'Current password', obscure: true),
      ],
      action: 'Continue',
    );
    if (credentials == null) return;
    await _runSecurityAction('Could not enable two-factor authentication',
        () async {
      final repository = ref.read(mobileControllerProvider.notifier).repository;
      final setup = await repository.setupMfa(credentials['password']!);
      if (!mounted) return;
      final code = await _showMfaSetup(
        secret: '${setup['secret'] ?? ''}',
        uri: '${setup['uri'] ?? ''}',
      );
      if (code == null) return;
      final enabled = await repository.enableMfa(code);
      if (!mounted) return;
      final codes = (enabled['recovery_codes'] as List<Object?>? ?? const [])
          .map((item) => '$item')
          .toList(growable: false);
      await _showRecoveryCodes(codes);
      await ref.read(mobileControllerProvider.notifier).refreshNavigation();
      _showSuccess('Two-factor authentication enabled');
    });
  }

  Future<void> _disableMfa() async {
    final values = await _credentialsDialog(
      title: L10n.of(context).ui_disable_two_factor_authentication_e4b58a45,
      fields: const <_DialogField>[
        _DialogField('password', 'Current password', obscure: true),
        _DialogField('code', 'Authenticator or recovery code'),
      ],
      action: 'Disable',
      destructive: true,
    );
    if (values == null) return;
    await _runSecurityAction('Could not disable two-factor authentication',
        () async {
      await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .disableMfa(values['code']!, values['password']!);
      await ref.read(mobileControllerProvider.notifier).refreshNavigation();
      _showSuccess('Two-factor authentication disabled');
    });
  }

  Future<void> _runSecurityAction(
    String errorSummary,
    Future<void> Function() action,
  ) async {
    setState(() => _saving = true);
    try {
      await action();
    } on Object catch (error) {
      _showError(error, summary: errorSummary);
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<Map<String, String>?> _credentialsDialog({
    required String title,
    required List<_DialogField> fields,
    required String action,
    bool destructive = false,
  }) async {
    final controllers = <String, TextEditingController>{
      for (final field in fields) field.key: TextEditingController(),
    };
    try {
      return await showDialog<Map<String, String>>(
        context: context,
        builder: (dialogContext) => AlertDialog(
          title: Text(title),
          content: ConstrainedBox(
            constraints: BoxConstraints(maxWidth: 460),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                for (final field in fields) ...[
                  TextField(
                    controller: controllers[field.key],
                    obscureText: field.obscure,
                    keyboardType: field.keyboardType,
                    autocorrect: false,
                    enableSuggestions: !field.obscure,
                    decoration: InputDecoration(labelText: field.label),
                  ),
                  SizedBox(height: 12),
                ],
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext),
              child: Text(L10n.of(context).ui_cancel_35afca3b),
            ),
            FilledButton(
              style: destructive
                  ? FilledButton.styleFrom(
                      backgroundColor: context.kaede.danger)
                  : null,
              onPressed: () {
                final values = <String, String>{
                  for (final field in fields)
                    field.key: field.obscure
                        ? controllers[field.key]!.text
                        : controllers[field.key]!.text.trim(),
                };
                if (values.values.any((value) => value.isEmpty)) {
                  ScaffoldMessenger.of(dialogContext).showSnackBar(
                    SnackBar(
                      content: Text(L10n.of(context)
                          .ui_complete_every_field_before_continuing_27efd3e0),
                    ),
                  );
                  return;
                }
                Navigator.pop(dialogContext, values);
              },
              child: Text(action),
            ),
          ],
        ),
      );
    } finally {
      for (final controller in controllers.values) {
        controller.dispose();
      }
    }
  }

  Future<String?> _showMfaSetup({
    required String secret,
    required String uri,
  }) async {
    final controller = TextEditingController();
    try {
      return await showDialog<String>(
        context: context,
        barrierDismissible: false,
        builder: (dialogContext) => AlertDialog(
          title: Text(
              L10n.of(context).ui_add_kaede_to_your_authenticator_d07ff1f7),
          content: ConstrainedBox(
            constraints: BoxConstraints(maxWidth: 480),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(L10n.of(context)
                    .ui_enter_this_setup_key_in_your_authenticator_ap_b81703ff),
                SizedBox(height: 14),
                SelectableText(secret,
                    style: TextStyle(
                        fontFamily: 'monospace', fontWeight: FontWeight.w800)),
                if (uri.isNotEmpty) ...[
                  SizedBox(height: 8),
                  ExpansionTile(
                    tilePadding: EdgeInsets.zero,
                    title:
                        Text(L10n.of(context).ui_advanced_setup_uri_f9f13308),
                    children: [SelectableText(uri)],
                  ),
                ],
                SizedBox(height: 12),
                TextField(
                  controller: controller,
                  keyboardType: TextInputType.number,
                  autofillHints: const [AutofillHints.oneTimeCode],
                  decoration: InputDecoration(
                      labelText:
                          L10n.of(context).ui_verification_code_cb8e95b7),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext),
              child: Text(L10n.of(context).ui_cancel_35afca3b),
            ),
            FilledButton(
              onPressed: () {
                final code = controller.text.trim();
                if (code.length < 6) {
                  ScaffoldMessenger.of(dialogContext).showSnackBar(
                    SnackBar(
                      content: Text(L10n.of(context)
                          .ui_enter_the_full_verification_code_ec2d5c93),
                    ),
                  );
                  return;
                }
                Navigator.pop(dialogContext, code);
              },
              child: Text(L10n.of(context).ui_verify_and_enable_eba13f60),
            ),
          ],
        ),
      );
    } finally {
      controller.dispose();
    }
  }

  Future<void> _showRecoveryCodes(List<String> codes) => showDialog<void>(
        context: context,
        barrierDismissible: false,
        builder: (dialogContext) => AlertDialog(
          title: Text(L10n.of(context).ui_save_your_recovery_codes_155a83d0),
          content: ConstrainedBox(
            constraints: BoxConstraints(maxWidth: 460),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(L10n.of(context)
                    .ui_each_code_can_be_used_once_if_you_lose_your_a_5e78a307),
                SizedBox(height: 14),
                SelectableText(codes.join('\\n'),
                    style: TextStyle(fontFamily: 'monospace')),
              ],
            ),
          ),
          actions: [
            FilledButton(
              onPressed: () => Navigator.pop(dialogContext),
              child: Text(L10n.of(context).ui_i_saved_them_a4929513),
            ),
          ],
        ),
      );

  Future<void> _saveSetting(
    String key,
    Object? value, {
    bool ensurePush = true,
  }) async {
    final previous = _settings[key];
    setState(() => _settings[key] = value);
    try {
      final updated = await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .updateSettings(<String, Object?>{key: value});
      ref.read(mobileControllerProvider.notifier).applySettings(updated);
      if (key == 'locale') await setAppLanguage('$value', explicit: true);
      if (ensurePush &&
          key == 'notification_settings' &&
          value is Map<Object?, Object?>) {
        if (value.values.any((item) => item == true)) {
          await ref
              .read(mobileControllerProvider.notifier)
              .enablePushNotifications();
        }
      }
      if (mounted) setState(() => _settings = updated);
    } on Object catch (error) {
      if (mounted) setState(() => _settings[key] = previous);
      _showError(error,
          summary: L10n.current.ui_could_not_save_that_setting_0e78d469);
    }
  }

  bool _notification(String key, bool fallback) {
    final notification = _settings['notification_settings'];
    return notification is Map<Object?, Object?> && notification[key] is bool
        ? notification[key]! as bool
        : fallback;
  }

  Future<void> _saveNotification(String key, bool value) async {
    final current = _settings['notification_settings'];
    final next = current is Map<Object?, Object?>
        ? Map<String, Object?>.from(current)
        : <String, Object?>{};
    next[key] = value;
    await _saveSetting('notification_settings', next);
  }

  Future<void> _saveDeveloperMode(bool enabled) async {
    final current = _settings['notification_settings'];
    final next = current is Map<Object?, Object?>
        ? Map<String, Object?>.from(current)
        : <String, Object?>{};
    next['developer_mode'] = enabled;
    await _saveSetting(
      'notification_settings',
      next,
      ensurePush: false,
    );
  }

  TtsPreferences get _ttsPreferences {
    final notification = _settings['notification_settings'];
    return TtsPreferences.fromSettings(
      notification is Map<Object?, Object?> ? notification : null,
    );
  }

  Future<void> _saveTtsPreferences(TtsPreferences preferences) async {
    final notification = _settings['notification_settings'];
    await _saveSetting(
      'notification_settings',
      preferences.mergeInto(
        notification is Map<Object?, Object?> ? notification : null,
      ),
      ensurePush: false,
    );
  }

  Future<void> _setBiometricLock(bool enabled) async {
    if (enabled) {
      final auth = LocalAuthentication();
      try {
        if (!await auth.isDeviceSupported() ||
            !await auth.authenticate(
              localizedReason:
                  L10n.current.ui_enable_device_lock_for_kaede_chat_60093c1e,
              options: AuthenticationOptions(
                biometricOnly: false,
                stickyAuth: true,
              ),
            )) {
          return;
        }
      } on Object catch (error) {
        _showError(error,
            summary: L10n.current.ui_could_not_enable_the_app_lock_317a7765);
        return;
      }
    }
    try {
      final preferences = await SharedPreferences.getInstance();
      await preferences.setBool('biometric_lock', enabled);
      if (mounted) setState(() => _biometricLock = enabled);
    } on Object catch (error) {
      _showError(error,
          summary:
              L10n.current.ui_could_not_save_the_app_lock_setting_29561c23);
    }
  }

  Future<void> _setOpusDtx(bool enabled) async {
    try {
      await ref.read(voiceSessionProvider).setOpusDtx(enabled);
      if (mounted) setState(() => _opusDtx = enabled);
    } on Object catch (error) {
      _showError(error,
          summary:
              L10n.current.ui_could_not_save_the_opus_dtx_setting_2a28595e);
    }
  }

  Future<void> _setBiometricLockTimeout(int seconds) async {
    try {
      final preferences = await SharedPreferences.getInstance();
      await preferences.setInt('biometric_lock_timeout_seconds', seconds);
      if (mounted) setState(() => _biometricLockTimeout = seconds);
    } on Object catch (error) {
      _showError(error,
          summary:
              L10n.current.ui_could_not_save_the_app_lock_timeout_5aa1cd90);
    }
  }

  Future<void> _revoke(String id) async {
    try {
      await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .revokeSession(id);
      await _load();
      _showSuccess('Device signed out');
    } on Object catch (error) {
      _showError(error,
          summary: L10n.current.ui_could_not_sign_out_that_device_47a81c51);
    }
  }

  String _sessionSubtitle(Map<String, Object?> session) {
    final seen = session['last_seen_at'] ?? session['created_at'];
    return seen == null ? 'Active session' : 'Last active $seen';
  }

  IconData _deviceIcon(String name) {
    final lower = name.toLowerCase();
    if (lower.contains('ios') ||
        lower.contains('android') ||
        lower.contains('mobile')) {
      return Icons.smartphone_rounded;
    }
    return Icons.computer_rounded;
  }

  void _showSuccess(String message) {
    if (mounted) {
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(message)));
    }
  }

  void _showError(Object error, {String? summary}) {
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(userFacingError(error, summary: summary)),
          backgroundColor: context.kaede.danger));
    }
  }
}

/// Muted leading glyph for a flat settings row.
class _LeadingIcon extends StatelessWidget {
  const _LeadingIcon(this.icon, {this.color});

  final IconData icon;
  final Color? color;

  @override
  Widget build(BuildContext context) => Container(
        width: 26,
        height: 26,
        alignment: Alignment.center,
        child: Icon(icon, size: 20, color: color ?? context.kaede.muted),
      );
}

/// Presence dot used as the leading glyph of the activity rows.
class _PresenceIcon extends StatelessWidget {
  const _PresenceIcon(this.status);

  final PresenceStatus status;

  @override
  Widget build(BuildContext context) => Container(
        width: 26,
        height: 26,
        alignment: Alignment.center,
        child: Icon(
          presenceIcon(status),
          size: 18,
          color: presenceColor(context, status),
        ),
      );
}

/// Banner, avatar and identity at the top of settings, Discord-style: a flat
/// banner strip with the avatar overlapping its bottom edge and the edit
/// buttons drawn directly on the images.
class _AccountHero extends StatelessWidget {
  const _AccountHero({
    required this.user,
    required this.presence,
    required this.onEditAvatar,
    required this.onEditBanner,
    required this.onRemoveAvatar,
    required this.onRemoveBanner,
  });

  final KaedeUser? user;
  final PresenceStatus presence;
  final VoidCallback? onEditAvatar;
  final VoidCallback? onEditBanner;
  final VoidCallback? onRemoveAvatar;
  final VoidCallback? onRemoveBanner;

  @override
  Widget build(BuildContext context) {
    final banner = user == null
        ? null
        : publicAssetUri(
            user!.ref.domain,
            user!.bannerHash,
            variant: 'thumbnail_1024',
          );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SizedBox(
          height: 96,
          child: ClipRRect(
            borderRadius: BorderRadius.circular(14),
            child: Stack(
              fit: StackFit.expand,
              children: [
                banner == null
                    ? DecoratedBox(
                        decoration: BoxDecoration(
                          gradient: LinearGradient(
                            begin: Alignment.topLeft,
                            end: Alignment.bottomRight,
                            colors: [
                              context.kaede.coralSoft,
                              context.kaede.purpleSoft,
                            ],
                          ),
                        ),
                      )
                    : CachedNetworkImage(
                        imageUrl: '$banner',
                        fit: BoxFit.cover,
                        errorWidget: (_, __, ___) =>
                            ColoredBox(color: context.kaede.coralSoft),
                      ),
                Positioned(
                  right: 10,
                  top: 10,
                  child: Row(
                    children: [
                      if (onRemoveBanner != null) ...[
                        _HeroEditButton(
                          icon: Icons.delete_outline_rounded,
                          tooltip: L10n.of(context).ui_remove_banner_74d33021,
                          onPressed: onRemoveBanner,
                          danger: true,
                        ),
                        SizedBox(width: 6),
                      ],
                      _HeroEditButton(
                        icon: Icons.panorama_rounded,
                        tooltip: L10n.of(context).ui_change_banner_ae718a61,
                        onPressed: onEditBanner,
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
        Padding(
          padding: EdgeInsets.fromLTRB(4, 0, 4, 4),
          child: Row(
            children: [
              SizedBox(
                width: 84,
                height: 84,
                child: Stack(
                  clipBehavior: Clip.none,
                  children: [
                    Positioned.fill(
                      child: Material(
                        color: settingsSurface(context),
                        shape: CircleBorder(),
                        child: user == null
                            ? CircleAvatar(
                                radius: 36,
                                backgroundColor: context.kaede.raised,
                                child: Icon(Icons.person_rounded,
                                    size: 34, color: context.kaede.textSoft),
                              )
                            : UserAvatar(
                                user: user!,
                                radius: 36,
                                presence: presence,
                                ringColor: settingsSurface(context),
                              ),
                      ),
                    ),
                    Positioned(
                      right: -6,
                      bottom: -2,
                      child: Row(
                        children: [
                          if (onRemoveAvatar != null) ...[
                            _HeroEditButton(
                              icon: Icons.delete_outline_rounded,
                              tooltip:
                                  L10n.of(context).ui_remove_avatar_d6403a8e,
                              onPressed: onRemoveAvatar,
                              danger: true,
                            ),
                            SizedBox(width: 4),
                          ],
                          _HeroEditButton(
                            icon: Icons.photo_camera_rounded,
                            tooltip: L10n.of(context).ui_change_avatar_7b892dce,
                            onPressed: onEditAvatar,
                          ),
                        ],
                      ),
                    ),
                  ],
                ),
              ),
              SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      user?.name ?? 'Your account',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontSize: 22,
                        fontWeight: FontWeight.w800,
                        letterSpacing: -.4,
                      ),
                    ),
                    SizedBox(height: 2),
                    Text(
                      user?.handle ?? '',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: context.kaede.muted,
                        fontSize: 13.5,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

/// Small circular action drawn on the hero images.
class _HeroEditButton extends StatelessWidget {
  const _HeroEditButton({
    required this.icon,
    required this.tooltip,
    required this.onPressed,
    this.danger = false,
  });

  final IconData icon;
  final String tooltip;
  final VoidCallback? onPressed;
  final bool danger;

  @override
  Widget build(BuildContext context) => Material(
        color: context.kaede.canvas.withValues(alpha: .78),
        borderRadius: BorderRadius.circular(16),
        child: InkWell(
          onTap: onPressed,
          borderRadius: BorderRadius.circular(16),
          child: Padding(
            padding: EdgeInsets.all(6),
            child: Tooltip(
              message: tooltip,
              child: Icon(
                icon,
                size: 16,
                color: danger ? context.kaede.danger : context.kaede.text,
              ),
            ),
          ),
        ),
      );
}

final class _DialogField {
  const _DialogField(this.key, this.label,
      {this.keyboardType, this.obscure = false});

  final String key;
  final String label;
  final TextInputType? keyboardType;
  final bool obscure;
}
