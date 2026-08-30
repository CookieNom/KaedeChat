import 'dart:async';
import 'dart:io';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:image_picker/image_picker.dart';
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
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:local_auth/local_auth.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Account settings, laid out Discord-style: one flat scrolling surface with
/// uppercase section headers, hover rows and Discord toggles instead of a
/// stack of bordered cards.
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
  var _biometricLock = false;
  var _biometricLockTimeout = 30;
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
              child: Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, input.text),
              child: Text('Continue'),
            ),
          ],
        ),
      );
    } finally {
      input.dispose();
    }
  }

  Future<void> _initializeEncryption() async {
    setState(() => _saving = true);
    try {
      await ref.read(mobileControllerProvider.notifier).e2eeClient();
      _showSuccess('This device is ready for end-to-end encryption.');
    } on Object catch (error) {
      _showError(error, summary: 'Could not initialize encryption');
    } finally {
      if (mounted) setState(() => _saving = false);
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
          title: Text('Save your encrypted recovery backup'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Anyone with this backup and its passphrase can read your encrypted history. Store both separately. Restore it only as a recovery action; Kaede will reconcile it with the shared account vault. The portable plaintext cache retains at most 2,000 recent messages or 8 MiB, so older history may require another trusted client.',
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
                    SnackBar(content: Text('Recovery backup copied.')),
                  );
                }
              },
              icon: Icon(Icons.copy_rounded),
              label: Text('Copy'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context),
              child: Text('I saved it'),
            ),
          ],
        ),
      );
    } on Object catch (error) {
      _showError(error, summary: 'Could not create the recovery backup');
    }
  }

  Future<void> _importEncryptionRecovery() async {
    final bundle = TextEditingController();
    final passphrase = TextEditingController();
    try {
      final accepted = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: Text('Restore encrypted history'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  'Use a recovery backup when automatic account-vault recovery is unavailable. Restoring replaces this client\u2019s cached state and resumes the same portable account identity. Backups carry at most 2,000 recent decrypted messages or 8 MiB of cached plaintext.',
                ),
                SizedBox(height: 12),
                TextField(
                  controller: bundle,
                  minLines: 4,
                  maxLines: 8,
                  decoration: InputDecoration(labelText: 'Recovery backup'),
                ),
                SizedBox(height: 12),
                TextField(
                  controller: passphrase,
                  obscureText: true,
                  decoration: InputDecoration(labelText: 'Passphrase'),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: Text('Replace and restore'),
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
      _showError(error, summary: 'Could not restore encrypted history');
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
              Text('Encryption identity',
                  style: Theme.of(context).textTheme.titleLarge),
              SizedBox(height: 6),
              Text(
                'Your signed-in clients share one portable MLS identity. Rotating it abandons unavailable encrypted history and pauses affected rooms until a member rotates their keys.',
              ),
              SizedBox(height: 12),
              for (final device in devices)
                ListTile(
                  leading: Icon(device['revoked_at'] == null
                      ? Icons.verified_user_outlined
                      : Icons.phonelink_erase_rounded),
                  title: Text('Portable account identity'),
                  subtitle: Text(
                    'Last enrolled from ${device['device_name'] ?? 'Kaede'} (${device['platform'] ?? 'unknown'}) \u00b7 ${device['id']}'
                    '${device['id'] == currentDeviceId ? ' \u00b7 Loaded here' : ''}',
                  ),
                  trailing: device['revoked_at'] != null
                      ? Text('Revoked')
                      : IconButton(
                          tooltip: 'Rotate encryption identity',
                          onPressed: () async {
                            final accepted = await showDialog<bool>(
                              context: context,
                              builder: (dialogContext) => AlertDialog(
                                title: Text('Start a new encryption identity?'),
                                content: Text(
                                  'This abandons encrypted history that is unavailable from an enrolled client or recovery backup. Every signed-in client must load the new identity, and affected rooms pause until their keys are rotated.',
                                ),
                                actions: [
                                  TextButton(
                                    onPressed: () =>
                                        Navigator.pop(dialogContext, false),
                                    child: Text('Cancel'),
                                  ),
                                  FilledButton(
                                    onPressed: () =>
                                        Navigator.pop(dialogContext, true),
                                    child: Text('Start fresh'),
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
                                summary:
                                    'Could not create a new encryption identity',
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
      _showError(error, summary: 'Could not load encryption identity');
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
        _loadError = null;
        _loading = false;
      });
    } on Object catch (error) {
      if (mounted) {
        setState(() {
          _loadError = userFacingError(
            error,
            summary:
                'Account settings could not be loaded. Some controls may show temporary defaults.',
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
                        ? 'SUSPENDED · UNAVAILABLE'
                        : 'REVOKED · UNAVAILABLE',
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
                                'Commands unavailable',
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
                  title: Text('Guild channels'),
                  onChanged: grantsEditable
                      ? (value) => setSheetState(() => guilds = value ?? false)
                      : null,
                ),
                CheckboxListTile(
                  contentPadding: EdgeInsets.zero,
                  value: privateChannels,
                  title: Text('Private conversations'),
                  onChanged: grantsEditable
                      ? (value) => setSheetState(
                            () => privateChannels = value ?? false,
                          )
                      : null,
                ),
                CheckboxListTile(
                  contentPadding: EdgeInsets.zero,
                  value: botDms,
                  title: Text('Direct messages with bots'),
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
                  child: Text('Save command access'),
                ),
                TextButton(
                  style: TextButton.styleFrom(
                      foregroundColor: context.kaede.danger),
                  onPressed: () => Navigator.pop(sheetContext, 'revoke'),
                  child: Text('Revoke app'),
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
          title: Text('Revoke ${installation.applicationName}?'),
          content: Text(
            'Its user-installed commands will disappear. The app may retain information you previously sent it.',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext, false),
              child: Text('Cancel'),
            ),
            FilledButton(
              style:
                  FilledButton.styleFrom(backgroundColor: context.kaede.danger),
              onPressed: () => Navigator.pop(dialogContext, true),
              child: Text('Revoke'),
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
      _showError(error, summary: 'Could not update that authorized app');
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
                    child: Text('Retry'),
                  ),
                ],
              ),
            ),
          ],
          SettingsSectionHeader('Profile',
              subheading: 'How people see you across the federation.'),
          Padding(
            padding: EdgeInsets.symmetric(horizontal: 4),
            child: Column(
              children: [
                SettingsField(
                  label: 'DISPLAY NAME',
                  controller: _displayName,
                  enabled: !_saving,
                ),
                SizedBox(height: 16),
                SettingsField(
                  label: 'CUSTOM STATUS',
                  controller: _customStatus,
                  maxLength: 128,
                  enabled: !_saving,
                ),
                SizedBox(height: 16),
                SettingsField(
                  label: 'ABOUT ME',
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
                  label: Text('Save profile'),
                ),
              ],
            ),
          ),
          SettingsSectionHeader('Account',
              subheading: 'The identity hosted by your home instance.'),
          SettingsRow.chevron(
            title: user?.email ?? 'No email address',
            subtitle: user?.email == null
                ? 'This instance does not require email.'
                : user!.emailVerified
                    ? 'Verified email'
                    : 'Email verification is pending',
            leading: _LeadingIcon(Icons.alternate_email_rounded),
            onTap: _saving ? null : _changeEmail,
          ),
          SettingsRow.chevron(
            divider: true,
            title: 'Confirm email change',
            subtitle: 'Enter the token from your confirmation email.',
            leading: _LeadingIcon(Icons.mark_email_read_outlined),
            onTap: _saving ? null : _confirmEmail,
          ),
          SizedBox(height: 16),
          SettingsRow(
            title: 'Authenticator app',
            subtitle: user?.mfaEnabled == true
                ? 'Two-factor authentication is enabled.'
                : 'Require a code in addition to your password.',
            leading: _LeadingIcon(Icons.password_rounded),
            divider: true,
            onTap: user?.mfaEnabled == true ? _disableMfa : _enableMfa,
          ),
          SettingsSectionHeader('Security',
              subheading:
                  'Encryption keys unlock your account vault on each trusted device. Your instance stores only ciphertext.'),
          SettingsRow.chevron(
            title: 'Set up this device',
            subtitle: 'Enable end-to-end encryption on this phone.',
            leading: _LeadingIcon(Icons.key_rounded),
            onTap: _saving ? null : _initializeEncryption,
          ),
          SettingsRow.chevron(
            title: 'Encryption identity',
            subtitle: 'Devices sharing this account\u2019s MLS identity.',
            leading: _LeadingIcon(Icons.devices_other_rounded),
            divider: true,
            onTap: _saving ? null : _manageEncryptionDevices,
          ),
          SettingsRow.chevron(
            title: 'Create recovery backup',
            leading: _LeadingIcon(Icons.download_for_offline_outlined),
            divider: true,
            onTap: _saving ? null : _exportEncryptionRecovery,
          ),
          SettingsRow.chevron(
            title: 'Restore recovery backup',
            leading: _LeadingIcon(Icons.restore_rounded),
            onTap: _saving ? null : _importEncryptionRecovery,
          ),
          SettingsSectionHeader('Activity status',
              subheading:
                  'Your availability follows you between mobile, desktop, and web.'),
          SettingsRow(
            title: 'Online',
            leading: _PresenceIcon(PresenceStatus.online),
            divider: true,
            onTap: () => _saveSetting('presence_preference', 'online'),
            trailing: _presenceCheck(presence == 'online'),
          ),
          SettingsRow(
            title: 'Idle',
            leading: _PresenceIcon(PresenceStatus.idle),
            divider: true,
            onTap: () => _saveSetting('presence_preference', 'idle'),
            trailing: _presenceCheck(presence == 'idle'),
          ),
          SettingsRow(
            title: 'Do not disturb',
            leading: _PresenceIcon(PresenceStatus.dnd),
            divider: true,
            onTap: () => _saveSetting('presence_preference', 'dnd'),
            trailing: _presenceCheck(presence == 'dnd'),
          ),
          SettingsRow(
            title: 'Invisible',
            leading: _PresenceIcon(PresenceStatus.invisible),
            onTap: () => _saveSetting('presence_preference', 'invisible'),
            trailing: _presenceCheck(presence == 'invisible'),
          ),
          SettingsSectionHeader(
            'Appearance',
            subheading:
                'Theme and regional formatting follow your account across clients.',
          ),
          SettingsChoiceRow(
            title: 'Theme',
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
                title: 'Theme',
                description:
                    'Sync with device follows the operating system light or dark appearance.',
                choices: const [
                  SettingsChoice('system', 'Sync with device'),
                  SettingsChoice('light', 'Light'),
                  SettingsChoice('dark', 'Dark'),
                ],
                selected: value,
              );
              if (chosen != null && chosen != value) {
                await _saveSetting('theme', chosen);
              }
            },
          ),
          SettingsChoiceRow(
            title: 'Locale and formats',
            subtitle:
                'Controls dates, system components and localized app-command labels. Kaede interface text is currently English.',
            value: '${_settings['locale'] ?? 'en-US'}',
            display: switch ('${_settings['locale'] ?? 'en-US'}') {
              'ja-JP' => 'Japanese formats (Japan)',
              _ => 'English (United States)',
            },
            leading: const _LeadingIcon(Icons.translate_rounded),
            onSelected: (value) async {
              final chosen = await showSettingsChoiceSheet(
                context,
                title: 'Locale and formats',
                description:
                    'This does not translate Kaede interface text, which is currently English.',
                choices: const [
                  SettingsChoice('en-US', 'English (United States)'),
                  SettingsChoice('ja-JP', 'Japanese formats (Japan)'),
                ],
                selected: value,
              );
              if (chosen != null && chosen != value) {
                await _saveSetting('locale', chosen);
              }
            },
          ),
          SettingsSectionHeader('Notifications',
              subheading: usesRelay
                  ? 'Closed-app delivery runs through Kaede Push Relay ($pushRelayHost). The relay sees your home instance and an opaque device subscription, but never message text, sender names, rooms or encryption keys.'
                  : 'This community build uses its own Firebase provider for closed-app delivery. Firebase only receives an opaque wake with no message content, and your lock-screen privacy settings still apply.'),
          SettingsRow.chevron(
            title: 'Enable background notifications',
            leading: _LeadingIcon(Icons.notifications_active_outlined),
            divider: true,
            onTap: () async {
              try {
                final enabled = await ref
                    .read(mobileControllerProvider.notifier)
                    .enablePushNotifications();
                if (!context.mounted) return;
                ScaffoldMessenger.of(context).showSnackBar(SnackBar(
                  content: Text(enabled
                      ? 'System notifications are enabled.'
                      : 'Android blocked notifications. Allow them in system settings, then try again.'),
                ));
              } on Object catch (error) {
                _showError(
                  error,
                  summary: 'Could not enable system notifications',
                );
              }
            },
          ),
          SettingsRow.chevron(
            title: 'Disable background notifications',
            leading: _LeadingIcon(Icons.notifications_off_outlined),
            divider: true,
            onTap: () async {
              await ref
                  .read(mobileControllerProvider.notifier)
                  .disablePushNotifications();
              if (!context.mounted) return;
              ScaffoldMessenger.of(context).showSnackBar(SnackBar(
                content: Text(
                  'Background notifications are disabled for this account.',
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
                        'Notification delivery needs attention',
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
            title: 'Direct messages',
            value: _notification('direct_messages', true),
            onChanged: (value) => _saveNotification('direct_messages', value),
            divider: true,
          ),
          SettingsSwitchRow(
            title: 'Mentions and replies',
            value: _notification('mentions', true),
            onChanged: (value) => _saveNotification('mentions', value),
            divider: true,
          ),
          SettingsSwitchRow(
            title: 'Friend requests',
            value: _notification('relationships', true),
            onChanged: (value) => _saveNotification('relationships', value),
            divider: true,
          ),
          SettingsSwitchRow(
            title: 'Show message previews',
            subtitle:
                'Shows the sender, text and profile picture after the private wake-up. FCM never receives them.',
            value: _notification('show_notification_previews', true),
            onChanged: (value) =>
                _saveNotification('show_notification_previews', value),
          ),
          SettingsChoiceRow(
            title: 'Text-to-Speech',
            subtitle:
                'Choose which incoming TTS messages this phone reads aloud.',
            value: tts.playback.name,
            display: switch (tts.playback) {
              TtsPlaybackMode.all => 'All channels',
              TtsPlaybackMode.current => 'Current channel',
              TtsPlaybackMode.never => 'Never',
            },
            onSelected: (value) async {
              final chosen = await showSettingsChoiceSheet(
                context,
                title: 'Text-to-Speech playback',
                description:
                    'This only affects messages marked as TTS. Ordinary messages are never spoken.',
                choices: const <SettingsChoice>[
                  SettingsChoice('all', 'For all channels'),
                  SettingsChoice('current', 'For current selected channel'),
                  SettingsChoice('never', 'Never'),
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
            'Accessibility',
            subheading: 'Control Text-to-Speech playback and reading speed.',
          ),
          SettingsSwitchRow(
            title: 'Allow playback and usage of /tts command',
            subtitle:
                'When off, Kaede will not send or speak Text-to-Speech messages on this device.',
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
                  'Text-to-Speech rate · ${tts.rate.toStringAsFixed(1)}×',
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
          SettingsSectionHeader('Privacy'),
          SettingsRow.chevron(
            title: 'My reports',
            subtitle: 'Review reports submitted to Trust & Safety.',
            leading: _LeadingIcon(Icons.flag_outlined),
            divider: true,
            onTap: () => Navigator.of(context).push(
              MaterialPageRoute<void>(builder: (_) => MyReportsScreen()),
            ),
          ),
          SettingsChoiceRow(
            title: 'Who can message you',
            value: '${_settings['dm_privacy'] ?? 'friends'}',
            display: _dmPrivacyLabel('${_settings['dm_privacy'] ?? 'friends'}'),
            leading: _LeadingIcon(Icons.lock_outline_rounded),
            divider: true,
            onSelected: (value) async {
              final chosen = await showSettingsChoiceSheet(
                context,
                title: 'Who can message you',
                choices: const [
                  SettingsChoice('everyone', 'Everyone',
                      hint: 'Any Kaede account can start a direct message.'),
                  SettingsChoice('friends', 'Friends only',
                      hint: 'Only people you have added as a friend.'),
                  SettingsChoice('shared_guild', 'Friends and shared guilds',
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
            title: 'Age-restricted commands in direct messages',
            subtitle: switch (user?.ageAssuranceState) {
              'adult' =>
                'Allow age-restricted application commands in DMs and group DMs.',
              'minor' =>
                'Unavailable because this account is age-assured as a minor.',
              _ =>
                'Unavailable until your instance completes age assurance for this account.',
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
            title: 'Lock Kaede when you leave',
            subtitle:
                'Unlock with biometrics, your device passcode, or your device PIN.',
            value: _biometricLock,
            onChanged: _setBiometricLock,
          ),
          if (_biometricLock)
            SettingsChoiceRow(
              title: 'Lock after leaving',
              value: '$_biometricLockTimeout',
              display: _lockTimeoutLabel(_biometricLockTimeout),
              onSelected: (value) {
                final chosen = int.tryParse(value);
                if (chosen != null) _setBiometricLockTimeout(chosen);
              },
            ),
          SettingsSectionHeader(
            'Authorized apps',
            subheading:
                'Apps installed for your account and the contexts where their commands appear.',
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
            'Developer',
            subheading:
                'Build applications and reveal qualified technical IDs in context menus.',
          ),
          SettingsSwitchRow(
            title: 'Developer mode',
            subtitle:
                'Adds Copy ID actions for users, servers, channels, messages and applications.',
            value: mobile.developerMode,
            onChanged: _saveDeveloperMode,
            divider: true,
          ),
          if (mobile.developerMode && user != null)
            SettingsRow(
              title: 'Copy my user ID',
              subtitle: user.ref.wire,
              leading: const _LeadingIcon(Icons.badge_outlined),
              divider: true,
              onTap: () async {
                await Clipboard.setData(ClipboardData(text: user.ref.wire));
                _showSuccess('User ID copied.');
              },
            ),
          SettingsRow.chevron(
            title: 'Developer Portal',
            subtitle:
                'Applications, teams, commands, credentials, workers, installs and media.',
            leading: _LeadingIcon(Icons.developer_board_outlined),
            onTap: () => Navigator.of(context).push(
              MaterialPageRoute<void>(
                builder: (_) => DeveloperPortalScreen(),
              ),
            ),
          ),
          if (_adminAvailable) ...[
            SettingsSectionHeader(
              'Administration',
              subheading:
                  'Capability-gated instance operations and Trust & Safety.',
            ),
            SettingsRow.chevron(
              title: 'Instance administration',
              subtitle:
                  'Users, applications, reports, federation blocks, operators and audit log.',
              leading: const _LeadingIcon(Icons.admin_panel_settings_outlined),
              onTap: () => Navigator.of(context).push(
                MaterialPageRoute<void>(
                  builder: (_) => InstanceAdministrationScreen(),
                ),
              ),
            ),
          ],
          SettingsSectionHeader('Devices',
              subheading: 'Signed-in devices on this account.'),
          SettingsRow(
            title: 'This device',
            subtitle: 'Current session',
            leading: _LeadingIcon(Icons.check_circle_rounded,
                color: context.kaede.mint),
          ),
          for (final session in _sessions)
            SettingsRow(
              title: '${session['device_name'] ?? 'Kaede client'}',
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
                child: Text('Sign out'),
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
              child: Text('Open-source licences'),
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
        title: Text('Log out of Kaede?'),
        content: Text(
          'Saved conversations on this device stay encrypted at rest and are '
          'removed when you sign out.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: Text('Stay signed in'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: context.kaede.danger,
            ),
            onPressed: () => Navigator.pop(dialogContext, true),
            child: Text('Log out'),
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
      _showError(error, summary: 'Could not save the profile');
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
      _showError('Choose a PNG, JPEG, GIF, or WebP image.');
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
        summary:
            'Could not update the ${kind == 'avatar' ? 'avatar' : 'banner'}',
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
        title: Text('Remove your $label?'),
        content: Text('You can upload a new $label at any time.'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: Text('Cancel'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: context.kaede.danger,
            ),
            onPressed: () => Navigator.pop(dialogContext, true),
            child: Text('Remove'),
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
      _showError(error, summary: 'Could not remove your $label');
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _changeEmail() async {
    final values = await _credentialsDialog(
      title: 'Change email',
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
      title: 'Confirm email change',
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
      title: 'Set up authenticator',
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
      title: 'Disable two-factor authentication',
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
              child: Text('Cancel'),
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
                      content: Text('Complete every field before continuing.'),
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
          title: Text('Add Kaede to your authenticator'),
          content: ConstrainedBox(
            constraints: BoxConstraints(maxWidth: 480),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                    'Enter this setup key in your authenticator app, then verify the six-digit code.'),
                SizedBox(height: 14),
                SelectableText(secret,
                    style: TextStyle(
                        fontFamily: 'monospace', fontWeight: FontWeight.w800)),
                if (uri.isNotEmpty) ...[
                  SizedBox(height: 8),
                  ExpansionTile(
                    tilePadding: EdgeInsets.zero,
                    title: Text('Advanced setup URI'),
                    children: [SelectableText(uri)],
                  ),
                ],
                SizedBox(height: 12),
                TextField(
                  controller: controller,
                  keyboardType: TextInputType.number,
                  autofillHints: const [AutofillHints.oneTimeCode],
                  decoration: InputDecoration(labelText: 'Verification code'),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext),
              child: Text('Cancel'),
            ),
            FilledButton(
              onPressed: () {
                final code = controller.text.trim();
                if (code.length < 6) {
                  ScaffoldMessenger.of(dialogContext).showSnackBar(
                    SnackBar(
                      content: Text('Enter the full verification code.'),
                    ),
                  );
                  return;
                }
                Navigator.pop(dialogContext, code);
              },
              child: Text('Verify and enable'),
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
          title: Text('Save your recovery codes'),
          content: ConstrainedBox(
            constraints: BoxConstraints(maxWidth: 460),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                    'Each code can be used once if you lose your authenticator. Kaede will not show them again.'),
                SizedBox(height: 14),
                SelectableText(codes.join('\\n'),
                    style: TextStyle(fontFamily: 'monospace')),
              ],
            ),
          ),
          actions: [
            FilledButton(
              onPressed: () => Navigator.pop(dialogContext),
              child: Text('I saved them'),
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
      _showError(error, summary: 'Could not save that setting');
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
              localizedReason: 'Enable device lock for Kaede Chat',
              options: AuthenticationOptions(
                biometricOnly: false,
                stickyAuth: true,
              ),
            )) {
          return;
        }
      } on Object catch (error) {
        _showError(error, summary: 'Could not enable the app lock');
        return;
      }
    }
    try {
      final preferences = await SharedPreferences.getInstance();
      await preferences.setBool('biometric_lock', enabled);
      if (mounted) setState(() => _biometricLock = enabled);
    } on Object catch (error) {
      _showError(error, summary: 'Could not save the app lock setting');
    }
  }

  Future<void> _setBiometricLockTimeout(int seconds) async {
    try {
      final preferences = await SharedPreferences.getInstance();
      await preferences.setInt('biometric_lock_timeout_seconds', seconds);
      if (mounted) setState(() => _biometricLockTimeout = seconds);
    } on Object catch (error) {
      _showError(error, summary: 'Could not save the app lock timeout');
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
      _showError(error, summary: 'Could not sign out that device');
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
                          tooltip: 'Remove banner',
                          onPressed: onRemoveBanner,
                          danger: true,
                        ),
                        SizedBox(width: 6),
                      ],
                      _HeroEditButton(
                        icon: Icons.panorama_rounded,
                        tooltip: 'Change banner',
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
                              tooltip: 'Remove avatar',
                              onPressed: onRemoveAvatar,
                              danger: true,
                            ),
                            SizedBox(width: 4),
                          ],
                          _HeroEditButton(
                            icon: Icons.photo_camera_rounded,
                            tooltip: 'Change avatar',
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
