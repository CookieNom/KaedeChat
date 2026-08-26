import 'dart:async';
import 'dart:io';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:image_picker/image_picker.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/domain/models.dart';
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
  var _loading = true;
  var _saving = false;
  var _biometricLock = false;
  var _biometricLockTimeout = 30;
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
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, input.text),
              child: const Text('Continue'),
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
          title: const Text('Save your encrypted recovery backup'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  'Anyone with this backup and its passphrase can read your encrypted history. Store both separately. Restore it only as a recovery action; Kaede will reconcile it with the shared account vault. The portable plaintext cache retains at most 2,000 recent messages or 8 MiB, so older history may require another trusted client.',
                ),
                const SizedBox(height: 12),
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
                    const SnackBar(content: Text('Recovery backup copied.')),
                  );
                }
              },
              icon: const Icon(Icons.copy_rounded),
              label: const Text('Copy'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('I saved it'),
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
          title: const Text('Restore encrypted history'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Text(
                  'Use a recovery backup when automatic account-vault recovery is unavailable. Restoring replaces this client\u2019s cached state and resumes the same portable account identity. Backups carry at most 2,000 recent decrypted messages or 8 MiB of cached plaintext.',
                ),
                const SizedBox(height: 12),
                TextField(
                  controller: bundle,
                  minLines: 4,
                  maxLines: 8,
                  decoration:
                      const InputDecoration(labelText: 'Recovery backup'),
                ),
                const SizedBox(height: 12),
                TextField(
                  controller: passphrase,
                  obscureText: true,
                  decoration: const InputDecoration(labelText: 'Passphrase'),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Replace and restore'),
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
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
            children: [
              Text('Encryption identity',
                  style: Theme.of(context).textTheme.titleLarge),
              const SizedBox(height: 6),
              const Text(
                'Your signed-in clients share one portable MLS identity. Rotating it abandons unavailable encrypted history and pauses affected rooms until a member rotates their keys.',
              ),
              const SizedBox(height: 12),
              for (final device in devices)
                ListTile(
                  leading: Icon(device['revoked_at'] == null
                      ? Icons.verified_user_outlined
                      : Icons.phonelink_erase_rounded),
                  title: const Text('Portable account identity'),
                  subtitle: Text(
                    'Last enrolled from ${device['device_name'] ?? 'Kaede'} (${device['platform'] ?? 'unknown'}) \u00b7 ${device['id']}'
                    '${device['id'] == currentDeviceId ? ' \u00b7 Loaded here' : ''}',
                  ),
                  trailing: device['revoked_at'] != null
                      ? const Text('Revoked')
                      : IconButton(
                          tooltip: 'Rotate encryption identity',
                          onPressed: () async {
                            final accepted = await showDialog<bool>(
                              context: context,
                              builder: (dialogContext) => AlertDialog(
                                title: const Text(
                                    'Start a new encryption identity?'),
                                content: const Text(
                                  'This abandons encrypted history that is unavailable from an enrolled client or recovery backup. Every signed-in client must load the new identity, and affected rooms pause until their keys are rotated.',
                                ),
                                actions: [
                                  TextButton(
                                    onPressed: () =>
                                        Navigator.pop(dialogContext, false),
                                    child: const Text('Cancel'),
                                  ),
                                  FilledButton(
                                    onPressed: () =>
                                        Navigator.pop(dialogContext, true),
                                    child: const Text('Start fresh'),
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
                          icon: const Icon(Icons.delete_outline_rounded),
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
      final results = await Future.wait<Object>(
          [repository.settings(), repository.sessions()]);
      final user = ref.read(mobileControllerProvider).user;
      final preferences = await SharedPreferences.getInstance();
      if (!mounted) return;
      setState(() {
        _settings = Map<String, Object?>.from(results[0] as Map);
        _sessions = results[1] as List<Map<String, Object?>>;
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

  @override
  Widget build(BuildContext context) {
    final mobile = ref.watch(mobileControllerProvider);
    final user = mobile.user;
    if (_loading) {
      return const ColoredBox(
        color: kSettingsSurface,
        child: Center(child: CircularProgressIndicator()),
      );
    }
    final presence = '${_settings['presence_preference'] ?? 'online'}';
    final usesRelay = ref.read(mobileControllerProvider.notifier).usesPushRelay;
    final pushRelayHost =
        ref.read(mobileControllerProvider.notifier).pushRelayHost;

    return ColoredBox(
      color: kSettingsSurface,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 40),
        children: [
          _AccountHero(
            user: user,
            presence: mobile.presencePreference,
            onEditAvatar: _saving ? null : () => _pickAsset('avatar'),
            onEditBanner: _saving ? null : () => _pickAsset('banner'),
          ),
          if (_loadError case final warning?) ...[
            const SizedBox(height: 16),
            Container(
              padding: const EdgeInsets.fromLTRB(12, 10, 6, 10),
              decoration: BoxDecoration(
                color: KaedeColors.warningSoft,
                borderRadius: BorderRadius.circular(10),
              ),
              child: Row(
                children: [
                  const Icon(Icons.warning_amber_rounded,
                      size: 17, color: KaedeColors.warning),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      warning,
                      style: const TextStyle(
                        color: KaedeColors.warning,
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
                    style: TextButton.styleFrom(minimumSize: const Size(0, 34)),
                    child: const Text('Retry'),
                  ),
                ],
              ),
            ),
          ],
          const SettingsSectionHeader('Profile',
              subheading: 'How people see you across the federation.'),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 4),
            child: Column(
              children: [
                SettingsField(
                  label: 'DISPLAY NAME',
                  controller: _displayName,
                  enabled: !_saving,
                ),
                const SizedBox(height: 16),
                SettingsField(
                  label: 'CUSTOM STATUS',
                  controller: _customStatus,
                  maxLength: 128,
                  enabled: !_saving,
                ),
                const SizedBox(height: 16),
                SettingsField(
                  label: 'ABOUT ME',
                  controller: _bio,
                  maxLines: 4,
                  maxLength: 500,
                  enabled: !_saving,
                ),
                const SizedBox(height: 18),
                FilledButton.icon(
                  onPressed: _saving ? null : _saveProfile,
                  icon: _saving
                      ? const SizedBox.square(
                          dimension: 16,
                          child: CircularProgressIndicator(strokeWidth: 2))
                      : const Icon(Icons.check_rounded),
                  label: const Text('Save profile'),
                ),
              ],
            ),
          ),
          const SettingsSectionHeader('Account',
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
          const SizedBox(height: 16),
          SettingsRow(
            title: 'Authenticator app',
            subtitle: user?.mfaEnabled == true
                ? 'Two-factor authentication is enabled.'
                : 'Require a code in addition to your password.',
            leading: _LeadingIcon(Icons.password_rounded),
            divider: true,
            onTap: user?.mfaEnabled == true ? _disableMfa : _enableMfa,
          ),
          const SettingsSectionHeader('Security',
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
          const SettingsSectionHeader('Activity status',
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
              ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
                content: Text(
                  'Background notifications are disabled for this account.',
                ),
              ));
            },
          ),
          if (mobile.pushWarning case final warning?) ...[
            const SizedBox(height: 10),
            DecoratedBox(
              decoration: BoxDecoration(
                color: KaedeColors.warning.withValues(alpha: 0.12),
                borderRadius: BorderRadius.circular(10),
                border: Border.all(
                    color: KaedeColors.warning.withValues(alpha: .4)),
              ),
              child: Padding(
                padding:
                    const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                child: Row(
                  children: [
                    const Icon(Icons.warning_amber_rounded,
                        size: 18, color: KaedeColors.warning),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Text(
                        'Notification delivery needs attention',
                        style: const TextStyle(
                          color: KaedeColors.warning,
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
              padding: const EdgeInsets.fromLTRB(4, 6, 4, 4),
              child: Text(
                warning,
                style: const TextStyle(
                    color: KaedeColors.textSoft, fontSize: 12.5, height: 1.4),
              ),
            ),
          ],
          if (!ref.read(mobileControllerProvider.notifier).remotePushAvailable)
            const SettingsInfo(
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
          const SettingsInfo(
            'Do Not Disturb suppresses banners and sounds on every signed-in client.',
          ),
          const SettingsSectionHeader('Privacy'),
          SettingsRow.chevron(
            title: 'My reports',
            subtitle: 'Review reports submitted to Trust & Safety.',
            leading: _LeadingIcon(Icons.flag_outlined),
            divider: true,
            onTap: () => Navigator.of(context).push(
              MaterialPageRoute<void>(builder: (_) => const MyReportsScreen()),
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
          const SettingsSectionHeader('Devices',
              subheading: 'Signed-in devices on this account.'),
          SettingsRow(
            title: 'This device',
            subtitle: 'Current session',
            leading: _LeadingIcon(Icons.check_circle_rounded,
                color: KaedeColors.mint),
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
                  foregroundColor: KaedeColors.danger,
                  minimumSize: const Size(0, 36),
                  padding: const EdgeInsets.symmetric(horizontal: 10),
                ),
                child: const Text('Sign out'),
              ),
            ),
          const SizedBox(height: 26),
          Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 320),
              child:
                  SettingsDangerButton('Log out', onPressed: _confirmSignOut),
            ),
          ),
          const SizedBox(height: 8),
          Center(
            child: TextButton(
              onPressed: () => showLicensePage(
                context: context,
                applicationName: 'Kaede Chat',
                applicationVersion: _versionLabel,
                applicationIcon: const Padding(
                  padding: EdgeInsets.symmetric(vertical: 8),
                  child: Icon(Icons.forum_rounded,
                      color: KaedeColors.coral, size: 34),
                ),
              ),
              style: TextButton.styleFrom(
                foregroundColor: KaedeColors.muted,
                textStyle: const TextStyle(fontSize: 12.5),
              ),
              child: const Text('Open-source licences'),
            ),
          ),
          Center(
            child: Text(
              _versionLabel ?? 'Kaede Chat',
              style: const TextStyle(color: KaedeColors.muted, fontSize: 11.5),
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
            ? KaedeColors.coralText
            : KaedeColors.muted.withValues(alpha: .6),
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
        title: const Text('Log out of Kaede?'),
        content: const Text(
          'Saved conversations on this device stay encrypted at rest and are '
          'removed when you sign out.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: const Text('Stay signed in'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: KaedeColors.danger,
            ),
            onPressed: () => Navigator.pop(dialogContext, true),
            child: const Text('Log out'),
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
      await ref.read(mobileControllerProvider.notifier).refreshNavigation();
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
            constraints: const BoxConstraints(maxWidth: 460),
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
                  const SizedBox(height: 12),
                ],
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext),
              child: const Text('Cancel'),
            ),
            FilledButton(
              style: destructive
                  ? FilledButton.styleFrom(backgroundColor: KaedeColors.danger)
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
                    const SnackBar(
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
          title: const Text('Add Kaede to your authenticator'),
          content: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 480),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const Text(
                    'Enter this setup key in your authenticator app, then verify the six-digit code.'),
                const SizedBox(height: 14),
                SelectableText(secret,
                    style: const TextStyle(
                        fontFamily: 'monospace', fontWeight: FontWeight.w800)),
                if (uri.isNotEmpty) ...[
                  const SizedBox(height: 8),
                  ExpansionTile(
                    tilePadding: EdgeInsets.zero,
                    title: const Text('Advanced setup URI'),
                    children: [SelectableText(uri)],
                  ),
                ],
                const SizedBox(height: 12),
                TextField(
                  controller: controller,
                  keyboardType: TextInputType.number,
                  autofillHints: const [AutofillHints.oneTimeCode],
                  decoration:
                      const InputDecoration(labelText: 'Verification code'),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () {
                final code = controller.text.trim();
                if (code.length < 6) {
                  ScaffoldMessenger.of(dialogContext).showSnackBar(
                    const SnackBar(
                      content: Text('Enter the full verification code.'),
                    ),
                  );
                  return;
                }
                Navigator.pop(dialogContext, code);
              },
              child: const Text('Verify and enable'),
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
          title: const Text('Save your recovery codes'),
          content: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 460),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const Text(
                    'Each code can be used once if you lose your authenticator. Kaede will not show them again.'),
                const SizedBox(height: 14),
                SelectableText(codes.join('\\n'),
                    style: const TextStyle(fontFamily: 'monospace')),
              ],
            ),
          ),
          actions: [
            FilledButton(
              onPressed: () => Navigator.pop(dialogContext),
              child: const Text('I saved them'),
            ),
          ],
        ),
      );

  Future<void> _saveSetting(String key, Object? value) async {
    final previous = _settings[key];
    setState(() => _settings[key] = value);
    try {
      final updated = await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .updateSettings(<String, Object?>{key: value});
      ref.read(mobileControllerProvider.notifier).applySettings(updated);
      if (key == 'notification_settings' && value is Map<Object?, Object?>) {
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

  Future<void> _setBiometricLock(bool enabled) async {
    if (enabled) {
      final auth = LocalAuthentication();
      try {
        if (!await auth.isDeviceSupported() ||
            !await auth.authenticate(
              localizedReason: 'Enable device lock for Kaede Chat',
              options: const AuthenticationOptions(
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
          backgroundColor: KaedeColors.danger));
    }
  }
}

/// Muted leading glyph for a flat settings row.
class _LeadingIcon extends StatelessWidget {
  const _LeadingIcon(this.icon, {this.color = KaedeColors.muted});

  final IconData icon;
  final Color color;

  @override
  Widget build(BuildContext context) => Container(
        width: 26,
        height: 26,
        alignment: Alignment.center,
        child: Icon(icon, size: 20, color: color),
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
        child:
            Icon(presenceIcon(status), size: 18, color: presenceColor(status)),
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
  });

  final KaedeUser? user;
  final PresenceStatus presence;
  final VoidCallback? onEditAvatar;
  final VoidCallback? onEditBanner;

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
                    ? const DecoratedBox(
                        decoration: BoxDecoration(
                          gradient: LinearGradient(
                            begin: Alignment.topLeft,
                            end: Alignment.bottomRight,
                            colors: [
                              KaedeColors.coralSoft,
                              KaedeColors.purpleSoft,
                            ],
                          ),
                        ),
                      )
                    : CachedNetworkImage(
                        imageUrl: '$banner',
                        fit: BoxFit.cover,
                        errorWidget: (_, __, ___) =>
                            const ColoredBox(color: KaedeColors.coralSoft),
                      ),
                Positioned(
                  right: 10,
                  top: 10,
                  child: _HeroEditButton(
                    icon: Icons.panorama_rounded,
                    tooltip: 'Change banner',
                    onPressed: onEditBanner,
                  ),
                ),
              ],
            ),
          ),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(4, 0, 4, 4),
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
                        color: kSettingsSurface,
                        shape: const CircleBorder(),
                        child: user == null
                            ? const CircleAvatar(
                                radius: 36,
                                backgroundColor: KaedeColors.raised,
                                child: Icon(Icons.person_rounded,
                                    size: 34, color: KaedeColors.textSoft),
                              )
                            : UserAvatar(
                                user: user!,
                                radius: 36,
                                presence: presence,
                                ringColor: kSettingsSurface,
                              ),
                      ),
                    ),
                    Positioned(
                      right: 0,
                      bottom: 0,
                      child: _HeroEditButton(
                        icon: Icons.photo_camera_rounded,
                        tooltip: 'Change avatar',
                        onPressed: onEditAvatar,
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      user?.name ?? 'Your account',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        fontSize: 22,
                        fontWeight: FontWeight.w800,
                        letterSpacing: -.4,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      user?.handle ?? '',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: KaedeColors.muted,
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
  });

  final IconData icon;
  final String tooltip;
  final VoidCallback? onPressed;

  @override
  Widget build(BuildContext context) => Material(
        color: KaedeColors.canvas.withValues(alpha: .78),
        borderRadius: BorderRadius.circular(16),
        child: InkWell(
          onTap: onPressed,
          borderRadius: BorderRadius.circular(16),
          child: Padding(
            padding: const EdgeInsets.all(6),
            child: Tooltip(
              message: tooltip,
              child: Icon(icon, size: 16, color: KaedeColors.text),
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
