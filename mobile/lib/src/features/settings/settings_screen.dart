import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:image_picker/image_picker.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/features/shared/remote_media.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:local_auth/local_auth.dart';
import 'package:shared_preferences/shared_preferences.dart';

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

  @override
  void initState() {
    super.initState();
    _load();
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
    if (_loading) return const Center(child: CircularProgressIndicator());
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 14, 16, 40),
      children: [
        Text('Your account', style: Theme.of(context).textTheme.headlineLarge),
        const SizedBox(height: 6),
        Text(user?.handle ?? '',
            style: const TextStyle(color: KaedeColors.muted)),
        if (_loadError case final warning?) ...[
          const SizedBox(height: 12),
          Card(
            child: ListTile(
              leading: const Icon(Icons.warning_amber_rounded,
                  color: KaedeColors.warning),
              title: Text(warning),
              trailing: TextButton(
                onPressed: () {
                  setState(() => _loading = true);
                  _load();
                },
                child: const Text('Retry'),
              ),
            ),
          ),
        ],
        const SizedBox(height: 18),
        _Section(
          icon: Icons.person_outline_rounded,
          title: 'Profile',
          subtitle: 'How people see you across the federation.',
          child: Column(
            children: [
              Row(
                children: [
                  if (user != null)
                    UserAvatar(user: user, radius: 36)
                  else
                    const CircleAvatar(radius: 36, child: Text('?')),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        OutlinedButton.icon(
                          onPressed:
                              _saving ? null : () => _pickAsset('avatar'),
                          icon: const Icon(Icons.add_a_photo_outlined),
                          label: const Text('Change avatar'),
                        ),
                        TextButton.icon(
                          onPressed:
                              _saving ? null : () => _pickAsset('banner'),
                          icon: const Icon(Icons.panorama_outlined),
                          label: const Text('Change banner'),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 14),
              TextField(
                  controller: _displayName,
                  decoration: const InputDecoration(labelText: 'Display name')),
              const SizedBox(height: 12),
              TextField(
                  controller: _customStatus,
                  maxLength: 128,
                  decoration:
                      const InputDecoration(labelText: 'Custom status')),
              const SizedBox(height: 4),
              TextField(
                controller: _bio,
                minLines: 3,
                maxLines: 6,
                maxLength: 500,
                decoration: const InputDecoration(labelText: 'About me'),
              ),
              const SizedBox(height: 10),
              Align(
                alignment: Alignment.centerRight,
                child: FilledButton.icon(
                  onPressed: _saving ? null : _saveProfile,
                  icon: _saving
                      ? const SizedBox.square(
                          dimension: 16,
                          child: CircularProgressIndicator(strokeWidth: 2))
                      : const Icon(Icons.check_rounded),
                  label: const Text('Save profile'),
                ),
              ),
            ],
          ),
        ),
        _Section(
          icon: Icons.security_rounded,
          title: 'Account security',
          subtitle: 'Protect the account hosted by your home instance.',
          child: Column(
            children: [
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.alternate_email_rounded),
                title: Text(user?.email ?? 'No email address'),
                subtitle: Text(user?.email == null
                    ? 'This instance does not require email.'
                    : user!.emailVerified
                        ? 'Verified email'
                        : 'Email verification is pending'),
                trailing: user?.email == null
                    ? null
                    : Icon(
                        user!.emailVerified
                            ? Icons.verified_rounded
                            : Icons.warning_amber_rounded,
                        color: user.emailVerified
                            ? KaedeColors.mint
                            : KaedeColors.coral,
                      ),
              ),
              Row(
                children: [
                  Expanded(
                    child: OutlinedButton.icon(
                      onPressed: _saving ? null : _changeEmail,
                      icon: const Icon(Icons.edit_outlined),
                      label: const Text('Change email'),
                    ),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: OutlinedButton.icon(
                      onPressed: _saving ? null : _confirmEmail,
                      icon: const Icon(Icons.mark_email_read_outlined),
                      label: const Text('Confirm token'),
                    ),
                  ),
                ],
              ),
              const Divider(height: 30),
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.password_rounded),
                title: const Text('Authenticator app'),
                subtitle: Text(user?.mfaEnabled == true
                    ? 'Two-factor authentication is enabled.'
                    : 'Require a code in addition to your password.'),
                trailing: user?.mfaEnabled == true
                    ? const Icon(Icons.verified_user_rounded,
                        color: KaedeColors.mint)
                    : null,
              ),
              SizedBox(
                width: double.infinity,
                child: user?.mfaEnabled == true
                    ? OutlinedButton.icon(
                        onPressed: _saving ? null : _disableMfa,
                        icon: const Icon(Icons.no_encryption_outlined),
                        label: const Text('Disable two-factor authentication'),
                      )
                    : FilledButton.tonalIcon(
                        onPressed: _saving ? null : _enableMfa,
                        icon: const Icon(Icons.enhanced_encryption_outlined),
                        label: const Text('Set up two-factor authentication'),
                      ),
              ),
            ],
          ),
        ),
        _Section(
          icon: Icons.adjust_rounded,
          title: 'Presence',
          subtitle:
              'Your availability follows you between mobile, desktop, and web.',
          child: SegmentedButton<String>(
            segments: const [
              ButtonSegment(
                  value: 'online',
                  label: Text('Online'),
                  icon: Icon(Icons.circle, color: KaedeColors.mint, size: 12)),
              ButtonSegment(
                  value: 'idle',
                  label: Text('Idle'),
                  icon: Icon(Icons.bedtime, size: 16)),
              ButtonSegment(
                  value: 'dnd',
                  label: Text('DND'),
                  icon: Icon(Icons.do_not_disturb_on,
                      color: KaedeColors.danger, size: 16)),
              ButtonSegment(
                  value: 'invisible',
                  label: Text('Invisible'),
                  icon: Icon(Icons.circle_outlined, size: 14)),
            ],
            selected: <String>{
              '${_settings['presence_preference'] ?? 'online'}'
            },
            onSelectionChanged: (value) =>
                _saveSetting('presence_preference', value.first),
          ),
        ),
        _Section(
          icon: Icons.notifications_outlined,
          title: 'Notifications',
          subtitle:
              'System notification categories can also be changed in your phone settings.',
          child: Column(
            children: [
              Text(
                ref.read(mobileControllerProvider.notifier).usesPushRelay
                    ? 'If enabled, your account on ${mobile.user?.ref.domain.value ?? 'your home instance'} '
                        'uses Kaede Push Relay (${ref.read(mobileControllerProvider.notifier).pushRelayHost}) for '
                        'closed-app delivery. Your home sends a signed, content-free '
                        'wake. The relay sees your home instance, an opaque device '
                        'subscription, delivery timing, and provider results; it does '
                        'not receive message text, sender names, room identifiers, '
                        'attachments, or encryption keys.'
                    : 'This community build uses its own Firebase provider for '
                        'closed-app delivery. If enabled, your home stores this '
                        'installation’s provider token and sends only an opaque '
                        'wake to Firebase. The wake contains no message text, sender '
                        'name, room identifier, attachment, or encryption key.',
                style: const TextStyle(color: KaedeColors.muted),
              ),
              const SizedBox(height: 12),
              SizedBox(
                width: double.infinity,
                child: FilledButton.icon(
                  onPressed: () async {
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
                  icon: const Icon(Icons.notifications_active_outlined),
                  label: const Text('Enable background notifications'),
                ),
              ),
              const SizedBox(height: 8),
              SizedBox(
                width: double.infinity,
                child: OutlinedButton.icon(
                  onPressed: () async {
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
                  icon: const Icon(Icons.notifications_off_outlined),
                  label: const Text('Disable background notifications'),
                ),
              ),
              if (mobile.pushWarning case final warning?)
                Padding(
                  padding: const EdgeInsets.only(top: 10),
                  child: DecoratedBox(
                    decoration: BoxDecoration(
                      color: KaedeColors.warning.withValues(alpha: 0.12),
                      borderRadius: BorderRadius.circular(12),
                      border: Border.all(
                        color: KaedeColors.warning.withValues(alpha: 0.45),
                      ),
                    ),
                    child: ListTile(
                      dense: true,
                      leading: const Icon(
                        Icons.warning_amber_rounded,
                        color: KaedeColors.warning,
                      ),
                      title:
                          const Text('Notification delivery needs attention'),
                      subtitle: Text(warning),
                    ),
                  ),
                ),
              if (!ref
                  .read(mobileControllerProvider.notifier)
                  .remotePushAvailable)
                const Padding(
                  padding: EdgeInsets.only(top: 10),
                  child: Text(
                    'This build can show alerts while Kaede is running, but '
                    'it has no compatible closed-app push provider.',
                    style: TextStyle(color: KaedeColors.muted),
                  ),
                ),
              const SizedBox(height: 8),
              _PreferenceSwitch(
                title: 'Direct messages',
                value: _notification('direct_messages', true),
                onChanged: (value) =>
                    _saveNotification('direct_messages', value),
              ),
              _PreferenceSwitch(
                title: 'Mentions and replies',
                value: _notification('mentions', true),
                onChanged: (value) => _saveNotification('mentions', value),
              ),
              _PreferenceSwitch(
                title: 'Friend requests',
                value: _notification('relationships', true),
                onChanged: (value) => _saveNotification('relationships', value),
              ),
              _PreferenceSwitch(
                title: 'Show message previews',
                subtitle:
                    'Enabled by default. Show the sender, message text, and profile picture. Kaede fetches these directly after the private push wake-up; FCM never receives them. Your lock-screen privacy settings still apply.',
                value: _notification('show_notification_previews', true),
                onChanged: (value) =>
                    _saveNotification('show_notification_previews', value),
              ),
              const Padding(
                padding: EdgeInsets.only(top: 8),
                child: Text(
                    'Do Not Disturb suppresses banners and sounds on every signed-in client.',
                    style: TextStyle(color: KaedeColors.muted)),
              ),
            ],
          ),
        ),
        _Section(
          icon: Icons.lock_outline_rounded,
          title: 'Privacy and app lock',
          child: Column(
            children: [
              DropdownButtonFormField<String>(
                initialValue: '${_settings['dm_privacy'] ?? 'friends'}',
                decoration:
                    const InputDecoration(labelText: 'Who can message you'),
                items: const [
                  DropdownMenuItem(value: 'everyone', child: Text('Everyone')),
                  DropdownMenuItem(
                      value: 'friends', child: Text('Friends only')),
                  DropdownMenuItem(
                      value: 'shared_guild',
                      child: Text('Friends and shared guilds')),
                ],
                onChanged: (value) {
                  if (value != null) _saveSetting('dm_privacy', value);
                },
              ),
              const SizedBox(height: 10),
              SwitchListTile.adaptive(
                contentPadding: EdgeInsets.zero,
                title: const Text('Lock Kaede when you leave'),
                subtitle: const Text(
                    'Unlock with biometrics, your device passcode, or your device PIN.'),
                value: _biometricLock,
                onChanged: _setBiometricLock,
              ),
              if (_biometricLock) ...[
                const SizedBox(height: 8),
                DropdownButtonFormField<int>(
                  initialValue: _biometricLockTimeout,
                  decoration:
                      const InputDecoration(labelText: 'Lock after leaving'),
                  items: const [
                    DropdownMenuItem(value: 0, child: Text('Immediately')),
                    DropdownMenuItem(value: 15, child: Text('15 seconds')),
                    DropdownMenuItem(value: 30, child: Text('30 seconds')),
                    DropdownMenuItem(value: 60, child: Text('1 minute')),
                    DropdownMenuItem(value: 300, child: Text('5 minutes')),
                  ],
                  onChanged: _setBiometricLockTimeout,
                ),
              ],
            ],
          ),
        ),
        _Section(
          icon: Icons.devices_rounded,
          title: 'Signed-in devices',
          subtitle: 'Revoke any session you do not recognize.',
          child: Column(
            children: [
              if (_sessions.isEmpty)
                const ListTile(
                    title: Text('This device'),
                    subtitle: Text('Current session')),
              for (final session in _sessions)
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  leading: Icon(_deviceIcon('${session['device_name'] ?? ''}')),
                  title: Text('${session['device_name'] ?? 'Kaede client'}'),
                  subtitle: Text(_sessionSubtitle(session)),
                  trailing: IconButton(
                    tooltip: 'Sign out device',
                    icon: const Icon(Icons.logout_rounded),
                    onPressed: () => _revoke('${session['id']}'),
                  ),
                ),
            ],
          ),
        ),
        const SizedBox(height: 10),
        OutlinedButton.icon(
          onPressed: () => ref.read(mobileControllerProvider.notifier).logout(),
          icon: const Icon(Icons.logout_rounded, color: KaedeColors.danger),
          label: const Text('Sign out',
              style: TextStyle(color: KaedeColors.danger)),
        ),
      ],
    );
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
                final values = controllers.map(
                  (key, controller) => MapEntry(key, controller.text.trim()),
                );
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
                SelectableText(codes.join('\n'),
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

  Future<void> _setBiometricLockTimeout(int? seconds) async {
    if (seconds == null) return;
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

final class _Section extends StatelessWidget {
  const _Section(
      {required this.icon,
      required this.title,
      required this.child,
      this.subtitle});
  final IconData icon;
  final String title;
  final String? subtitle;
  final Widget child;

  @override
  Widget build(BuildContext context) => Card(
        margin: const EdgeInsets.only(bottom: 10),
        clipBehavior: Clip.antiAlias,
        child: ExpansionTile(
          leading: Icon(icon, color: KaedeColors.coral),
          title:
              Text(title, style: const TextStyle(fontWeight: FontWeight.w800)),
          subtitle: subtitle == null
              ? null
              : Text(subtitle!,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style:
                      const TextStyle(color: KaedeColors.muted, fontSize: 12)),
          childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
          expandedCrossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Padding(
              padding: const EdgeInsets.only(top: 14),
              child: child,
            ),
          ],
        ),
      );
}

final class _PreferenceSwitch extends StatelessWidget {
  const _PreferenceSwitch({
    required this.title,
    required this.value,
    required this.onChanged,
    this.subtitle,
  });
  final String title;
  final String? subtitle;
  final bool value;
  final ValueChanged<bool> onChanged;

  @override
  Widget build(BuildContext context) => SwitchListTile.adaptive(
        contentPadding: EdgeInsets.zero,
        title: Text(title),
        subtitle: subtitle == null ? null : Text(subtitle!),
        value: value,
        onChanged: onChanged,
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
