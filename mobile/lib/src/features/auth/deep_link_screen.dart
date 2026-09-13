import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/app/providers.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/application_installations.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/permission_selection.dart';
import 'package:kaede_mobile/src/e2ee/store.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';
import 'package:kaede_mobile/src/platform/push_service.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

enum MobileLinkKind {
  invite,
  verifyEmail,
  resetPassword,
  emailChange,
  message,
  applicationInstall,
}

final class MobileDeepLink {
  MobileDeepLink({
    required this.kind,
    required this.instance,
    this.token,
    this.code,
    this.destination,
    this.application,
    this.templateSlug,
  });

  final MobileLinkKind kind;
  final Domain instance;
  final String? token;
  final String? code;
  final PushDestination? destination;
  final EntityRef? application;
  final String? templateSlug;

  bool get requiresSession =>
      kind == MobileLinkKind.invite ||
      kind == MobileLinkKind.emailChange ||
      kind == MobileLinkKind.message ||
      kind == MobileLinkKind.applicationInstall;

  String get signInNotice => switch (kind) {
        MobileLinkKind.invite => 'Sign in to review and accept this invite.',
        MobileLinkKind.emailChange => 'Sign in to confirm your email change.',
        MobileLinkKind.message => 'Sign in to open the linked message.',
        MobileLinkKind.applicationInstall =>
          'Sign in to review this application invitation.',
        _ => 'Sign in to continue.',
      };

  static MobileDeepLink? parse(Uri uri) {
    try {
      final instanceText = uri.scheme == 'https' || uri.scheme == 'http'
          ? uri.host
          : uri.queryParameters['instance'];
      const configuredHost = String.fromEnvironment(
        'KAEDE_APP_LINK_HOST',
        defaultValue: 'kaede.chat',
      );
      final instance = Domain(
        instanceText?.isNotEmpty == true ? instanceText! : configuredHost,
      );
      final parts = uri.pathSegments;
      final fragment = uri.fragment.isEmpty
          ? const <String, String>{}
          : Uri.splitQueryString(uri.fragment);
      final token = uri.queryParameters['token'] ?? fragment['token'];
      if (parts.length == 2 && parts.first == 'invite') {
        return MobileDeepLink(
            kind: MobileLinkKind.invite, instance: instance, code: parts[1]);
      }
      if (parts.length == 4 &&
          parts.first == 'applications' &&
          parts[2] == 'install' &&
          parts[3].isNotEmpty) {
        return MobileDeepLink(
          kind: MobileLinkKind.applicationInstall,
          instance: instance,
          application: EntityRef.parse(parts[1]),
          templateSlug: parts[3],
        );
      }
      if (parts.length == 1 &&
          (parts.first == 'verify' || parts.first == 'verify-email') &&
          token?.isNotEmpty == true) {
        return MobileDeepLink(
            kind: MobileLinkKind.verifyEmail, instance: instance, token: token);
      }
      if (parts.length == 1 &&
          parts.first == 'reset-password' &&
          token?.isNotEmpty == true) {
        return MobileDeepLink(
            kind: MobileLinkKind.resetPassword,
            instance: instance,
            token: token);
      }
      if (parts.length == 1 &&
          parts.first == 'verify-email-change' &&
          token?.isNotEmpty == true) {
        return MobileDeepLink(
            kind: MobileLinkKind.emailChange, instance: instance, token: token);
      }
      final around = uri.queryParameters['around'];
      if (parts.length == 3 && parts.first == 'g') {
        final channel = EntityRef.parse(parts[2]);
        return MobileDeepLink(
          kind: MobileLinkKind.message,
          instance: instance,
          destination: PushDestination(
            channel: channel,
            message:
                around?.isNotEmpty == true ? EntityRef.parse(around!) : null,
          ),
        );
      }
      if (parts.length == 2 && parts.first == 'home') {
        final channel = EntityRef.parse(parts[1]);
        return MobileDeepLink(
          kind: MobileLinkKind.message,
          instance: instance,
          destination: PushDestination(
            channel: channel,
            message:
                around?.isNotEmpty == true ? EntityRef.parse(around!) : null,
          ),
        );
      }
    } on Object {
      return null;
    }
    return null;
  }
}

final class DeepLinkActionScreen extends ConsumerStatefulWidget {
  const DeepLinkActionScreen({super.key, required this.link});
  final MobileDeepLink link;

  @override
  ConsumerState<DeepLinkActionScreen> createState() =>
      _DeepLinkActionScreenState();
}

final class _DeepLinkActionScreenState
    extends ConsumerState<DeepLinkActionScreen> {
  final _password = TextEditingController();
  final _confirmation = TextEditingController();
  var _running = false;
  Map<String, Object?>? _invitePreview;
  String? _success;
  Object? _error;

  @override
  void initState() {
    super.initState();
    if (widget.link.kind == MobileLinkKind.invite) {
      WidgetsBinding.instance
          .addPostFrameCallback((_) => unawaited(_loadInvitePreview()));
    } else if (widget.link.kind != MobileLinkKind.resetPassword) {
      WidgetsBinding.instance.addPostFrameCallback((_) => unawaited(_run()));
    }
  }

  @override
  void dispose() {
    _password.dispose();
    _confirmation.dispose();
    super.dispose();
  }

  String _inviteCode(MobileController controller) => qualifiedInviteCode(
        widget.link,
        controller.api.tokens?.instance,
      );

  Future<void> _loadInvitePreview() async {
    if (_running || widget.link.kind != MobileLinkKind.invite) return;
    setState(() {
      _running = true;
      _error = null;
      _invitePreview = null;
    });
    try {
      final repository = ref.read(repositoryProvider);
      final controller = ref.read(mobileControllerProvider.notifier);
      final preview = await repository.previewInvite(_inviteCode(controller));
      final guild = preview['guild'];
      if (guild is! Map ||
          guild['name'] is! String ||
          guild['origin_domain'] is! String) {
        throw FormatException('Invalid invite preview');
      }
      if (!mounted) return;
      setState(
          () => _invitePreview = Map<String, Object?>.unmodifiable(preview));
    } on Object catch (error) {
      if (mounted) setState(() => _error = error);
    } finally {
      if (mounted) setState(() => _running = false);
    }
  }

  Future<void> _run() async {
    if (_running) return;
    if (widget.link.kind == MobileLinkKind.resetPassword) {
      if (_password.text.length < 10) {
        setState(() => _error = UserInputException(L10n.of(context)
            .ui_use_a_password_with_at_least_10_characters_e92a08df));
        return;
      }
      if (_password.text != _confirmation.text) {
        setState(() => _error = UserInputException(
            L10n.of(context).ui_passwords_do_not_match_fd207588));
        return;
      }
    }
    setState(() {
      _running = true;
      _error = null;
    });
    try {
      final repository = ref.read(repositoryProvider);
      final controller = ref.read(mobileControllerProvider.notifier);
      switch (widget.link.kind) {
        case MobileLinkKind.invite:
          if (_invitePreview == null) {
            throw UserInputException('Review the invitation before accepting.');
          }
          await repository.acceptInvite(_inviteCode(controller));
          await controller.refreshNavigation();
          _success = 'Invite accepted.';
          break;
        case MobileLinkKind.verifyEmail:
          final restoreInstance = controller.api.tokens?.instance;
          repository.api.selectInstance(widget.link.instance);
          try {
            await repository.verifyEmail(widget.link.token!);
          } finally {
            if (restoreInstance != null) {
              repository.api.selectInstance(restoreInstance);
            }
          }
          _success = 'Email address verified. You can sign in now.';
          break;
        case MobileLinkKind.resetPassword:
          final accountRef = await repository.resetPassword(
            widget.link.instance,
            widget.link.token!,
            _password.text,
          );
          await MobileE2EEStore().rebaseAfterPasswordReset(accountRef);
          _success = 'Password updated. Sign in with your new password.';
          break;
        case MobileLinkKind.emailChange:
          _requireMatchingSession(controller);
          await repository.confirmEmailChange(widget.link.token!);
          _success = 'Email change confirmed.';
          break;
        case MobileLinkKind.message:
          _requireMatchingSession(controller);
          final opened =
              await controller.openPushDestination(widget.link.destination!);
          if (!opened) {
            throw UserInputException('That message is unavailable.');
          }
          if (mounted) context.go('/');
          return;
        case MobileLinkKind.applicationInstall:
          // Installation links use the dedicated review screen below and are
          // never authorized as a side effect of opening the URL.
          return;
      }
    } on Object catch (error) {
      _error = error;
    } finally {
      if (mounted) setState(() => _running = false);
    }
  }

  void _requireMatchingSession(MobileController controller) {
    if (controller.api.tokens?.instance != widget.link.instance) {
      throw UserInputException(
        'This link belongs to ${widget.link.instance.value}. Sign in to that home instance first.',
      );
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: Text(_title)),
        body: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              padding: EdgeInsets.all(24),
              child: ConstrainedBox(
                constraints: BoxConstraints(maxWidth: 440),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Icon(_icon, size: 48, color: context.kaede.coral),
                    SizedBox(height: 18),
                    Text(_title,
                        textAlign: TextAlign.center,
                        style: Theme.of(context).textTheme.headlineMedium),
                    SizedBox(height: 8),
                    Text(widget.link.instance.value,
                        textAlign: TextAlign.center,
                        style: TextStyle(color: context.kaede.muted)),
                    if (widget.link.kind == MobileLinkKind.invite &&
                        _invitePreview != null) ...[
                      SizedBox(height: 18),
                      _InviteReview(preview: _invitePreview!),
                    ],
                    if (widget.link.kind == MobileLinkKind.resetPassword &&
                        _success == null) ...[
                      SizedBox(height: 22),
                      TextField(
                        controller: _password,
                        obscureText: true,
                        decoration: InputDecoration(
                            labelText:
                                L10n.of(context).ui_new_password_6a3aaab2),
                      ),
                      SizedBox(height: 12),
                      TextField(
                        controller: _confirmation,
                        obscureText: true,
                        decoration: InputDecoration(
                            labelText:
                                L10n.of(context).ui_confirm_password_5ad9dc20),
                      ),
                    ],
                    if (_error case final error?) ...[
                      SizedBox(height: 16),
                      Text(userFacingError(error),
                          textAlign: TextAlign.center,
                          style: TextStyle(color: context.kaede.danger)),
                    ],
                    if (_success case final success?) ...[
                      SizedBox(height: 16),
                      Text(success,
                          textAlign: TextAlign.center,
                          style: TextStyle(color: context.kaede.mint)),
                    ],
                    SizedBox(height: 22),
                    if (_success != null)
                      FilledButton(
                        onPressed: () => context.go('/'),
                        child: Text(L10n.of(context).ui_continue_ab43d664),
                      )
                    else
                      FilledButton(
                        onPressed: _running
                            ? null
                            : widget.link.kind == MobileLinkKind.invite &&
                                    _invitePreview == null
                                ? _loadInvitePreview
                                : _run,
                        child: _running
                            ? SizedBox.square(
                                dimension: 18,
                                child:
                                    CircularProgressIndicator(strokeWidth: 2))
                            : Text(widget.link.kind ==
                                    MobileLinkKind.resetPassword
                                ? L10n.of(context).ui_reset_password_679a51e9
                                : widget.link.kind == MobileLinkKind.invite &&
                                        _invitePreview != null
                                    ? L10n.of(context)
                                        .ui_accept_invitation_968c52bc
                                    : L10n.of(context).ui_try_again_213e90fa),
                      ),
                  ],
                ),
              ),
            ),
          ),
        ),
      );

  String get _title => switch (widget.link.kind) {
        MobileLinkKind.invite => 'Guild invitation',
        MobileLinkKind.verifyEmail => 'Verify email',
        MobileLinkKind.resetPassword => 'Reset password',
        MobileLinkKind.emailChange => 'Confirm email change',
        MobileLinkKind.message => 'Opening message',
        MobileLinkKind.applicationInstall => 'Application invitation',
      };

  IconData get _icon => switch (widget.link.kind) {
        MobileLinkKind.invite => Icons.group_add_outlined,
        MobileLinkKind.verifyEmail ||
        MobileLinkKind.emailChange =>
          Icons.mark_email_read_outlined,
        MobileLinkKind.resetPassword => Icons.password_rounded,
        MobileLinkKind.message => Icons.chat_bubble_outline_rounded,
        MobileLinkKind.applicationInstall => Icons.apps_rounded,
      };
}

String qualifiedInviteCode(MobileDeepLink link, Domain? home) {
  final code = link.code;
  if (link.kind != MobileLinkKind.invite || code == null || code.isEmpty) {
    throw ArgumentError(
        L10n.current.ui_a_guild_invite_link_with_a_code_is_required_588ca9dd);
  }
  if (code.contains('@') || home == null || home == link.instance) return code;
  return '$code@${link.instance.value}';
}

List<String> invitePreviewDetails(Map<String, Object?> preview) {
  final details = <String>[];
  final uses = preview['uses'];
  final maximum = preview['max_uses'];
  if (uses is num && maximum is num && maximum > 0) {
    details.add('${maximum.toInt() - uses.toInt()} uses remain');
  }
  final roles = preview['role_ids'];
  if (roles is List && roles.isNotEmpty) {
    details.add('Grants ${roles.length} role${roles.length == 1 ? '' : 's'}');
  }
  final targetCount = preview['target_user_count'];
  if (targetCount is num && targetCount > 0) {
    details.add('Limited invitation');
  }
  final targetType = preview['target_type'];
  if (targetType == 'stream') details.add('Opens a Go Live stream');
  final event = preview['guild_scheduled_event'];
  if (event is Map && event['name'] is String) {
    details.add('Event: ${event['name']}');
  }
  return List<String>.unmodifiable(details);
}

final class _InviteReview extends StatelessWidget {
  const _InviteReview({required this.preview});

  final Map<String, Object?> preview;

  @override
  Widget build(BuildContext context) {
    final rawGuild = preview['guild'];
    final guild = rawGuild is Map ? rawGuild : const <String, Object?>{};
    final name = guild['name'] is String ? guild['name']! as String : 'Guild';
    final domain = guild['origin_domain'] is String
        ? guild['origin_domain']! as String
        : '';
    final description =
        guild['description'] is String ? guild['description']! as String : null;
    final details = invitePreviewDetails(preview);
    return DecoratedBox(
      decoration: BoxDecoration(
        color: context.kaede.canvas,
        border: Border.all(color: context.kaede.border),
        borderRadius: BorderRadius.circular(14),
      ),
      child: Padding(
        padding: EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(name, style: Theme.of(context).textTheme.titleLarge),
            if (domain.isNotEmpty) ...[
              SizedBox(height: 3),
              Text(domain, style: TextStyle(color: context.kaede.muted)),
            ],
            if (description?.isNotEmpty == true) ...[
              SizedBox(height: 10),
              Text(description!),
            ],
            for (final detail in details) ...[
              SizedBox(height: 8),
              Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(Icons.check_circle_outline_rounded,
                      size: 17, color: context.kaede.mint),
                  SizedBox(width: 8),
                  Expanded(child: Text(detail)),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }
}

final class ApplicationInstallDeepLinkScreen extends ConsumerStatefulWidget {
  const ApplicationInstallDeepLinkScreen({super.key, required this.link});

  final MobileDeepLink link;

  @override
  ConsumerState<ApplicationInstallDeepLinkScreen> createState() =>
      _ApplicationInstallDeepLinkScreenState();
}

final class _ApplicationInstallDeepLinkScreenState
    extends ConsumerState<ApplicationInstallDeepLinkScreen> {
  ApplicationInstallInvite? _invite;
  String? _error;
  var _loading = true;
  var _saving = false;
  var _guildSaving = false;
  var _installed = false;
  var _guildInstalled = false;
  var _guilds = true;
  var _privateChannels = true;
  var _botDms = true;
  List<KaedeGuild> _availableGuilds = const <KaedeGuild>[];
  String? _selectedGuild;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final repository = ref.read(repositoryProvider);
      final invite = await repository.resolveApplicationInstallInvite(
        widget.link.application!,
        widget.link.templateSlug!,
      );
      if (invite.application != widget.link.application ||
          invite.templateSlug != widget.link.templateSlug) {
        throw FormatException(
          'The server returned a different application invitation.',
        );
      }
      final availableGuilds = invite.supportsGuildInstall
          ? (await repository.guilds())
              .where((guild) =>
                  guild.allows(Permission.manageGuild) ||
                  guild.allows(Permission.administrator))
              .toList(growable: false)
          : const <KaedeGuild>[];
      if (mounted) {
        setState(() {
          _invite = invite;
          _guilds = invite.userInstallContexts.contains('guild');
          _privateChannels =
              invite.userInstallContexts.contains('private_channel');
          _botDms = invite.userInstallContexts.contains('bot_dm');
          _availableGuilds = availableGuilds;
          _selectedGuild = availableGuilds.firstOrNull?.ref.wire;
        });
      }
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(
              error,
              summary: L10n.of(context)
                  .ui_this_application_invitation_is_unavailable_6876cc30,
            ));
      }
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _authorize() async {
    final invite = _invite;
    if (_saving || invite == null || !invite.supportsUserInstall) return;
    final contexts = <String>[
      if (_guilds) 'guild',
      if (_privateChannels) 'private_channel',
      if (_botDms) 'bot_dm',
    ];
    if (contexts.isEmpty) {
      setState(() => _error =
          L10n.of(context).ui_choose_at_least_one_place_for_this_app_cc13afa3);
      return;
    }
    setState(() {
      _saving = true;
      _error = null;
    });
    try {
      await ref.read(repositoryProvider).installUserApplication(
            invite.application,
            scopes: invite.userInstallScopes,
            contexts: contexts,
          );
      if (mounted) setState(() => _installed = true);
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(
              error,
              summary: L10n.of(context)
                  .ui_could_not_authorize_this_app_for_your_account_aaa20c98,
            ));
      }
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _installInGuild() async {
    final invite = _invite;
    final selected = _availableGuilds
        .where((guild) => guild.ref.wire == _selectedGuild)
        .firstOrNull;
    if (_guildSaving || invite == null || selected == null) return;
    setState(() {
      _guildSaving = true;
      _error = null;
    });
    try {
      await ref.read(repositoryProvider).installGuildApplication(
            selected.ref,
            invite.application,
            invite.templateSlug,
          );
      if (mounted) setState(() => _guildInstalled = true);
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(
              error,
              summary: L10n.of(context)
                  .ui_could_not_add_this_app_to_the_server_e6821d4e,
            ));
      }
    } finally {
      if (mounted) setState(() => _guildSaving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final invite = _invite;
    return Scaffold(
      appBar: AppBar(title: Text(L10n.of(context).ui_add_app_57813d5f)),
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: EdgeInsets.all(20),
            child: ConstrainedBox(
              constraints: BoxConstraints(maxWidth: 560),
              child: _loading
                  ? Center(child: CircularProgressIndicator())
                  : invite == null
                      ? _ApplicationInviteError(
                          error: _error ?? 'This invitation is unavailable.',
                          onRetry: _load,
                        )
                      : Column(
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: [
                            _ApplicationInviteHeader(invite: invite),
                            SizedBox(height: 16),
                            Text(
                              invite.applicationDescription ??
                                  invite.templateDescription ??
                                  'This app has not provided a description.',
                              style: TextStyle(
                                  color: context.kaede.textSoft, height: 1.45),
                            ),
                            SizedBox(height: 20),
                            if (invite.supportsGuildInstall) ...[
                              Text(L10n.of(context).ui_add_to_a_server_eca6de19,
                                  style: Theme.of(context)
                                      .textTheme
                                      .titleMedium
                                      ?.copyWith(fontWeight: FontWeight.w800)),
                              SizedBox(height: 6),
                              if (_guildInstalled)
                                const _GuildApplicationInstalledNotice()
                              else ...[
                                DropdownButtonFormField<String>(
                                  initialValue: _selectedGuild,
                                  decoration: InputDecoration(
                                    labelText:
                                        L10n.of(context).ui_server_6d7302f2,
                                  ),
                                  items: [
                                    for (final guild in _availableGuilds)
                                      DropdownMenuItem<String>(
                                        value: guild.ref.wire,
                                        child: Text(guild.name),
                                      ),
                                  ],
                                  onChanged: _guildSaving
                                      ? null
                                      : (value) => setState(
                                          () => _selectedGuild = value),
                                ),
                                if (_availableGuilds.isEmpty)
                                  Padding(
                                    padding: EdgeInsets.only(top: 8),
                                    child: Text(
                                      L10n.of(context)
                                          .ui_you_need_manage_server_in_a_server_to_add_thi_42ac4a66,
                                      style:
                                          TextStyle(color: context.kaede.muted),
                                    ),
                                  ),
                                SizedBox(height: 10),
                                FilledButton.icon(
                                  onPressed:
                                      _guildSaving || _selectedGuild == null
                                          ? null
                                          : _installInGuild,
                                  icon: _guildSaving
                                      ? SizedBox.square(
                                          dimension: 17,
                                          child: CircularProgressIndicator(
                                              strokeWidth: 2),
                                        )
                                      : Icon(Icons.add_to_photos_outlined),
                                  label: Text(_guildSaving
                                      ? L10n.of(context).ui_adding_2060c750
                                      : L10n.of(context)
                                          .ui_authorize_and_add_app_c6da0429),
                                ),
                              ],
                              SizedBox(height: 20),
                            ],
                            if (invite.supportsUserInstall && _installed)
                              _ApplicationInstalledNotice(invite: invite)
                            else if (invite.supportsUserInstall) ...[
                              Text(
                                  L10n.of(context)
                                      .ui_install_for_your_account_d367e7f9,
                                  style: Theme.of(context)
                                      .textTheme
                                      .titleMedium
                                      ?.copyWith(fontWeight: FontWeight.w800)),
                              SizedBox(height: 6),
                              Text(
                                L10n.of(context)
                                    .ui_the_app_receives_only_command_interactions_yo_f341864d,
                                style: TextStyle(color: context.kaede.muted),
                              ),
                              if (invite.userInstallContexts.contains('guild'))
                                CheckboxListTile(
                                  contentPadding: EdgeInsets.zero,
                                  value: _guilds,
                                  title: Text(L10n.of(context)
                                      .ui_guild_channels_fcb4d8c8),
                                  onChanged: _saving
                                      ? null
                                      : (value) => setState(
                                          () => _guilds = value ?? false),
                                ),
                              if (invite.userInstallContexts
                                  .contains('private_channel'))
                                CheckboxListTile(
                                  contentPadding: EdgeInsets.zero,
                                  value: _privateChannels,
                                  title: Text(L10n.of(context)
                                      .ui_private_conversations_0de4d400),
                                  onChanged: _saving
                                      ? null
                                      : (value) => setState(() =>
                                          _privateChannels = value ?? false),
                                ),
                              if (invite.userInstallContexts.contains('bot_dm'))
                                CheckboxListTile(
                                  contentPadding: EdgeInsets.zero,
                                  value: _botDms,
                                  title: Text(L10n.of(context)
                                      .ui_direct_messages_with_bots_f733fcd2),
                                  onChanged: _saving
                                      ? null
                                      : (value) => setState(
                                          () => _botDms = value ?? false),
                                ),
                              FilledButton.icon(
                                key: ValueKey('authorize-personal-application'),
                                onPressed: _saving ? null : _authorize,
                                icon: _saving
                                    ? SizedBox.square(
                                        dimension: 17,
                                        child: CircularProgressIndicator(
                                            strokeWidth: 2),
                                      )
                                    : Icon(Icons.verified_user_outlined),
                                label: Text(_saving
                                    ? L10n.of(context).ui_authorizing_14b71d35
                                    : L10n.of(context)
                                        .ui_authorize_for_my_account_b07cd520),
                              ),
                            ],
                            SizedBox(height: 20),
                            _ApplicationInviteAccess(invite: invite),
                            if (_error case final error?) ...[
                              SizedBox(height: 14),
                              Semantics(
                                liveRegion: true,
                                child: Text(error,
                                    style:
                                        TextStyle(color: context.kaede.danger)),
                              ),
                            ],
                            SizedBox(height: 18),
                            OutlinedButton(
                              onPressed: () => context.go('/'),
                              child: Text(
                                  L10n.of(context).ui_back_to_kaede_e4c68b03),
                            ),
                          ],
                        ),
            ),
          ),
        ),
      ),
    );
  }
}

final class _ApplicationInviteError extends StatelessWidget {
  const _ApplicationInviteError({required this.error, required this.onRetry});

  final String error;
  final Future<void> Function() onRetry;

  @override
  Widget build(BuildContext context) => Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(Icons.link_off_rounded, size: 46, color: context.kaede.coral),
          SizedBox(height: 14),
          Text(error, textAlign: TextAlign.center),
          SizedBox(height: 16),
          FilledButton(
            onPressed: () => unawaited(onRetry()),
            child: Text(L10n.of(context).ui_try_again_213e90fa),
          ),
        ],
      );
}

final class _ApplicationInviteHeader extends StatelessWidget {
  const _ApplicationInviteHeader({required this.invite});

  final ApplicationInstallInvite invite;

  @override
  Widget build(BuildContext context) => Row(
        children: [
          CircleAvatar(
            radius: 28,
            backgroundColor: context.kaede.coral.withValues(alpha: .16),
            child: Text(
              invite.applicationName.characters.first.toUpperCase(),
              style: TextStyle(
                color: context.kaede.coral,
                fontWeight: FontWeight.w900,
                fontSize: 22,
              ),
            ),
          ),
          SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(L10n.of(context).ui_app_authorization_748f85d5,
                    style: TextStyle(
                      color: context.kaede.muted,
                      fontSize: 10,
                      fontWeight: FontWeight.w800,
                    )),
                Text(invite.applicationName,
                    style: Theme.of(context)
                        .textTheme
                        .headlineSmall
                        ?.copyWith(fontWeight: FontWeight.w900)),
                Text(invite.botHandle,
                    style: TextStyle(color: context.kaede.muted)),
              ],
            ),
          ),
        ],
      );
}

final class _ApplicationInstalledNotice extends StatelessWidget {
  const _ApplicationInstalledNotice({required this.invite});

  final ApplicationInstallInvite invite;

  @override
  Widget build(BuildContext context) => Container(
        padding: EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: context.kaede.mint.withValues(alpha: .1),
          border: Border.all(color: context.kaede.mint.withValues(alpha: .5)),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              Icon(Icons.check_circle_outline_rounded,
                  color: context.kaede.mint),
              SizedBox(width: 8),
              Text(L10n.of(context).ui_authorized_for_your_account_33a340ad,
                  style: TextStyle(fontWeight: FontWeight.w800)),
            ]),
            SizedBox(height: 6),
            Text(
              L10n.of(context)
                  .ui_value0_can_now_offer_commands_in_the_contexts_2fa598df(
                      (invite.applicationName).toString()),
              style: TextStyle(color: context.kaede.textSoft),
            ),
          ],
        ),
      );
}

final class _GuildApplicationInstalledNotice extends StatelessWidget {
  const _GuildApplicationInstalledNotice();

  @override
  Widget build(BuildContext context) => Container(
        padding: EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: context.kaede.mint.withValues(alpha: .1),
          border: Border.all(color: context.kaede.mint.withValues(alpha: .5)),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Row(children: [
          Icon(Icons.check_circle_outline_rounded, color: context.kaede.mint),
          SizedBox(width: 8),
          Expanded(
            child: Text(L10n.of(context).ui_app_added_to_the_server_06ec620f,
                style: TextStyle(fontWeight: FontWeight.w800)),
          ),
        ]),
      );
}

final class _ApplicationInviteAccess extends StatelessWidget {
  const _ApplicationInviteAccess({required this.invite});

  final ApplicationInstallInvite invite;

  @override
  Widget build(BuildContext context) => Container(
        padding: EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: context.kaede.raised,
          border: Border.all(color: context.kaede.border),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(L10n.of(context).ui_access_review_451257ad,
                style: TextStyle(fontWeight: FontWeight.w800)),
            SizedBox(height: 7),
            Text(
              invite.supportsUserInstall
                  ? L10n.of(context).ui_account_installation_requests_159bac48
                  : L10n.of(context).ui_server_installation_requests_588f5c6a,
              style: TextStyle(color: context.kaede.muted),
            ),
            SizedBox(height: 8),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: [
                for (final scope in invite.supportsUserInstall
                    ? invite.userInstallScopes
                    : invite.scopes)
                  Chip(label: Text(scope)),
                if (invite.supportsUserInstall)
                  Chip(
                      label: Text(
                          L10n.of(context).ui_interactions_intent_bdae144e)),
              ],
            ),
            if (invite.supportsUserInstall &&
                invite.supportsGuildInstall &&
                (invite.scopes.isNotEmpty || invite.intents.isNotEmpty)) ...[
              SizedBox(height: 12),
              Text(
                L10n.of(context)
                    .ui_guild_install_template_not_granted_by_persona_b99ca78f,
                style: TextStyle(color: context.kaede.muted, fontSize: 12),
              ),
              SizedBox(height: 6),
              Wrap(
                spacing: 6,
                runSpacing: 6,
                children: [
                  for (final scope in invite.scopes) Chip(label: Text(scope)),
                  for (final intent in invite.intents)
                    Chip(
                        label: Text(L10n.of(context)
                            .ui_value0_intent_955fc082((intent).toString()))),
                ],
              ),
            ],
            if (invite.supportsGuildInstall) ...[
              SizedBox(height: 12),
              _ServerPermissionSummary(mask: invite.permissions),
            ],
            SizedBox(height: 12),
            Text(
              L10n.of(context)
                  .ui_opening_this_link_never_grants_encrypted_room_b05baaef,
              style: TextStyle(color: context.kaede.muted, fontSize: 12),
            ),
          ],
        ),
      );
}

final class _ServerPermissionSummary extends StatelessWidget {
  const _ServerPermissionSummary({required this.mask});

  final String mask;

  @override
  Widget build(BuildContext context) {
    final permissions = selectedApplicationPermissions(mask);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          L10n.of(context).ui_server_permissions_19d984ac,
          style: TextStyle(color: context.kaede.muted),
        ),
        SizedBox(height: 6),
        if (permissions.isEmpty)
          Text(
            L10n.of(context).ui_no_server_permissions_requested_3b5377f5,
            style: TextStyle(color: context.kaede.muted, fontSize: 12),
          )
        else
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              for (final permission in permissions)
                Tooltip(
                  message: permission.description,
                  child: Chip(label: Text(permission.label)),
                ),
            ],
          ),
      ],
    );
  }
}
