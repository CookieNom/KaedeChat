import 'dart:async';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/guild_navigation.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/role_colors.dart';
import 'package:kaede_mobile/src/e2ee/client.dart';
import 'package:kaede_mobile/src/e2ee/disclosures.dart';
import 'package:kaede_mobile/src/features/chat/channel_view.dart';
import 'package:kaede_mobile/src/features/chat/message_search_screen.dart';
import 'package:kaede_mobile/src/features/guild/guild_management_screen.dart';
import 'package:kaede_mobile/src/features/settings/settings_screen.dart';
import 'package:kaede_mobile/src/features/shared/remote_media.dart';
import 'package:kaede_mobile/src/features/voice/voice_room.dart';
import 'package:kaede_mobile/src/features/voice/voice_session.dart';
import 'package:kaede_mobile/src/gateway/gateway_client.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:uuid/uuid.dart';

String conversationHeaderTitle(KaedeChannel channel) {
  if (channel.guildRef != null) {
    final name = channel.name?.trim();
    return '#${name?.isNotEmpty == true ? name : 'channel'}';
  }
  if (channel.conversationType == 'group' &&
      channel.name?.trim().isNotEmpty == true) {
    return channel.name!.trim();
  }
  final names = channel.recipients.map((user) => user.name).toList();
  if (names.isEmpty) {
    return channel.conversationType == 'group'
        ? 'Group conversation'
        : 'Conversation';
  }
  return names.take(3).join(', ') +
      (names.length > 3 ? ' +${names.length - 3}' : '');
}

bool supportsPinnedMessages(KaedeChannel channel) =>
    channel.type == ChannelType.dm ||
    channel.type == ChannelType.text ||
    channel.type == ChannelType.announcement;

bool conversationCallUsesOverflow(double width) => width <= 360;

@visibleForTesting
bool messageSearchRouteCanDismiss(BuildContext context) =>
    context.mounted && ModalRoute.of(context)?.isCurrent == true;

String? conversationHeaderSubtitle(KaedeChannel channel) {
  final ordinary = channel.guildRef != null
      ? channel.topic?.trim()
      : channel.conversationType == 'group'
          ? '${channel.recipients.length + 1} members'
          : channel.topic?.trim();
  if (channel.encryptionMode != 'e2ee') return ordinary;
  final status = channel.encryptionState == 'active'
      ? 'Encrypted · identities unverified'
      : 'Encryption paused · key rotation required';
  return ordinary?.isNotEmpty == true ? '$status · $ordinary' : status;
}

Future<void> _showE2eeRoomSettings(
  BuildContext context,
  WidgetRef ref,
  KaedeChannel initialChannel, {
  required bool canManage,
}) async {
  var channel = initialChannel;
  var busy = false;
  String? error;
  String? safetyNumber;
  await showDialog<void>(
    context: context,
    builder: (dialogContext) => StatefulBuilder(
      builder: (context, setDialogState) {
        final encrypted = channel.encryptionMode == 'e2ee';
        final active = encrypted && channel.encryptionState == 'active';
        final needsRekey = encrypted &&
            {'rekeying', 'failed'}.contains(channel.encryptionState);
        Future<void> run(Future<void> Function(MobileE2EEClient) action) async {
          if (busy) return;
          setDialogState(() {
            busy = true;
            error = null;
          });
          try {
            final controller = ref.read(mobileControllerProvider.notifier);
            final client = await controller.e2eeClient();
            await action(client);
            await controller.refreshNavigation();
          } on Object catch (caught) {
            error = userFacingError(
              caught,
              summary: 'Could not update end-to-end encryption.',
            );
          } finally {
            if (dialogContext.mounted) {
              setDialogState(() => busy = false);
            }
          }
        }

        return AlertDialog(
          title: const Row(children: [
            Icon(Icons.lock_rounded),
            SizedBox(width: 10),
            Expanded(child: Text('End-to-end encryption')),
          ]),
          content: SizedBox(
            width: 520,
            child: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    active
                        ? channel.type == ChannelType.voice
                            ? 'Encryption is active for microphone, camera, screen video, and screen audio.'
                            : 'Encryption is active for new messages, files, and supported calls in this channel.'
                        : needsRekey
                            ? 'Encrypted activity is paused until a member rotates the room keys.'
                            : encrypted
                                ? 'Encryption is being prepared. Messaging remains paused until setup completes.'
                                : channel.type == ChannelType.voice
                                    ? 'Encryption is optional and cannot be turned off after it is enabled for this voice channel.'
                                    : 'Encryption is optional and cannot be turned off after it is enabled for this conversation.',
                  ),
                  const SizedBox(height: 12),
                  const Text(
                    'Identity verification',
                    style: TextStyle(fontWeight: FontWeight.w800),
                  ),
                  const SizedBox(height: 6),
                  const Text(
                    'Until participants compare the safety number through a separate trusted channel, content is encrypted but identities are unverified. Comparing it is what detects first-contact or active-instance key substitution.',
                  ),
                  const SizedBox(height: 12),
                  if (!encrypted) ...[
                    const Text(
                      'Before enabling:',
                      style: TextStyle(fontWeight: FontWeight.w800),
                    ),
                    const SizedBox(height: 6),
                    Text(
                      channel.type == ChannelType.voice
                          ? '• Server recording, transcription, and media moderation will be unavailable.\n'
                              '• Unsupported clients cannot join.\n'
                              '• Participants, timing, track types, and traffic metadata remain visible.\n'
                              '• Anyone can still record media on their own device.'
                          : '• Server message search, link previews, bots, webhooks, server-side file previews and malware scanning will be unavailable.\n'
                              '• Notification wakes contain no message content.\n'
                              '• Existing history stays plaintext; new content is encrypted.\n'
                              '• Metadata such as participants, timing, and message size remains visible.\n'
                              '• Losing the synchronized encrypted vault, every trusted client’s local state, and the recovery backup permanently loses encrypted history.\n'
                              '• Removed members retain content already received.',
                    ),
                  ],
                  if (safetyNumber != null) ...[
                    const SizedBox(height: 14),
                    const Text('Conversation safety number',
                        style: TextStyle(fontWeight: FontWeight.w800)),
                    const SizedBox(height: 6),
                    SelectableText(safetyNumber!),
                    const SizedBox(height: 6),
                    const Text(
                      'Compare this number with the other participants through a trusted channel. It changes when membership or devices change.',
                    ),
                  ],
                  if (error != null) ...[
                    const SizedBox(height: 12),
                    Text(error!,
                        style: const TextStyle(color: KaedeColors.coral)),
                  ],
                ],
              ),
            ),
          ),
          actions: [
            TextButton(
              onPressed: busy ? null : () => Navigator.pop(dialogContext),
              child: const Text('Done'),
            ),
            if (active)
              FilledButton.tonal(
                onPressed: busy
                    ? null
                    : () => run((client) async {
                          await client.syncRoomState(channel);
                          final value = await client.safetyNumber(channel);
                          if (dialogContext.mounted) {
                            setDialogState(() => safetyNumber = value);
                          }
                        }),
                child: const Text('Verify safety number'),
              ),
            if (canManage && (!encrypted || needsRekey))
              FilledButton.icon(
                onPressed: busy
                    ? null
                    : () => run((client) async {
                          channel = needsRekey
                              ? await client.rekeyRoom(channel)
                              : await client.enableRoom(channel);
                          if (!needsRekey) {
                            final accountRef = ref
                                    .read(mobileControllerProvider)
                                    .user
                                    ?.ref
                                    .wire ??
                                ref
                                    .read(mobileControllerProvider.notifier)
                                    .api
                                    .tokens
                                    ?.userRef
                                    ?.wire;
                            if (accountRef != null) {
                              await acknowledgeEncryptedRoom(
                                accountRef,
                                channel.ref.wire,
                              );
                            }
                          }
                        }),
                icon: Icon(
                    needsRekey ? Icons.sync_lock_rounded : Icons.lock_rounded),
                label: Text(needsRekey ? 'Rotate keys' : 'Enable encryption'),
              ),
          ],
        );
      },
    ),
  );
}

final class MobileShell extends ConsumerStatefulWidget {
  const MobileShell({super.key});

  @override
  ConsumerState<MobileShell> createState() => _MobileShellState();
}

final class _MobileShellState extends ConsumerState<MobileShell> {
  var _section = _ShellSection.messages;
  late final PageController _pages;

  /// Page index and conversation visibility are notifiers rather than state
  /// fields so settling a swipe rebuilds the back handler and nothing else.
  final _messagePage = ValueNotifier(0);
  final _conversationVisible = ValueNotifier(false);

  @override
  void initState() {
    super.initState();
    _pages = PageController();
  }

  @override
  void dispose() {
    _pages.dispose();
    _messagePage.dispose();
    _conversationVisible.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    ref.listen<IncomingCall?>(
      mobileControllerProvider.select((state) => state.pendingCallJoin),
      (_, call) {
        if (call != null) unawaited(_joinAnsweredCall(call));
      },
    );
    // Only the open conversation belongs to this build. Banners and the voice
    // strip watch their own slices, so an arriving message or presence update
    // cannot rebuild the page view while a swipe is still in flight.
    final activeChannel = ref.watch(
      mobileControllerProvider.select((state) => state.activeChannel),
    );
    final body = SafeArea(
      bottom: false,
      child: Column(
        children: [
          const _IncomingCallBanner(),
          _ShellBanners(
            onOpenSettings: () => _showSection(_ShellSection.settings),
          ),
          _VoiceStatusStrip(onOpenChannel: _openVoiceChannel),
          Expanded(
            child: switch (_section) {
              _ShellSection.messages => PageView(
                  controller: _pages,
                  // Builds the neighbouring page ahead of time so the first
                  // frames of a swipe are not spent laying it out.
                  allowImplicitScrolling: true,
                  onPageChanged: _onPageChanged,
                  children: [
                    _ChatBrowser(
                      onOpenChannel: _openConversation,
                      onOpenFriends: () => _showSection(_ShellSection.friends),
                      onOpenSettings: () =>
                          _showSection(_ShellSection.settings),
                    ),
                    activeChannel == null
                        ? const _NoConversationSelected()
                        : _ConversationScreen(
                            channel: activeChannel,
                            visible: _conversationVisible,
                            onBack: _openNavigation,
                            onMembers: activeChannel.guildRef == null
                                ? null
                                : _openMembers,
                          ),
                  ],
                ),
              _ShellSection.friends => _SectionScreen(
                  title: 'Friends',
                  onBack: () => _showSection(_ShellSection.messages),
                  child: _FriendsPage(onOpenChat: () {
                    _showSection(_ShellSection.messages);
                    _openConversation();
                  }),
                ),
              _ShellSection.settings => _SectionScreen(
                  title: 'Settings',
                  onBack: () => _showSection(_ShellSection.messages),
                  child: const SettingsScreen(),
                ),
            },
          ),
        ],
      ),
    );
    return ValueListenableBuilder<int>(
      valueListenable: _messagePage,
      child: Scaffold(body: body),
      builder: (context, page, child) => PopScope(
        canPop: _section == _ShellSection.messages && page == 0,
        onPopInvokedWithResult: (didPop, _) {
          if (didPop) return;
          if (page > 0) {
            _openNavigation();
          } else {
            _showSection(_ShellSection.messages);
          }
        },
        child: child!,
      ),
    );
  }

  Future<void> _joinAnsweredCall(IncomingCall call) async {
    final controller = ref.read(mobileControllerProvider.notifier);
    controller.clearPendingCallJoin();
    KaedeChannel? channel;
    for (final candidate in ref.read(mobileControllerProvider).dms) {
      if (candidate.ref == call.channel) channel = candidate;
    }
    if (channel == null) {
      await controller.refreshNavigation();
      for (final candidate in ref.read(mobileControllerProvider).dms) {
        if (candidate.ref == call.channel) channel = candidate;
      }
    }
    if (!mounted || channel == null) return;
    await controller.selectDm(channel);
    if (!mounted) return;
    if (_section != _ShellSection.messages) {
      setState(() => _section = _ShellSection.messages);
    }
    _openConversation();
    await Navigator.of(context).push(MaterialPageRoute<void>(
      builder: (_) => _DmCallRoom(channel: channel!, callRef: call.call),
    ));
  }

  void _onPageChanged(int page) {
    _messagePage.value = page;
    _conversationVisible.value = page == 1;
    ref
        .read(mobileControllerProvider.notifier)
        .setConversationPaneVisible(page == 1);
  }

  Future<void> _openVoiceChannel(KaedeChannel channel) async {
    await ref.read(mobileControllerProvider.notifier).selectChannel(channel);
    if (!mounted) return;
    if (_section != _ShellSection.messages) {
      setState(() => _section = _ShellSection.messages);
    }
    _openConversation();
  }

  void _openConversation() {
    if (!_pages.hasClients) return;
    _pages.animateToPage(1,
        duration: const Duration(milliseconds: 220),
        curve: Curves.easeOutCubic);
  }

  void _openNavigation() {
    if (!_pages.hasClients) return;
    _pages.animateToPage(0,
        duration: const Duration(milliseconds: 220),
        curve: Curves.easeOutCubic);
  }

  void _openMembers() {
    final guild = ref.read(mobileControllerProvider).activeGuild;
    if (guild == null) return;
    unawaited(Navigator.of(context).push(
      MaterialPageRoute(builder: (_) => _GuildMemberRoute(guild: guild)),
    ));
  }

  void _showSection(_ShellSection section) {
    setState(() => _section = section);
    ref.read(mobileControllerProvider.notifier).setConversationPaneVisible(
          section == _ShellSection.messages && _messagePage.value == 1,
        );
  }
}

enum _ShellSection { messages, friends, settings }

final class _IncomingCallBanner extends ConsumerWidget {
  const _IncomingCallBanner();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final call = ref.watch(
      mobileControllerProvider.select((state) => state.incomingCall),
    );
    if (call == null) return const SizedBox.shrink();
    final controller = ref.read(mobileControllerProvider.notifier);
    return SafeArea(
      bottom: false,
      child: Material(
        color: KaedeColors.sidebar,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(14, 8, 10, 8),
          child: Row(
            children: [
              const Icon(Icons.call_rounded, color: KaedeColors.mint),
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                      call.callerName,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(fontWeight: FontWeight.w800),
                    ),
                    const Text('Incoming call'),
                  ],
                ),
              ),
              IconButton.filledTonal(
                tooltip: 'Decline',
                onPressed: controller.declineIncomingCall,
                icon: const Icon(Icons.call_end_rounded,
                    color: KaedeColors.coral),
              ),
              const SizedBox(width: 6),
              IconButton.filled(
                tooltip: 'Answer',
                onPressed: controller.answerIncomingCall,
                icon: const Icon(Icons.call_rounded),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

final class _SectionScreen extends StatelessWidget {
  const _SectionScreen({
    required this.title,
    required this.onBack,
    required this.child,
  });

  final String title;
  final VoidCallback onBack;
  final Widget child;

  @override
  Widget build(BuildContext context) => ColoredBox(
        color: KaedeColors.sidebar,
        child: Column(
          children: [
            ConversationCompactHeader(
              leading: IconButton(
                onPressed: onBack,
                icon: const Icon(Icons.arrow_back_rounded),
              ),
              title: title,
            ),
            Expanded(child: SafeArea(top: false, child: child)),
          ],
        ),
      );
}

final class _ConversationScreen extends ConsumerStatefulWidget {
  const _ConversationScreen({
    required this.channel,
    required this.visible,
    required this.onBack,
    this.onMembers,
  });

  final KaedeChannel channel;

  /// Whether the conversation is the page on screen. A listenable rather than a
  /// plain flag so settling a swipe does not rebuild the conversation, which
  /// only needs the value for its encryption disclosure.
  final ValueListenable<bool> visible;
  final VoidCallback onBack;
  final VoidCallback? onMembers;

  @override
  ConsumerState<_ConversationScreen> createState() =>
      _ConversationScreenState();
}

final class _ConversationScreenState
    extends ConsumerState<_ConversationScreen> {
  Map<String, Object?>? _activeCall;
  var _callBusy = false;
  String? _disclosureInFlight;

  bool get _isGroup => widget.channel.conversationType == 'group';

  String get _title {
    return conversationHeaderTitle(widget.channel);
  }

  String? get _subtitle {
    return conversationHeaderSubtitle(widget.channel);
  }

  @override
  void initState() {
    super.initState();
    widget.visible.addListener(_visibilityChanged);
    unawaited(_loadCall());
    _scheduleEncryptedRoomDisclosure();
  }

  @override
  void didUpdateWidget(covariant _ConversationScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.visible != widget.visible) {
      oldWidget.visible.removeListener(_visibilityChanged);
      widget.visible.addListener(_visibilityChanged);
    }
    if (oldWidget.channel.ref != widget.channel.ref ||
        !identical(oldWidget.channel, widget.channel)) {
      unawaited(_loadCall());
    }
    if (oldWidget.channel.ref != widget.channel.ref ||
        oldWidget.channel.encryptionMode != widget.channel.encryptionMode) {
      _scheduleEncryptedRoomDisclosure();
    }
  }

  @override
  void dispose() {
    widget.visible.removeListener(_visibilityChanged);
    super.dispose();
  }

  void _visibilityChanged() {
    if (widget.visible.value) _scheduleEncryptedRoomDisclosure();
  }

  void _scheduleEncryptedRoomDisclosure() {
    if (!widget.visible.value || widget.channel.encryptionMode != 'e2ee') {
      return;
    }
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) unawaited(_showEncryptedRoomDisclosure());
    });
  }

  Future<void> _showEncryptedRoomDisclosure() async {
    if (!widget.visible.value ||
        widget.channel.encryptionMode != 'e2ee' ||
        _disclosureInFlight != null) {
      return;
    }
    final channel = widget.channel;
    final accountRef = ref.read(mobileControllerProvider).user?.ref.wire ??
        ref.read(mobileControllerProvider.notifier).api.tokens?.userRef?.wire;
    if (accountRef == null) return;
    final disclosureKey = '$accountRef|${channel.ref.wire}';
    _disclosureInFlight = disclosureKey;
    try {
      if (await hasAcknowledgedEncryptedRoom(accountRef, channel.ref.wire)) {
        return;
      }
      if (!mounted ||
          !widget.visible.value ||
          widget.channel.ref != channel.ref) {
        return;
      }
      final kind = channel.guildRef == null
          ? EncryptedRoomKind.conversation
          : channel.type == ChannelType.voice
              ? EncryptedRoomKind.media
              : EncryptedRoomKind.messages;
      final accepted = await showDialog<bool>(
        context: context,
        barrierDismissible: false,
        builder: (dialogContext) => AlertDialog(
          icon: const Icon(Icons.lock_rounded),
          title: const Text('Encrypted room'),
          content: SingleChildScrollView(
            child: Text(encryptedRoomJoinWarning(kind)),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext, false),
              child: const Text('Go back'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(dialogContext, true),
              child: const Text('Continue'),
            ),
          ],
        ),
      );
      if (accepted == true) {
        await acknowledgeEncryptedRoom(accountRef, channel.ref.wire);
      } else if (mounted && widget.channel.ref == channel.ref) {
        widget.onBack();
      }
    } finally {
      if (_disclosureInFlight == disclosureKey) {
        _disclosureInFlight = null;
      }
      if (mounted && widget.channel.ref != channel.ref) {
        _scheduleEncryptedRoomDisclosure();
      }
    }
  }

  Future<void> _loadCall() async {
    if (widget.channel.type != ChannelType.dm) return;
    try {
      final result = await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .activeCall(widget.channel.ref);
      if (!mounted) return;
      setState(() => _activeCall = result['call'] is Map
          ? Map<String, Object?>.from(result['call']! as Map)
          : null);
    } on Object {
      // Chat remains usable if call presence cannot be loaded.
    }
  }

  EntityRef? get _callRef {
    final call = _activeCall;
    if (call == null) return null;
    final id = '${call['id'] ?? ''}';
    final domain = '${call['authority_domain'] ?? ''}';
    if (id.isEmpty || domain.isEmpty) return null;
    return EntityRef(Snowflake(id), Domain(domain));
  }

  Future<void> _startOrJoinCall() async {
    if (_callBusy) return;
    setState(() => _callBusy = true);
    try {
      var callRef = _callRef;
      if (callRef == null) {
        final created = await ref
            .read(mobileControllerProvider.notifier)
            .repository
            .startCall(widget.channel.ref);
        if (!mounted) return;
        setState(() => _activeCall = created);
        callRef = _callRef;
      } else {
        final accepted = await ref
            .read(mobileControllerProvider.notifier)
            .repository
            .callAction(callRef, 'accept');
        if (!mounted) return;
        setState(() => _activeCall = accepted);
      }
      if (callRef == null || !mounted) return;
      await Navigator.of(context).push(MaterialPageRoute<void>(
        builder: (_) => _DmCallRoom(channel: widget.channel, callRef: callRef!),
      ));
      await _loadCall();
    } on Object catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(userFacingError(error))),
        );
      }
    } finally {
      if (mounted) setState(() => _callBusy = false);
    }
  }

  Future<void> _showGroupSettings() async {
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      builder: (_) => _GroupDmSettings(channel: widget.channel),
    );
  }

  Future<void> _showPinnedMessages() => showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        showDragHandle: true,
        builder: (_) => _PinnedMessagesSheet(channel: widget.channel),
      );

  Future<void> _showMessageSearch() => Navigator.of(context).push(
        MaterialPageRoute<void>(
          builder: (searchContext) => MessageSearchScreen(
            repository: ref.read(mobileControllerProvider.notifier).repository,
            scope: widget.channel.guildRef == null ? 'channel' : 'guild',
            scopeRef: widget.channel.guildRef ?? widget.channel.ref,
            channel: widget.channel,
            accountRef:
                ref.read(mobileControllerProvider.notifier).api.tokens?.userRef,
            users: messageSearchUserCandidates(<KaedeUser?>[
              ref.read(mobileControllerProvider).user,
              ...ref.read(mobileControllerProvider).userProfiles.values,
              ...widget.channel.recipients,
            ]),
            onJump: (result) async {
              final controller = ref.read(mobileControllerProvider.notifier);
              final opened = await controller.selectAndJumpToMessage(
                result.channel,
                result.message.ref,
                shouldContinue: () =>
                    messageSearchRouteCanDismiss(searchContext),
              );
              if (!searchContext.mounted ||
                  !opened ||
                  !messageSearchRouteCanDismiss(searchContext)) {
                return;
              }
              Navigator.of(searchContext).pop();
            },
          ),
        ),
      );

  /// Encryption is a one-way decision for a room, so it lives inside the
  /// channel or conversation settings sheet rather than the header.
  Future<void> _showChannelSettings() async {
    if (widget.channel.guildRef != null) {
      final state = ref.read(mobileControllerProvider);
      final guild = state.activeGuild;
      final controller = ref.read(mobileControllerProvider.notifier);
      final localGuild =
          controller.api.tokens?.instance == widget.channel.guildRef!.domain;
      final canManage = guild != null &&
          localGuild &&
          (state.user?.ref == guild.ownerRef ||
              guild.allows(Permission.manageChannels));
      await showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        useSafeArea: true,
        showDragHandle: true,
        builder: (_) => _ChannelDetailsSheet(
          channel: widget.channel,
          guild: guild,
          canManageChannels: canManage,
        ),
      );
      return;
    }
    if (_isGroup) {
      await _showGroupSettings();
      return;
    }
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      builder: (_) => _DirectMessageDetailsSheet(channel: widget.channel),
    );
  }

  @override
  Widget build(BuildContext context) {
    final recipient = widget.channel.recipients.isEmpty
        ? null
        : widget.channel.recipients.first;
    final width = MediaQuery.sizeOf(context).width;
    final compactHeader = width <= 400;
    final callUsesOverflow = conversationCallUsesOverflow(width);
    final isDm = widget.channel.type == ChannelType.dm;
    final overflowItems = <PopupMenuEntry<String>>[
      if (supportsPinnedMessages(widget.channel))
        const PopupMenuItem(
          value: 'pins',
          child: ListTile(
            dense: true,
            contentPadding: EdgeInsets.zero,
            leading: Icon(Icons.push_pin_outlined),
            title: Text('Pinned messages'),
          ),
        ),
      if (isDm && callUsesOverflow)
        PopupMenuItem(
          value: 'call',
          enabled: !_callBusy,
          child: ListTile(
            dense: true,
            contentPadding: EdgeInsets.zero,
            leading: Icon(
                _activeCall == null ? Icons.call_outlined : Icons.call_rounded),
            title: Text(_activeCall == null ? 'Start call' : 'Join call'),
          ),
        ),
      if (widget.onMembers != null && compactHeader)
        const PopupMenuItem(
          value: 'members',
          child: ListTile(
            dense: true,
            contentPadding: EdgeInsets.zero,
            leading: Icon(Icons.people_alt_outlined),
            title: Text('Member list'),
          ),
        ),
      PopupMenuItem(
        value: 'settings',
        child: ListTile(
          dense: true,
          contentPadding: EdgeInsets.zero,
          leading: const Icon(Icons.settings_outlined),
          title: Text(widget.channel.guildRef != null
              ? 'Channel settings'
              : _isGroup
                  ? 'Group settings'
                  : 'Conversation settings'),
        ),
      ),
    ];
    return Column(
      children: [
        ConversationCompactHeader(
          leading: IconButton(
            onPressed: widget.onBack,
            icon: const Icon(Icons.arrow_back_rounded),
          ),
          avatar: recipient == null || _isGroup
              ? null
              : GestureDetector(
                  onTap: () => showUserProfile(
                    context,
                    recipient,
                    ref
                        .read(mobileControllerProvider.notifier)
                        .presenceFor(recipient),
                  ),
                  child: UserAvatar(
                    user: recipient,
                    radius: 17,
                    presence: ref
                            .watch(mobileControllerProvider)
                            .presenceByUser[recipient.ref] ??
                        recipient.presence,
                    ringColor: KaedeColors.canvas,
                  ),
                ),
          title: _title,
          subtitle: _subtitle,
          actions: [
            if (isDm && !callUsesOverflow)
              IconButton(
                tooltip: _activeCall == null ? 'Start call' : 'Join call',
                onPressed: _callBusy ? null : _startOrJoinCall,
                icon: Icon(_activeCall == null
                    ? Icons.call_outlined
                    : Icons.call_rounded),
              ),
            IconButton(
              tooltip: 'Search this conversation',
              onPressed: _showMessageSearch,
              icon: const Icon(Icons.search_rounded),
            ),
            if (widget.onMembers != null && !compactHeader)
              IconButton(
                tooltip: 'Member list',
                onPressed: widget.onMembers,
                icon: const Icon(Icons.people_alt_outlined),
              ),
            PopupMenuButton<String>(
              tooltip: 'More options',
              position: PopupMenuPosition.under,
              icon: const Icon(Icons.more_vert_rounded),
              onSelected: (action) {
                switch (action) {
                  case 'pins':
                    unawaited(_showPinnedMessages());
                    return;
                  case 'settings':
                    unawaited(_showChannelSettings());
                    return;
                  case 'members':
                    widget.onMembers?.call();
                    return;
                  case 'call':
                    unawaited(_startOrJoinCall());
                    return;
                }
              },
              itemBuilder: (_) => overflowItems,
            ),
          ],
        ),
        const Expanded(child: ChannelView()),
      ],
    );
  }
}

/// Guild channel details: what the channel is, plus the decisions that belong
/// one level away from the transcript.
final class _ChannelDetailsSheet extends ConsumerStatefulWidget {
  const _ChannelDetailsSheet({
    required this.channel,
    required this.guild,
    required this.canManageChannels,
  });

  final KaedeChannel channel;
  final KaedeGuild? guild;
  final bool canManageChannels;

  @override
  ConsumerState<_ChannelDetailsSheet> createState() =>
      _ChannelDetailsSheetState();
}

final class _ChannelDetailsSheetState
    extends ConsumerState<_ChannelDetailsSheet> {
  var _busy = false;

  KaedeChannel get _channel {
    for (final guild in ref.read(mobileControllerProvider).guilds) {
      for (final candidate in guild.channels) {
        if (candidate.ref == widget.channel.ref) return candidate;
      }
    }
    return widget.channel;
  }

  Future<void> _edit() async {
    final guild = widget.guild;
    if (guild == null) return;
    final channel = _channel;
    final draft = await showGuildChannelEditorSheet(
      context,
      channel: channel,
      channels: guild.channels,
    );
    if (draft == null || !mounted) return;
    setState(() => _busy = true);
    try {
      final controller = ref.read(mobileControllerProvider.notifier);
      await controller.repository.updateChannel(
        guild.ref,
        channel.ref,
        channel.version ?? '*',
        draft.json,
      );
      await controller.refreshNavigation();
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Channel saved')),
        );
      }
    } on Object catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(userFacingError(
            error,
            summary: 'Could not save the channel',
          )),
        ));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final channel = _channel;
    ref.watch(mobileControllerProvider
        .select((state) => state.e2eeActivationEnabled));
    final state = ref.read(mobileControllerProvider);
    final showEncryption =
        channel.encryptionMode == 'e2ee' || state.e2eeActivationEnabled;
    final topic = channel.topic?.trim();
    return SafeArea(
      child: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(20, 0, 20, 24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          mainAxisSize: MainAxisSize.min,
          children: [
            Row(
              children: [
                Icon(
                  channel.type == ChannelType.voice
                      ? Icons.volume_up_rounded
                      : channel.type == ChannelType.announcement
                          ? Icons.campaign_rounded
                          : Icons.tag_rounded,
                  color: KaedeColors.muted,
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    channel.name ?? 'channel',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: Theme.of(context).textTheme.headlineSmall,
                  ),
                ),
              ],
            ),
            if (topic?.isNotEmpty == true) ...[
              const SizedBox(height: 8),
              Text(
                topic!,
                style: const TextStyle(color: KaedeColors.muted, height: 1.4),
              ),
            ],
            const SizedBox(height: 18),
            if (widget.canManageChannels)
              _SettingsRow(
                icon: Icons.edit_outlined,
                title: 'Edit channel',
                subtitle: 'Name, category, topic and slow mode',
                onTap: _busy ? null : _edit,
              ),
            if (showEncryption)
              _SettingsRow(
                icon: channel.encryptionMode == 'e2ee'
                    ? Icons.lock_rounded
                    : Icons.lock_open_rounded,
                iconColor: channel.encryptionMode == 'e2ee'
                    ? KaedeColors.mint
                    : KaedeColors.muted,
                title: 'End-to-end encryption',
                subtitle: channel.encryptionMode == 'e2ee'
                    ? channel.encryptionState == 'active'
                        ? 'Active · verify the safety number'
                        : 'Paused until a member rotates the keys'
                    : 'Off · permanent once enabled',
                onTap: _busy
                    ? null
                    : () => _showE2eeRoomSettings(
                          context,
                          ref,
                          channel,
                          canManage: widget.canManageChannels,
                        ),
              ),
            if (!widget.canManageChannels && !showEncryption)
              const Text(
                'You do not have permission to change this channel.',
                style: TextStyle(color: KaedeColors.muted, fontSize: 13),
              ),
            if (widget.guild != null && widget.canManageChannels)
              _SettingsRow(
                icon: Icons.tune_rounded,
                title: 'Guild settings',
                subtitle: 'Roles, members and every channel',
                onTap: _busy
                    ? null
                    : () {
                        Navigator.pop(context);
                        Navigator.of(context).push(
                          MaterialPageRoute<void>(
                            builder: (_) =>
                                GuildManagementScreen(guild: widget.guild!),
                          ),
                        );
                      },
              ),
          ],
        ),
      ),
    );
  }
}

/// One-to-one conversation details, including the encryption decision.
final class _DirectMessageDetailsSheet extends ConsumerWidget {
  const _DirectMessageDetailsSheet({required this.channel});

  final KaedeChannel channel;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    ref.watch(mobileControllerProvider.select((state) => (
          state.dms,
          state.user,
          state.e2eeActivationEnabled,
        )));
    final state = ref.read(mobileControllerProvider);
    var current = channel;
    for (final candidate in state.dms) {
      if (candidate.ref == channel.ref) {
        current = candidate;
        break;
      }
    }
    final recipient = current.recipients.isEmpty
        ? null
        : current.recipients.firstWhere(
            (user) => user.ref != state.user?.ref,
            orElse: () => current.recipients.first,
          );
    final showEncryption =
        current.encryptionMode == 'e2ee' || state.e2eeActivationEnabled;
    return SafeArea(
      child: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(20, 0, 20, 24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              'Conversation settings',
              style: Theme.of(context).textTheme.headlineSmall,
            ),
            const SizedBox(height: 16),
            if (recipient != null)
              _SettingsRow(
                icon: Icons.person_outline_rounded,
                title: recipient.name,
                subtitle: recipient.handle,
                onTap: () => showUserProfile(
                  context,
                  recipient,
                  ref
                      .read(mobileControllerProvider.notifier)
                      .presenceFor(recipient),
                ),
              ),
            if (showEncryption)
              _SettingsRow(
                icon: current.encryptionMode == 'e2ee'
                    ? Icons.lock_rounded
                    : Icons.lock_open_rounded,
                iconColor: current.encryptionMode == 'e2ee'
                    ? KaedeColors.mint
                    : KaedeColors.muted,
                title: 'End-to-end encryption',
                subtitle: current.encryptionMode == 'e2ee'
                    ? current.encryptionState == 'active'
                        ? 'Active · verify the safety number'
                        : 'Paused until keys rotate'
                    : 'Off · permanent once enabled',
                onTap: () => _showE2eeRoomSettings(
                  context,
                  ref,
                  current,
                  canManage: true,
                ),
              ),
          ],
        ),
      ),
    );
  }
}

/// Row used by the settings sheets: icon, title, supporting line, chevron.
final class _SettingsRow extends StatelessWidget {
  const _SettingsRow({
    required this.icon,
    required this.title,
    required this.onTap,
    this.subtitle,
    this.iconColor,
  });

  final IconData icon;
  final String title;
  final String? subtitle;
  final VoidCallback? onTap;
  final Color? iconColor;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(bottom: 8),
        child: Material(
          color: KaedeColors.raised,
          borderRadius: BorderRadius.circular(KaedeRadius.medium),
          child: InkWell(
            onTap: onTap,
            borderRadius: BorderRadius.circular(KaedeRadius.medium),
            child: Container(
              padding: const EdgeInsets.fromLTRB(14, 12, 10, 12),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(KaedeRadius.medium),
                border: Border.all(color: KaedeColors.border),
              ),
              child: Row(
                children: [
                  Icon(icon, size: 19, color: iconColor ?? KaedeColors.muted),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          title,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            fontWeight: FontWeight.w600,
                            fontSize: 14.5,
                          ),
                        ),
                        if (subtitle case final detail?)
                          Text(
                            detail,
                            maxLines: 2,
                            style: const TextStyle(
                              color: KaedeColors.muted,
                              fontSize: 12,
                              height: 1.3,
                            ),
                          ),
                      ],
                    ),
                  ),
                  const Icon(Icons.chevron_right_rounded,
                      color: KaedeColors.muted),
                ],
              ),
            ),
          ),
        ),
      );
}

final class _PinnedMessagesSheet extends ConsumerStatefulWidget {
  const _PinnedMessagesSheet({required this.channel});

  final KaedeChannel channel;

  @override
  ConsumerState<_PinnedMessagesSheet> createState() =>
      _PinnedMessagesSheetState();
}

final class _PinnedMessagesSheetState
    extends ConsumerState<_PinnedMessagesSheet> {
  List<KaedeMessage> _messages = const [];
  final _unpinning = <EntityRef>{};
  var _loading = true;
  String? _error;

  bool get _canManage =>
      widget.channel.type == ChannelType.dm ||
      widget.channel.allows(Permission.manageMessages);

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
      final controller = ref.read(mobileControllerProvider.notifier);
      var messages = await controller.repository.pins(widget.channel.ref);
      if (widget.channel.encryptionMode == 'e2ee') {
        messages = await (await controller.e2eeClient())
            .decryptMessages(widget.channel, messages);
      }
      messages.sort((a, b) => b.createdAt.compareTo(a.createdAt));
      if (mounted) setState(() => _messages = List.unmodifiable(messages));
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(
              error,
              summary: 'Could not load pinned messages',
            ));
      }
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _jump(KaedeMessage message) async {
    Navigator.of(context).pop();
    await ref
        .read(mobileControllerProvider.notifier)
        .jumpToMessage(message.ref, expectedChannel: widget.channel.ref);
  }

  Future<void> _unpin(KaedeMessage message) async {
    if (_unpinning.contains(message.ref)) return;
    setState(() {
      _unpinning.add(message.ref);
      _error = null;
    });
    try {
      await ref
          .read(mobileControllerProvider.notifier)
          .setMessagePinned(message, false);
      if (mounted) {
        setState(() => _messages = List.unmodifiable(
              _messages.where((item) => item.ref != message.ref),
            ));
      }
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(
              error,
              summary: 'Could not unpin that message',
            ));
      }
    } finally {
      if (mounted) setState(() => _unpinning.remove(message.ref));
    }
  }

  @override
  Widget build(BuildContext context) => SafeArea(
        child: SizedBox(
          height: MediaQuery.sizeOf(context).height * .72,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 2, 8, 12),
                child: Row(
                  children: [
                    const Icon(Icons.push_pin_rounded),
                    const SizedBox(width: 10),
                    const Expanded(
                      child: Text(
                        'Pinned messages',
                        style: TextStyle(
                          fontSize: 20,
                          fontWeight: FontWeight.w800,
                        ),
                      ),
                    ),
                    IconButton(
                      tooltip: 'Close',
                      onPressed: () => Navigator.pop(context),
                      icon: const Icon(Icons.close_rounded),
                    ),
                  ],
                ),
              ),
              if (_error case final error?)
                Material(
                  color: Theme.of(context).colorScheme.errorContainer,
                  child: Padding(
                    padding: const EdgeInsets.fromLTRB(16, 8, 8, 8),
                    child: Row(
                      children: [
                        Expanded(child: Text(error)),
                        TextButton(
                            onPressed: _load, child: const Text('Retry')),
                      ],
                    ),
                  ),
                ),
              Expanded(
                child: _loading
                    ? const Center(child: CircularProgressIndicator())
                    : _error != null && _messages.isEmpty
                        ? const Center(
                            child: Padding(
                              padding: EdgeInsets.all(28),
                              child: Text(
                                'Pinned messages are unavailable right now.',
                                textAlign: TextAlign.center,
                                style: TextStyle(color: KaedeColors.muted),
                              ),
                            ),
                          )
                        : _messages.isEmpty
                            ? const _EmptyPinnedMessages()
                            : ListView.separated(
                                padding:
                                    const EdgeInsets.fromLTRB(12, 8, 12, 20),
                                itemCount: _messages.length,
                                separatorBuilder: (_, __) =>
                                    const SizedBox(height: 8),
                                itemBuilder: (context, index) {
                                  final message = _messages[index];
                                  final author = message.author?.name ??
                                      message.authorRef.wire;
                                  final content = message.content?.trim();
                                  final preview = content?.isNotEmpty == true
                                      ? content!
                                      : message.attachments.isNotEmpty
                                          ? '${message.attachments.length} attachment${message.attachments.length == 1 ? '' : 's'}'
                                          : 'Message';
                                  return Card(
                                    margin: EdgeInsets.zero,
                                    child: ListTile(
                                      key: ValueKey(
                                          'pinned-${message.ref.wire}'),
                                      onTap: () => _jump(message),
                                      title: Text(
                                        author,
                                        maxLines: 1,
                                        overflow: TextOverflow.ellipsis,
                                        style: const TextStyle(
                                          fontWeight: FontWeight.w700,
                                        ),
                                      ),
                                      subtitle: Text(
                                        preview,
                                        maxLines: 3,
                                        overflow: TextOverflow.ellipsis,
                                      ),
                                      trailing: _canManage
                                          ? IconButton(
                                              tooltip: 'Unpin message',
                                              onPressed: _unpinning
                                                      .contains(message.ref)
                                                  ? null
                                                  : () => _unpin(message),
                                              icon: _unpinning
                                                      .contains(message.ref)
                                                  ? const SizedBox.square(
                                                      dimension: 18,
                                                      child:
                                                          CircularProgressIndicator(
                                                        strokeWidth: 2,
                                                      ),
                                                    )
                                                  : const Icon(
                                                      Icons.push_pin_rounded),
                                            )
                                          : const Icon(
                                              Icons.chevron_right_rounded),
                                    ),
                                  );
                                },
                              ),
              ),
            ],
          ),
        ),
      );
}

final class _EmptyPinnedMessages extends StatelessWidget {
  const _EmptyPinnedMessages();

  @override
  Widget build(BuildContext context) => const Center(
        child: Padding(
          padding: EdgeInsets.all(28),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.push_pin_outlined, size: 38, color: KaedeColors.muted),
              SizedBox(height: 12),
              Text(
                'No pinned messages yet.',
                style: TextStyle(fontWeight: FontWeight.w700),
              ),
              SizedBox(height: 5),
              Text(
                'Pinned messages stay easy to find here.',
                textAlign: TextAlign.center,
                style: TextStyle(color: KaedeColors.muted),
              ),
            ],
          ),
        ),
      );
}

final class _DmCallRoom extends ConsumerWidget {
  const _DmCallRoom({required this.channel, required this.callRef});

  final KaedeChannel channel;
  final EntityRef callRef;

  @override
  Widget build(BuildContext context, WidgetRef ref) => Scaffold(
        appBar: AppBar(
          title: Text(channel.name ??
              (channel.recipients.isEmpty
                  ? 'Conversation call'
                  : channel.recipients.first.name)),
          actions: [
            Padding(
              padding: const EdgeInsets.only(right: 10),
              child: TextButton.icon(
                style: TextButton.styleFrom(
                  foregroundColor: KaedeColors.danger,
                ),
                icon: const Icon(Icons.call_end_rounded, size: 18),
                onPressed: () async {
                  try {
                    await ref
                        .read(mobileControllerProvider.notifier)
                        .repository
                        .callAction(callRef, 'end');
                    await ref.read(voiceSessionProvider).leave();
                    if (context.mounted) Navigator.of(context).pop();
                  } on Object catch (error) {
                    if (context.mounted) {
                      ScaffoldMessenger.of(context).showSnackBar(
                        SnackBar(content: Text(userFacingError(error))),
                      );
                    }
                  }
                },
                label: const Text('End call'),
              ),
            ),
          ],
        ),
        body: VoiceRoom(channel: channel, callRef: callRef),
      );
}

final class _GroupDmSettings extends ConsumerStatefulWidget {
  const _GroupDmSettings({required this.channel});

  final KaedeChannel channel;

  @override
  ConsumerState<_GroupDmSettings> createState() => _GroupDmSettingsState();
}

final class _GroupDmSettingsState extends ConsumerState<_GroupDmSettings> {
  late final TextEditingController _name;
  final _invite = TextEditingController();
  var _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _name = TextEditingController(text: widget.channel.name ?? '');
  }

  @override
  void dispose() {
    _name.dispose();
    _invite.dispose();
    super.dispose();
  }

  Future<void> _run(Future<void> Function() action) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await action();
      await ref.read(mobileControllerProvider.notifier).refreshNavigation();
    } on Object catch (error) {
      if (mounted) setState(() => _error = userFacingError(error));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    ref.watch(mobileControllerProvider.select((state) => (
          state.dms,
          state.user,
          state.e2eeActivationEnabled,
        )));
    final mobile = ref.read(mobileControllerProvider);
    var channel = widget.channel;
    for (final candidate in mobile.dms) {
      if (candidate.ref == widget.channel.ref) {
        channel = candidate;
        break;
      }
    }
    final current = mobile.user;
    final isOwner = current?.ref == channel.ownerRef;
    return SafeArea(
      child: Padding(
        padding: EdgeInsets.fromLTRB(
          20,
          6,
          20,
          MediaQuery.viewInsetsOf(context).bottom + 20,
        ),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                'Group settings',
                style: Theme.of(context).textTheme.headlineSmall,
              ),
              const SizedBox(height: 4),
              Text(
                '${channel.recipients.length + 1} members · anyone can add a '
                'friend',
                style: const TextStyle(
                  color: KaedeColors.muted,
                  fontSize: 12.5,
                ),
              ),
              const SizedBox(height: 20),
              TextField(
                controller: _name,
                maxLength: 100,
                decoration: const InputDecoration(
                  labelText: 'Group name',
                  counterText: '',
                ),
              ),
              const SizedBox(height: 8),
              Align(
                alignment: Alignment.centerRight,
                child: FilledButton.tonal(
                  onPressed: _busy
                      ? null
                      : () => _run(() async {
                            await ref
                                .read(mobileControllerProvider.notifier)
                                .repository
                                .renameGroupDm(
                                  channel.ref,
                                  _name.text.trim().isEmpty
                                      ? null
                                      : _name.text.trim(),
                                );
                          }),
                  child: const Text('Save name'),
                ),
              ),
              const SizedBox(height: 18),
              TextField(
                controller: _invite,
                onChanged: (_) => setState(() {}),
                decoration: const InputDecoration(
                  labelText: 'Add a friend',
                  hintText: '@friend@example.net',
                ),
              ),
              const SizedBox(height: 8),
              Align(
                alignment: Alignment.centerRight,
                child: FilledButton.tonalIcon(
                  onPressed: _busy || _invite.text.trim().isEmpty
                      ? null
                      : () => _run(() async {
                            await ref
                                .read(mobileControllerProvider.notifier)
                                .repository
                                .addGroupDmMember(
                                    channel.ref, _invite.text.trim());
                            _invite.clear();
                          }),
                  icon: const Icon(Icons.person_add_alt_1_rounded, size: 18),
                  label: const Text('Add member'),
                ),
              ),
              if (channel.encryptionMode == 'e2ee' ||
                  mobile.e2eeActivationEnabled) ...[
                const SizedBox(height: 18),
                Material(
                  color: KaedeColors.raised,
                  borderRadius: BorderRadius.circular(KaedeRadius.medium),
                  child: InkWell(
                    borderRadius: BorderRadius.circular(KaedeRadius.medium),
                    onTap: _busy
                        ? null
                        : () => _showE2eeRoomSettings(
                              context,
                              ref,
                              channel,
                              canManage: isOwner,
                            ),
                    child: Container(
                      padding: const EdgeInsets.fromLTRB(14, 12, 10, 12),
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(KaedeRadius.medium),
                        border: Border.all(color: KaedeColors.border),
                      ),
                      child: Row(
                        children: [
                          Icon(
                            channel.encryptionMode == 'e2ee'
                                ? Icons.lock_rounded
                                : Icons.lock_open_rounded,
                            size: 19,
                            color: channel.encryptionMode == 'e2ee'
                                ? KaedeColors.mint
                                : KaedeColors.muted,
                          ),
                          const SizedBox(width: 12),
                          Expanded(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                const Text(
                                  'End-to-end encryption',
                                  style: TextStyle(
                                    fontWeight: FontWeight.w600,
                                    fontSize: 14.5,
                                  ),
                                ),
                                Text(
                                  channel.encryptionMode == 'e2ee'
                                      ? channel.encryptionState == 'active'
                                          ? 'Active'
                                          : 'Paused until keys rotate'
                                      : 'Optional · review the tradeoffs '
                                          'before enabling',
                                  style: const TextStyle(
                                    color: KaedeColors.muted,
                                    fontSize: 12,
                                  ),
                                ),
                              ],
                            ),
                          ),
                          const Icon(Icons.chevron_right_rounded,
                              color: KaedeColors.muted),
                        ],
                      ),
                    ),
                  ),
                ),
              ],
              const SizedBox(height: 20),
              const Text(
                'MEMBERS',
                style: TextStyle(
                  color: KaedeColors.muted,
                  fontSize: 11,
                  fontWeight: FontWeight.w800,
                  letterSpacing: .9,
                ),
              ),
              const SizedBox(height: 4),
              if (current != null)
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  leading: UserAvatar(
                    user: current,
                    radius: 18,
                    ringColor: KaedeColors.panel,
                  ),
                  title: Text(current.name),
                  subtitle: const Text('You'),
                  trailing: current.ref == channel.ownerRef
                      ? const Chip(label: Text('Owner'))
                      : null,
                ),
              for (final member in channel.recipients)
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  leading: UserAvatar(
                    user: member,
                    radius: 18,
                    presence:
                        mobile.presenceByUser[member.ref] ?? member.presence,
                    ringColor: KaedeColors.panel,
                  ),
                  title: Text(member.name),
                  subtitle: Text(member.handle),
                  onTap: () => showUserProfile(
                    context,
                    member,
                    mobile.presenceByUser[member.ref] ?? member.presence,
                  ),
                  trailing: member.ref == channel.ownerRef
                      ? const Chip(label: Text('Owner'))
                      : isOwner
                          ? IconButton(
                              tooltip: 'Remove member',
                              onPressed: _busy
                                  ? null
                                  : () => _run(() => ref
                                      .read(mobileControllerProvider.notifier)
                                      .repository
                                      .removeGroupDmMember(
                                        channel.ref,
                                        member.ref,
                                      )),
                              icon: const Icon(Icons.person_remove_outlined),
                            )
                          : null,
                ),
              if (_error case final error?)
                Padding(
                  padding: const EdgeInsets.only(top: 10),
                  child: Container(
                    padding: const EdgeInsets.all(12),
                    decoration: BoxDecoration(
                      color: KaedeColors.dangerSoft,
                      borderRadius: BorderRadius.circular(KaedeRadius.medium),
                    ),
                    child: Text(
                      error,
                      style: const TextStyle(
                        color: KaedeColors.danger,
                        fontSize: 13,
                      ),
                    ),
                  ),
                ),
              const SizedBox(height: 16),
              OutlinedButton.icon(
                style: OutlinedButton.styleFrom(
                  foregroundColor: KaedeColors.danger,
                  side: const BorderSide(color: KaedeColors.dangerSoft),
                ),
                onPressed: _busy
                    ? null
                    : () => _run(() async {
                          await ref
                              .read(mobileControllerProvider.notifier)
                              .repository
                              .leaveGroupDm(channel.ref);
                          if (context.mounted) {
                            Navigator.of(context).pop();
                          }
                        }),
                icon: const Icon(Icons.logout_rounded),
                label: const Text('Leave group'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// The member list as its own route.
///
/// It used to be a third page of the shell's page view, which meant leftward
/// drags were shared between it and swipe-to-reply. Pushing it instead leaves
/// the page view with one gesture per direction.
final class _GuildMemberRoute extends ConsumerWidget {
  const _GuildMemberRoute({required this.guild});

  final KaedeGuild guild;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    // Keeps roles and the guild name live while the route is open, ignoring a
    // selection that moved on to some other guild underneath it.
    final live = ref.watch(mobileControllerProvider.select((state) =>
        state.activeGuild?.ref == guild.ref ? state.activeGuild : null));
    return Scaffold(
      body: _GuildMemberPane(
        guild: live ?? guild,
        onBack: () => Navigator.of(context).pop(),
      ),
    );
  }
}

final class _GuildMemberPane extends ConsumerStatefulWidget {
  const _GuildMemberPane({required this.guild, required this.onBack});

  final KaedeGuild guild;
  final VoidCallback onBack;

  @override
  ConsumerState<_GuildMemberPane> createState() => _GuildMemberPaneState();
}

final class _GuildMemberPaneState extends ConsumerState<_GuildMemberPane> {
  List<GuildMember>? _members;
  String? _error;
  var _partial = false;
  EntityRef? _rosterRequestedFor;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant _GuildMemberPane oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.guild.ref != widget.guild.ref) {
      _members = null;
      _error = null;
      _partial = false;
      _rosterRequestedFor = null;
      _load();
    }
  }

  Future<void> _load() async {
    final requestedGuild = widget.guild;
    final controller = ref.read(mobileControllerProvider.notifier);
    _requestRoster(controller, requestedGuild.ref);
    try {
      final members = await controller.repository.members(requestedGuild.ref);
      if (mounted && widget.guild.ref == requestedGuild.ref) {
        setState(() {
          _members = members;
          _error = null;
          _partial = false;
        });
      }
    } on Object catch (error) {
      if (mounted && widget.guild.ref == requestedGuild.ref) {
        final state = ref.read(mobileControllerProvider);
        final channelRefs =
            requestedGuild.channels.map((channel) => channel.ref).toSet();
        final known = <EntityRef, KaedeUser>{};
        if (state.user case final user?) known[user.ref] = user;
        for (final entry in state.messageStore.entries) {
          if (!channelRefs.contains(entry.key)) continue;
          for (final message in entry.value) {
            if (message.author case final user?) {
              known[user.ref] = user;
            }
          }
        }
        setState(() {
          _members = known.values
              .map((user) => GuildMember(user: user, roleIds: const <String>[]))
              .toList(growable: false);
          _error = _members!.isEmpty
              ? userFacingError(
                  error,
                  summary: 'Could not load the member list',
                )
              : null;
          _partial = _members!.isNotEmpty;
        });
      }
    }
  }

  /// The REST roster carries no presence; the gateway's member chunk does, so
  /// the pane asks for one as soon as realtime is available.
  void _requestRoster(MobileController controller, EntityRef guild) {
    if (!ref.read(mobileControllerProvider).gatewayHealth.isConnected) return;
    if (_rosterRequestedFor == guild) return;
    _rosterRequestedFor = guild;
    controller.requestGuildMembers(guild);
  }

  @override
  Widget build(BuildContext context) {
    ref.watch(mobileControllerProvider.select((state) => (
          state.gatewayHealth.isConnected,
          state.userProfiles,
          state.user,
          state.presenceByUser,
          state.presencePreference,
        )));
    final mobile = ref.read(mobileControllerProvider);
    if (mobile.gatewayHealth.isConnected &&
        _rosterRequestedFor != widget.guild.ref) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) {
          _requestRoster(
            ref.read(mobileControllerProvider.notifier),
            widget.guild.ref,
          );
        }
      });
    }
    final members = _members;
    final controller = ref.read(mobileControllerProvider.notifier);
    final groups = members == null
        ? const <MemberListSection>[]
        : groupGuildMembers(
            members: <GuildMember>[
              for (final member in members)
                GuildMember(
                  user: mobile.userProfiles[member.user.ref] ?? member.user,
                  roleIds: member.roleIds,
                  nickname: member.nickname,
                  timeoutUntil: member.timeoutUntil,
                ),
            ],
            roles: widget.guild.roles,
            presenceFor: controller.presenceFor,
          );
    final total = members?.length ?? 0;
    return ColoredBox(
      color: KaedeColors.sidebar,
      child: Column(
        children: [
          ConversationCompactHeader(
            leading: IconButton(
              onPressed: widget.onBack,
              icon: const Icon(Icons.arrow_back_rounded),
            ),
            title: 'Members',
            subtitle: members == null
                ? widget.guild.name
                : '$total in ${widget.guild.name}',
          ),
          Expanded(
            child: SafeArea(
              top: false,
              child: _error != null && members == null
                  ? _MemberListError(message: _error!, onRetry: _load)
                  : members == null
                      ? const Center(child: CircularProgressIndicator())
                      : RefreshIndicator(
                          onRefresh: _load,
                          child: CustomScrollView(
                            slivers: [
                              if (_partial)
                                const SliverToBoxAdapter(
                                  child: _StatusBanner(
                                    icon: Icons.info_outline_rounded,
                                    background: KaedeColors.warningSoft,
                                    foreground: KaedeColors.warning,
                                    title: 'Partial member list',
                                    subtitle:
                                        'Only members seen in cached messages '
                                        'are shown. Pull down to retry.',
                                  ),
                                ),
                              for (final section in groups) ...[
                                SliverToBoxAdapter(
                                  child: _SidebarSectionHeader(
                                    title: '${section.title} — '
                                        '${section.members.length}',
                                  ),
                                ),
                                SliverList.builder(
                                  itemCount: section.members.length,
                                  itemBuilder: (context, index) {
                                    final member = section.members[index];
                                    final user = member.user;
                                    return _MemberRow(
                                      user: user,
                                      presence: controller.presenceFor(user),
                                      nickname: member.nickname,
                                      nameColor: memberRoleColor(
                                        widget.guild,
                                        member,
                                      ),
                                      dimmed: section.offline,
                                      onTap: () => showUserProfile(
                                        context,
                                        user,
                                        controller.presenceFor(user),
                                        memberOf: widget.guild.name,
                                        actions: <Widget>[
                                          if (user.profileResolved &&
                                              user.ref != mobile.user?.ref)
                                            FilledButton.icon(
                                              onPressed: () {
                                                Navigator.pop(context);
                                                _openDm(
                                                  context,
                                                  ref,
                                                  user,
                                                  widget.onBack,
                                                );
                                              },
                                              icon: const Icon(Icons
                                                  .chat_bubble_outline_rounded),
                                              label: const Text('Message'),
                                            ),
                                        ],
                                      ),
                                    );
                                  },
                                ),
                              ],
                              const SliverToBoxAdapter(
                                child: SizedBox(height: 16),
                              ),
                            ],
                          ),
                        ),
            ),
          ),
        ],
      ),
    );
  }
}

/// One heading plus its members in the roster.
final class MemberListSection {
  const MemberListSection({
    required this.title,
    required this.members,
    required this.offline,
  });

  final String title;
  final List<GuildMember> members;
  final bool offline;
}

/// Groups a roster the way the web and desktop clients do: hoisted roles
/// highest-first, then everyone else who is around, then offline members.
List<MemberListSection> groupGuildMembers({
  required List<GuildMember> members,
  required List<KaedeRole> roles,
  required PresenceStatus Function(KaedeUser user) presenceFor,
}) {
  int rank(PresenceStatus status) => switch (status) {
        PresenceStatus.online => 0,
        PresenceStatus.idle => 1,
        PresenceStatus.dnd => 2,
        PresenceStatus.invisible || PresenceStatus.offline => 3,
      };
  bool isOffline(GuildMember member) {
    final presence = presenceFor(member.user);
    return presence == PresenceStatus.offline ||
        presence == PresenceStatus.invisible;
  }

  String label(GuildMember member) =>
      (member.nickname ?? member.user.name).toLowerCase();

  final sorted = [...members]..sort((left, right) {
      final difference =
          rank(presenceFor(left.user)) - rank(presenceFor(right.user));
      if (difference != 0) return difference;
      return label(left).compareTo(label(right));
    });
  final around = sorted.where((member) => !isOffline(member)).toList();
  final offline = sorted.where(isOffline).toList();
  final hoisted = [
    for (final role in roles)
      if (role.hoist && role.position > 0) role,
  ]..sort((left, right) => right.position.compareTo(left.position));

  final sections = <MemberListSection>[];
  final claimed = <EntityRef>{};
  for (final role in hoisted) {
    final group = <GuildMember>[];
    for (final member in around) {
      if (claimed.contains(member.user.ref)) continue;
      if (!member.roleIds.contains(role.ref.id.value)) continue;
      // A member only appears under their highest hoisted role.
      final highest = hoisted.firstWhere(
        (candidate) => member.roleIds.contains(candidate.ref.id.value),
        orElse: () => role,
      );
      if (highest.ref != role.ref) continue;
      group.add(member);
      claimed.add(member.user.ref);
    }
    if (group.isNotEmpty) {
      sections.add(MemberListSection(
        title: role.name,
        members: group,
        offline: false,
      ));
    }
  }
  final remaining =
      around.where((member) => !claimed.contains(member.user.ref)).toList();
  if (remaining.isNotEmpty) {
    sections.add(MemberListSection(
      title: 'Online',
      members: remaining,
      offline: false,
    ));
  }
  if (offline.isNotEmpty) {
    sections.add(MemberListSection(
      title: 'Offline',
      members: offline,
      offline: true,
    ));
  }
  return sections;
}

final class _MemberRow extends StatelessWidget {
  const _MemberRow({
    required this.user,
    required this.presence,
    required this.nickname,
    required this.dimmed,
    required this.onTap,
    this.nameColor,
  });

  final KaedeUser user;
  final PresenceStatus presence;
  final String? nickname;
  final bool dimmed;
  final VoidCallback onTap;
  final Color? nameColor;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 1),
        child: Material(
          color: Colors.transparent,
          borderRadius: BorderRadius.circular(KaedeRadius.medium),
          child: InkWell(
            onTap: onTap,
            borderRadius: BorderRadius.circular(KaedeRadius.medium),
            child: Opacity(
              opacity: dimmed ? .55 : 1,
              child: Padding(
                padding: const EdgeInsets.fromLTRB(8, 7, 10, 7),
                child: Row(
                  children: [
                    UserAvatar(user: user, radius: 17, presence: presence),
                    const SizedBox(width: 11),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            nickname ?? user.name,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              fontWeight: FontWeight.w600,
                              fontSize: 14.5,
                              color: nameColor,
                            ),
                          ),
                          if (user.customStatus?.trim().isNotEmpty == true)
                            Text(
                              user.customStatus!.trim(),
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(
                                color: KaedeColors.muted,
                                fontSize: 12,
                              ),
                            ),
                        ],
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      );
}

final class _MemberListError extends StatelessWidget {
  const _MemberListError({required this.message, required this.onRetry});

  final String message;
  final Future<void> Function() onRetry;

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(28),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.people_outline_rounded,
                  size: 34, color: KaedeColors.muted),
              const SizedBox(height: 12),
              Text(
                message,
                textAlign: TextAlign.center,
                style: const TextStyle(color: KaedeColors.muted),
              ),
              const SizedBox(height: 14),
              OutlinedButton.icon(
                onPressed: onRetry,
                icon: const Icon(Icons.refresh_rounded),
                label: const Text('Try again'),
              ),
            ],
          ),
        ),
      );
}

final class _ChatBrowser extends ConsumerWidget {
  const _ChatBrowser({
    required this.onOpenChannel,
    required this.onOpenFriends,
    required this.onOpenSettings,
  });

  final VoidCallback onOpenChannel;
  final VoidCallback onOpenFriends;
  final VoidCallback onOpenSettings;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    // Rebuild only when a slice the rail or conversation list actually
    // renders changes (guilds, DMs, selection, badges, presence, profiles).
    // High-frequency updates that don't affect this browser — composer
    // drafts, typing indicators, gateway health, per-channel message lists —
    // no longer tear down and rebuild the server rail and conversation rows.
    ref.watch(mobileControllerProvider.select((state) => (
          state.activeGuild,
          state.guilds,
          state.guildNavigation,
          state.selectedGuild,
          state.selectedChannel,
          state.dms,
          state.user,
          state.userProfiles,
          state.presenceByUser,
          state.presencePreference,
          state.relationships,
          state.unreadCounts,
          state.mentionCounts,
        )));
    final state = ref.read(mobileControllerProvider);
    return Row(
      children: [
        _ServerRail(
          state: state,
          onOpenHome: () =>
              ref.read(mobileControllerProvider.notifier).selectHome(),
          onOpenGuild: (guild) =>
              ref.read(mobileControllerProvider.notifier).selectGuild(guild),
          onAddGuild: () => _showGuildActions(context, ref),
        ),
        Expanded(
          child: state.activeGuild == null
              ? _DirectMessageBrowser(
                  state: state,
                  onOpenChannel: onOpenChannel,
                  onOpenFriends: onOpenFriends,
                  onOpenSettings: onOpenSettings,
                )
              : _GuildBrowser(
                  state: state,
                  guild: state.activeGuild!,
                  onOpenChannel: onOpenChannel,
                  onOpenSettings: onOpenSettings,
                ),
        ),
      ],
    );
  }
}

final class _ServerRail extends ConsumerWidget {
  const _ServerRail({
    required this.state,
    required this.onOpenHome,
    required this.onOpenGuild,
    required this.onAddGuild,
  });

  final MobileState state;
  final VoidCallback onOpenHome;
  final ValueChanged<KaedeGuild> onOpenGuild;
  final VoidCallback onAddGuild;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final navigation =
        reconcileGuildNavigation(state.guildNavigation, state.guilds);
    final guildByRef = <EntityRef, KaedeGuild>{
      for (final guild in state.guilds) guild.ref: guild,
    };
    return SizedBox(
      width: 72,
      child: ColoredBox(
        color: KaedeColors.rail,
        child: SafeArea(
          right: false,
          bottom: false,
          child: Column(
            children: [
              const SizedBox(height: 8),
              _RailButton(
                label: 'Direct messages',
                active: state.selectedGuild == null,
                onTap: onOpenHome,
                badge: state.dms.fold(
                  0,
                  (total, dm) => total + (state.mentionCounts[dm.ref] ?? 0),
                ),
                unread: state.dms.any(
                  (dm) => (state.unreadCounts[dm.ref] ?? 0) > 0,
                ),
                activeColor: KaedeColors.coral,
                child: Icon(
                  Icons.chat_bubble_rounded,
                  size: 23,
                  color: state.selectedGuild == null
                      ? KaedeColors.onCoral
                      : KaedeColors.textSoft,
                ),
              ),
              Container(
                margin: const EdgeInsets.fromLTRB(18, 2, 18, 8),
                height: 1,
                color: KaedeColors.border,
              ),
              Expanded(
                child: ReorderableListView.builder(
                  padding: EdgeInsets.zero,
                  buildDefaultDragHandles: false,
                  itemCount: navigation.items.length,
                  proxyDecorator: (child, index, animation) => Material(
                    color: Colors.transparent,
                    child: Opacity(opacity: .85, child: child),
                  ),
                  onReorder: (oldIndex, newIndex) => ref
                      .read(mobileControllerProvider.notifier)
                      .saveGuildNavigation(
                        reorderGuildNavigation(navigation, oldIndex, newIndex),
                      ),
                  itemBuilder: (context, index) {
                    final item = navigation.items[index];
                    return switch (item) {
                      GuildNavigationGuildItem() =>
                        ReorderableDelayedDragStartListener(
                          key: ValueKey('guild:${item.guild.wire}'),
                          index: index,
                          child: Builder(
                            builder: (context) {
                              final guild = guildByRef[item.guild];
                              return guild == null
                                  ? const SizedBox.shrink()
                                  : _RailButton(
                                      label: guild.name,
                                      active: guild.ref == state.selectedGuild,
                                      onTap: () => onOpenGuild(guild),
                                      badge: _guildMentions(state, guild),
                                      unread: _guildUnread(state, guild),
                                      child: GuildIcon(
                                        guild: guild,
                                        size: 48,
                                        borderRadius:
                                            guild.ref == state.selectedGuild
                                                ? 15
                                                : 24,
                                      ),
                                    );
                            },
                          ),
                        ),
                      GuildNavigationGroupItem() =>
                        ReorderableDelayedDragStartListener(
                          key: ValueKey('group:${item.id}'),
                          index: index,
                          child: _GuildRailFolder(
                            state: state,
                            group: item,
                            guildByRef: guildByRef,
                            onOpenGuild: onOpenGuild,
                            onToggle: () => ref
                                .read(mobileControllerProvider.notifier)
                                .saveGuildNavigation(
                                  updateGuildNavigationGroup(
                                    navigation,
                                    item.id,
                                    collapsed: !item.collapsed,
                                  ),
                                ),
                          ),
                        ),
                    };
                  },
                ),
              ),
              _RailButton(
                label: 'Add a guild',
                active: false,
                onTap: onAddGuild,
                idleColor: KaedeColors.rail,
                border: true,
                child: const Icon(Icons.add_rounded,
                    color: KaedeColors.mint, size: 26),
              ),
              _RailButton(
                label: 'Organize guilds',
                active: false,
                onTap: () => showModalBottomSheet<void>(
                  context: context,
                  isScrollControlled: true,
                  showDragHandle: true,
                  builder: (_) => _GuildOrganizerSheet(
                    initial: navigation,
                    guilds: state.guilds,
                  ),
                ),
                idleColor: KaedeColors.rail,
                border: true,
                child: const Icon(Icons.create_new_folder_outlined,
                    size: 21, color: KaedeColors.muted),
              ),
              const SizedBox(height: 4),
            ],
          ),
        ),
      ),
    );
  }
}

final class _GuildRailFolder extends StatelessWidget {
  const _GuildRailFolder({
    required this.state,
    required this.group,
    required this.guildByRef,
    required this.onOpenGuild,
    required this.onToggle,
  });

  final MobileState state;
  final GuildNavigationGroupItem group;
  final Map<EntityRef, KaedeGuild> guildByRef;
  final ValueChanged<KaedeGuild> onOpenGuild;
  final VoidCallback onToggle;

  @override
  Widget build(BuildContext context) {
    final guilds = group.guilds
        .map((ref) => guildByRef[ref])
        .whereType<KaedeGuild>()
        .toList();
    final mentions =
        guilds.fold(0, (total, guild) => total + _guildMentions(state, guild));
    final holdsSelection =
        guilds.any((guild) => guild.ref == state.selectedGuild);
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      padding: const EdgeInsets.symmetric(vertical: 3),
      decoration: BoxDecoration(
        color: group.collapsed
            ? Colors.transparent
            : KaedeColors.panel.withValues(alpha: .72),
        borderRadius: BorderRadius.circular(20),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          _RailButton(
            label: group.name,
            active: holdsSelection && group.collapsed,
            onTap: onToggle,
            badge: group.collapsed ? mentions : 0,
            unread: group.collapsed &&
                guilds.any((guild) => _guildUnread(state, guild)),
            child: Icon(
              group.collapsed
                  ? Icons.folder_rounded
                  : Icons.folder_open_rounded,
              color: KaedeColors.coralText,
              size: 25,
            ),
          ),
          if (!group.collapsed)
            for (final guild in guilds)
              _RailButton(
                label: guild.name,
                active: guild.ref == state.selectedGuild,
                onTap: () => onOpenGuild(guild),
                badge: _guildMentions(state, guild),
                unread: _guildUnread(state, guild),
                size: 42,
                child: GuildIcon(
                  guild: guild,
                  size: 42,
                  borderRadius: guild.ref == state.selectedGuild ? 13 : 21,
                ),
              ),
        ],
      ),
    );
  }
}

final class _GuildGroupDraft {
  const _GuildGroupDraft(this.name, this.guilds);

  final String name;
  final List<EntityRef> guilds;
}

final class _GuildOrganizerSheet extends ConsumerStatefulWidget {
  const _GuildOrganizerSheet({required this.initial, required this.guilds});

  final GuildNavigation initial;
  final List<KaedeGuild> guilds;

  @override
  ConsumerState<_GuildOrganizerSheet> createState() =>
      _GuildOrganizerSheetState();
}

final class _GuildOrganizerSheetState
    extends ConsumerState<_GuildOrganizerSheet> {
  late GuildNavigation _navigation;

  @override
  void initState() {
    super.initState();
    _navigation = widget.initial;
  }

  Map<EntityRef, KaedeGuild> get _guildByRef => <EntityRef, KaedeGuild>{
        for (final guild in widget.guilds) guild.ref: guild,
      };

  Future<void> _editGroup([GuildNavigationGroupItem? existing]) async {
    final name = TextEditingController(text: existing?.name ?? 'Guild group');
    final selected = <EntityRef>{...?existing?.guilds};
    final draft = await showDialog<_GuildGroupDraft>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          title: Text(
              existing == null ? 'Create guild group' : 'Edit guild group'),
          content: SizedBox(
            width: 420,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(
                  controller: name,
                  autofocus: true,
                  maxLength: 32,
                  decoration: const InputDecoration(labelText: 'Group name'),
                ),
                const SizedBox(height: 8),
                Flexible(
                  child: ListView(
                    shrinkWrap: true,
                    children: [
                      for (final guild in widget.guilds)
                        CheckboxListTile(
                          value: selected.contains(guild.ref),
                          secondary: GuildIcon(guild: guild, size: 34),
                          title: Text(guild.name),
                          subtitle: Text(guild.ref.domain.value),
                          onChanged: (checked) => setDialogState(() {
                            if (checked == true) {
                              selected.add(guild.ref);
                            } else {
                              selected.remove(guild.ref);
                            }
                          }),
                        ),
                    ],
                  ),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: name.text.trim().isEmpty || selected.isEmpty
                  ? null
                  : () => Navigator.pop(
                        context,
                        _GuildGroupDraft(name.text.trim(), selected.toList()),
                      ),
              child: const Text('Save group'),
            ),
          ],
        ),
      ),
    );
    name.dispose();
    if (draft == null || !mounted) return;
    setState(() {
      _navigation = existing == null
          ? createGuildNavigationGroup(
              _navigation,
              const Uuid().v4(),
              draft.name,
              draft.guilds,
            )
          : replaceGuildNavigationGroup(
              _navigation,
              existing.id,
              draft.name,
              draft.guilds,
            );
    });
  }

  @override
  Widget build(BuildContext context) {
    final guildByRef = _guildByRef;
    return SafeArea(
      child: SizedBox(
        height: MediaQuery.sizeOf(context).height * .84,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(18, 0, 18, 18),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                'Organize guilds',
                style: TextStyle(fontSize: 24, fontWeight: FontWeight.w900),
              ),
              const SizedBox(height: 4),
              const Text(
                'Press and hold a row, then drag it. Groups and ordering sync to web and desktop.',
                style: TextStyle(color: KaedeColors.muted),
              ),
              const SizedBox(height: 12),
              Expanded(
                child: ReorderableListView.builder(
                  itemCount: _navigation.items.length,
                  onReorder: (oldIndex, newIndex) => setState(() {
                    _navigation = reorderGuildNavigation(
                      _navigation,
                      oldIndex,
                      newIndex,
                    );
                  }),
                  itemBuilder: (context, index) {
                    final item = _navigation.items[index];
                    if (item is GuildNavigationGuildItem) {
                      final guild = guildByRef[item.guild];
                      return ListTile(
                        key: ValueKey('organize-guild:${item.guild.wire}'),
                        leading: guild == null
                            ? const Icon(Icons.public_off_rounded)
                            : GuildIcon(guild: guild, size: 42),
                        title: Text(guild?.name ?? 'Unavailable guild'),
                        subtitle: Text(item.guild.domain.value),
                        trailing: const Icon(Icons.drag_handle_rounded),
                      );
                    }
                    final group = item as GuildNavigationGroupItem;
                    final names = group.guilds
                        .map((ref) => guildByRef[ref]?.name)
                        .whereType<String>()
                        .join(', ');
                    return ListTile(
                      key: ValueKey('organize-group:${group.id}'),
                      leading: const CircleAvatar(
                        backgroundColor: KaedeColors.raised,
                        child: Icon(Icons.folder_rounded,
                            color: KaedeColors.coral),
                      ),
                      title: Text(group.name),
                      subtitle: Text(names),
                      trailing: PopupMenuButton<String>(
                        onSelected: (value) {
                          if (value == 'edit') {
                            _editGroup(group);
                          } else if (value == 'ungroup') {
                            setState(() {
                              _navigation =
                                  ungroupGuildNavigation(_navigation, group.id);
                            });
                          }
                        },
                        itemBuilder: (context) => const [
                          PopupMenuItem(
                              value: 'edit', child: Text('Edit group')),
                          PopupMenuItem(
                              value: 'ungroup', child: Text('Ungroup guilds')),
                        ],
                      ),
                    );
                  },
                ),
              ),
              const SizedBox(height: 12),
              Row(
                children: [
                  OutlinedButton.icon(
                    onPressed: _editGroup,
                    icon: const Icon(Icons.create_new_folder_outlined),
                    label: const Text('Create group'),
                  ),
                  const Spacer(),
                  FilledButton.icon(
                    onPressed: () async {
                      await ref
                          .read(mobileControllerProvider.notifier)
                          .saveGuildNavigation(_navigation);
                      if (context.mounted) Navigator.pop(context);
                    },
                    icon: const Icon(Icons.sync_rounded),
                    label: const Text('Save'),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

final class _DirectMessageBrowser extends ConsumerWidget {
  const _DirectMessageBrowser({
    required this.state,
    required this.onOpenChannel,
    required this.onOpenFriends,
    required this.onOpenSettings,
  });

  final MobileState state;
  final VoidCallback onOpenChannel;
  final VoidCallback onOpenFriends;
  final VoidCallback onOpenSettings;

  void _openSearch(BuildContext context, WidgetRef ref) {
    Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (searchContext) => MessageSearchScreen(
          repository: ref.read(mobileControllerProvider.notifier).repository,
          scope: 'dms',
          scopeRef: null,
          channel: null,
          accountRef:
              ref.read(mobileControllerProvider.notifier).api.tokens?.userRef,
          users: messageSearchUserCandidates(<KaedeUser?>[
            state.user,
            ...state.userProfiles.values,
            for (final channel in state.dms) ...channel.recipients,
          ]),
          onJump: (result) async {
            final controller = ref.read(mobileControllerProvider.notifier);
            final opened = await controller.selectAndJumpToMessage(
              result.channel,
              result.message.ref,
              shouldContinue: () => messageSearchRouteCanDismiss(searchContext),
            );
            if (!searchContext.mounted ||
                !opened ||
                !messageSearchRouteCanDismiss(searchContext)) {
              return;
            }
            Navigator.of(searchContext).pop();
            onOpenChannel();
          },
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final pendingRequests = state.relationships
        .where((item) => '${item['type']}' == 'pending_in')
        .length;
    return ColoredBox(
      color: KaedeColors.sidebar,
      child: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 10, 4),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    'Messages',
                    style: Theme.of(context).textTheme.headlineMedium,
                  ),
                ),
                _SquareAction(
                  tooltip: 'New conversation',
                  icon: Icons.edit_square,
                  filled: true,
                  onTap: () => _newConversationAction(
                    context,
                    ref,
                    state,
                    onOpenChannel,
                  ),
                ),
              ],
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
            child: _SearchField(
              hint: 'Search messages',
              onTap: () => _openSearch(context, ref),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 4, 12, 2),
            child: _NavRow(
              icon: Icons.people_alt_rounded,
              title: 'Friends',
              subtitle: pendingRequests > 0
                  ? '$pendingRequests waiting on you'
                  : null,
              badge: pendingRequests,
              onTap: onOpenFriends,
            ),
          ),
          _SidebarSectionHeader(
            title: 'Direct messages',
            trailing: IconButton(
              tooltip: 'New conversation',
              visualDensity: VisualDensity.compact,
              onPressed: () => _newConversationAction(
                context,
                ref,
                state,
                onOpenChannel,
              ),
              icon: const Icon(Icons.add_rounded, size: 18),
            ),
          ),
          Expanded(
            child: state.dms.isEmpty
                ? const _EmptyNavigation(
                    icon: Icons.chat_bubble_outline_rounded,
                    title: 'No conversations yet',
                    body: 'Start a message with a friend to see it here.',
                  )
                : ListView.builder(
                    padding: const EdgeInsets.fromLTRB(8, 0, 8, 12),
                    itemCount: state.dms.length,
                    itemBuilder: (context, index) {
                      final dm = state.dms[index];
                      final users = dm.recipients
                          .where((user) => user.ref != state.user?.ref)
                          .toList();
                      final group = dm.conversationType == 'group';
                      final title = users.isEmpty
                          ? 'Conversation'
                          : group
                              ? (dm.name?.trim().isNotEmpty == true
                                  ? dm.name!.trim()
                                  : users
                                      .map((user) => user.name)
                                      .take(3)
                                      .join(', '))
                              : users.first.name;
                      final person = users.isEmpty ? null : users.first;
                      final presence = person == null
                          ? null
                          : state.presenceByUser[person.ref] ?? person.presence;
                      return _ConversationRow(
                        avatar: _DmAvatar(
                          channel: dm,
                          self: state.user,
                          presence: presence,
                        ),
                        title: title,
                        subtitle: group
                            ? '${dm.recipients.length + 1} members'
                            : person?.customStatus?.trim().isNotEmpty == true
                                ? person!.customStatus!.trim()
                                : presence == null
                                    ? 'Direct message'
                                    : presenceLabel(presence),
                        active: dm.ref == state.selectedChannel,
                        unread: state.unreadCounts[dm.ref] ?? 0,
                        mentions: state.mentionCounts[dm.ref] ?? 0,
                        onTap: () {
                          onOpenChannel();
                          unawaited(ref
                              .read(mobileControllerProvider.notifier)
                              .selectDm(dm));
                        },
                      );
                    },
                  ),
          ),
          _AccountBar(
            user: state.user,
            presence: state.presencePreference,
            onTap: onOpenSettings,
          ),
        ],
      ),
    );
  }
}

/// Tappable search affordance that looks like a field but opens the full
/// search screen, avoiding a second focusable input in the sidebar.
final class _SearchField extends StatelessWidget {
  const _SearchField({required this.hint, required this.onTap});

  final String hint;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => Material(
        color: KaedeColors.canvas,
        borderRadius: BorderRadius.circular(KaedeRadius.small),
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(KaedeRadius.small),
          child: Container(
            height: 38,
            padding: const EdgeInsets.symmetric(horizontal: 10),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(KaedeRadius.small),
              border: Border.all(color: KaedeColors.border),
            ),
            child: Row(
              children: [
                const Icon(Icons.search_rounded,
                    size: 17, color: KaedeColors.muted),
                const SizedBox(width: 8),
                Text(
                  hint,
                  style: const TextStyle(
                    color: KaedeColors.muted,
                    fontSize: 13.5,
                  ),
                ),
              ],
            ),
          ),
        ),
      );
}

/// Uppercase group label used above sidebar lists.
final class _SidebarSectionHeader extends StatelessWidget {
  const _SidebarSectionHeader({required this.title, this.trailing});

  final String title;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) => Padding(
        padding: EdgeInsets.fromLTRB(18, 14, trailing == null ? 18 : 8, 2),
        child: Row(
          children: [
            Expanded(
              child: Text(
                title.toUpperCase(),
                style: const TextStyle(
                  color: KaedeColors.muted,
                  fontSize: 11,
                  fontWeight: FontWeight.w800,
                  letterSpacing: .9,
                ),
              ),
            ),
            if (trailing case final action?) action,
          ],
        ),
      );
}

final class _GuildBrowser extends ConsumerWidget {
  const _GuildBrowser({
    required this.state,
    required this.guild,
    required this.onOpenChannel,
    required this.onOpenSettings,
  });

  final MobileState state;
  final KaedeGuild guild;
  final VoidCallback onOpenChannel;
  final VoidCallback onOpenSettings;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final controller = ref.read(mobileControllerProvider.notifier);
    final localGuild = controller.api.tokens?.instance == guild.ref.domain;
    final canManageChannels = localGuild &&
        (state.user?.ref == guild.ownerRef ||
            guild.allows(Permission.manageChannels));
    final canCreateInvite = localGuild &&
        (state.user?.ref == guild.ownerRef ||
            guild.allows(Permission.createInvite));
    final banner = publicAssetUri(guild.ref.domain, guild.bannerHash,
        variant: 'thumbnail_1024');
    final channels = [...guild.channels]
      ..sort((left, right) => left.position.compareTo(right.position));
    final children = <EntityRef, List<KaedeChannel>>{};
    for (final channel in channels) {
      if (channel.parentRef case final parent?) {
        (children[parent] ??= <KaedeChannel>[]).add(channel);
      }
    }
    return ColoredBox(
      color: KaedeColors.sidebar,
      child: Column(
        children: [
          _GuildHeader(
            guild: guild,
            banner: banner,
            onSettings: localGuild
                ? () => Navigator.of(context).push(
                      MaterialPageRoute<void>(
                        builder: (_) => GuildManagementScreen(guild: guild),
                      ),
                    )
                : null,
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 10, 12, 2),
            child: Row(
              children: [
                Expanded(
                  child: _SearchField(
                    hint: 'Search ${guild.name}',
                    onTap: () => Navigator.of(context).push(
                      MaterialPageRoute<void>(
                        builder: (searchContext) => MessageSearchScreen(
                          repository: controller.repository,
                          scope: 'guild',
                          scopeRef: guild.ref,
                          channel: null,
                          accountRef: controller.api.tokens?.userRef,
                          users: messageSearchUserCandidates(<KaedeUser?>[
                            state.user,
                            ...state.userProfiles.values,
                          ]),
                          onJump: (result) async {
                            final opened =
                                await controller.selectAndJumpToMessage(
                              result.channel,
                              result.message.ref,
                              shouldContinue: () =>
                                  messageSearchRouteCanDismiss(searchContext),
                            );
                            if (!searchContext.mounted ||
                                !opened ||
                                !messageSearchRouteCanDismiss(searchContext)) {
                              return;
                            }
                            Navigator.of(searchContext).pop();
                            onOpenChannel();
                          },
                        ),
                      ),
                    ),
                  ),
                ),
                if (canCreateInvite) ...[
                  const SizedBox(width: 8),
                  _SquareAction(
                    tooltip: 'Invite people',
                    icon: Icons.person_add_alt_1_rounded,
                    size: 38,
                    onTap: () async {
                      final targets = guildTextChannelTargets(channels);
                      if (targets.isEmpty) {
                        ScaffoldMessenger.of(context).showSnackBar(
                          const SnackBar(
                            content: Text(
                              'Create a text or announcement channel before '
                              'creating an invite.',
                            ),
                          ),
                        );
                        return;
                      }
                      final channel = await showGuildTextChannelPicker(
                        context,
                        channels: targets,
                        title: 'Invite people to…',
                      );
                      if (channel == null || !context.mounted) return;
                      _createAndShowInvite(context, controller, guild, channel);
                    },
                  ),
                ],
              ],
            ),
          ),
          GuildChannelsHeader(
            onAddChannel: canManageChannels
                ? () => _createGuildChannel(
                      context,
                      controller,
                      guild,
                      channels,
                    )
                : null,
          ),
          Expanded(
            child: ListView(
              padding: const EdgeInsets.fromLTRB(8, 0, 8, 14),
              children: [
                for (final channel in channels)
                  if (channel.parentRef == null)
                    if (channel.type == ChannelType.category)
                      _CategoryGroup(
                        category: channel,
                        children:
                            children[channel.ref] ?? const <KaedeChannel>[],
                        state: state,
                        onOpen: (child) =>
                            _openChannel(controller, child, onOpenChannel),
                      )
                    else
                      _ChannelRow(
                        channel: channel,
                        state: state,
                        onTap: () =>
                            _openChannel(controller, channel, onOpenChannel),
                      ),
              ],
            ),
          ),
          _AccountBar(
            user: state.user,
            presence: state.presencePreference,
            onTap: onOpenSettings,
          ),
        ],
      ),
    );
  }
}

/// Guild banner with the name overlaid, collapsing to a flat header when the
/// guild has no banner set.
final class _GuildHeader extends StatelessWidget {
  const _GuildHeader({
    required this.guild,
    required this.banner,
    required this.onSettings,
  });

  final KaedeGuild guild;
  final Uri? banner;
  final VoidCallback? onSettings;

  @override
  Widget build(BuildContext context) => SizedBox(
        height: banner == null ? 66 : 128,
        child: Stack(
          fit: StackFit.expand,
          children: [
            if (banner != null)
              CachedNetworkImage(
                imageUrl: '$banner',
                fit: BoxFit.cover,
                errorWidget: (_, __, ___) => const SizedBox.shrink(),
              ),
            if (banner != null)
              const DecoratedBox(
                decoration: BoxDecoration(
                  gradient: LinearGradient(
                    begin: Alignment.topCenter,
                    end: Alignment.bottomCenter,
                    colors: [Color(0x33000000), Color(0xF01D1B19)],
                  ),
                ),
              ),
            Positioned(
              left: 16,
              right: 8,
              bottom: 0,
              top: banner == null ? 0 : null,
              child: Row(
                children: [
                  if (banner == null) ...[
                    GuildIcon(guild: guild, size: 34, borderRadius: 11),
                    const SizedBox(width: 10),
                  ],
                  Expanded(
                    child: Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          guild.name,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            fontSize: banner == null ? 17 : 21,
                            fontWeight: FontWeight.w800,
                            letterSpacing: -.3,
                          ),
                        ),
                        Text(
                          guild.description?.trim().isNotEmpty == true
                              ? guild.description!.trim()
                              : guild.ref.domain.value,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: KaedeColors.muted,
                            fontSize: 11.5,
                          ),
                        ),
                      ],
                    ),
                  ),
                  if (onSettings != null)
                    IconButton(
                      tooltip: 'Guild settings',
                      onPressed: onSettings,
                      style: IconButton.styleFrom(
                        backgroundColor: banner == null
                            ? Colors.transparent
                            : Colors.black26,
                      ),
                      icon: const Icon(Icons.settings_rounded, size: 19),
                    ),
                ],
              ),
            ),
            if (banner != null)
              const Positioned(
                left: 0,
                right: 0,
                bottom: 0,
                child: Divider(height: 1, color: KaedeColors.border),
              ),
          ],
        ),
      );
}

final class GuildChannelsHeader extends StatelessWidget {
  const GuildChannelsHeader({
    this.onAddChannel,
    super.key,
  });

  final VoidCallback? onAddChannel;

  @override
  Widget build(BuildContext context) => Padding(
        padding: EdgeInsets.fromLTRB(18, 12, onAddChannel == null ? 18 : 8, 0),
        child: Row(
          children: [
            const Expanded(
              child: Text(
                'Channels',
                style: TextStyle(
                  color: KaedeColors.muted,
                  fontSize: 11,
                  fontWeight: FontWeight.w800,
                  letterSpacing: .9,
                ),
              ),
            ),
            if (onAddChannel != null)
              TextButton.icon(
                key: const ValueKey('guild-add-channel-button'),
                style: TextButton.styleFrom(
                  minimumSize: const Size(0, 36),
                  padding: const EdgeInsets.symmetric(horizontal: 8),
                  visualDensity: VisualDensity.compact,
                  foregroundColor: KaedeColors.muted,
                  textStyle: const TextStyle(
                    fontSize: 11.5,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                onPressed: onAddChannel,
                icon: const Icon(Icons.add_rounded, size: 16),
                label: const Text('Add channel'),
              ),
          ],
        ),
      );
}

Future<void> _createGuildChannel(
  BuildContext context,
  MobileController controller,
  KaedeGuild guild,
  List<KaedeChannel> channels,
) async {
  final draft = await showGuildChannelEditorSheet(
    context,
    channels: channels,
  );
  if (draft == null || !context.mounted) return;
  try {
    final created = await controller.createGuildChannel(guild.ref, draft.json);
    if (!context.mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text('#${created.name ?? draft.name} created')),
    );
  } on Object catch (error) {
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(userFacingError(
            error,
            summary: 'Could not create the channel',
          )),
          backgroundColor: KaedeColors.danger,
        ),
      );
    }
  }
}

void _openChannel(MobileController controller, KaedeChannel channel,
    VoidCallback onOpenChannel) {
  onOpenChannel();
  unawaited(controller.selectChannel(channel));
}

final class _CategoryGroup extends StatefulWidget {
  const _CategoryGroup({
    required this.category,
    required this.children,
    required this.state,
    required this.onOpen,
  });

  final KaedeChannel category;
  final List<KaedeChannel> children;
  final MobileState state;
  final ValueChanged<KaedeChannel> onOpen;

  @override
  State<_CategoryGroup> createState() => _CategoryGroupState();
}

final class _CategoryGroupState extends State<_CategoryGroup> {
  var expanded = true;

  /// Channels with activity stay visible even while the category is closed,
  /// so collapsing never hides something unread.
  bool _keepVisible(KaedeChannel channel) =>
      channel.ref == widget.state.selectedChannel ||
      (widget.state.unreadCounts[channel.ref] ?? 0) > 0 ||
      (widget.state.mentionCounts[channel.ref] ?? 0) > 0;

  @override
  Widget build(BuildContext context) {
    final visible = expanded
        ? widget.children
        : widget.children.where(_keepVisible).toList(growable: false);
    return Column(
      children: [
        InkWell(
          borderRadius: BorderRadius.circular(KaedeRadius.small),
          onTap: () => setState(() => expanded = !expanded),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(8, 14, 8, 4),
            child: Row(
              children: [
                AnimatedRotation(
                  turns: expanded ? 0 : -.25,
                  duration: const Duration(milliseconds: 150),
                  child: const Icon(Icons.keyboard_arrow_down_rounded,
                      size: 16, color: KaedeColors.muted),
                ),
                const SizedBox(width: 4),
                Expanded(
                  child: Text(
                    (widget.category.name ?? 'Category').toUpperCase(),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: KaedeColors.muted,
                      fontSize: 11,
                      fontWeight: FontWeight.w800,
                      letterSpacing: .9,
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
        for (final channel in visible)
          _ChannelRow(
            channel: channel,
            state: widget.state,
            onTap: () => widget.onOpen(channel),
          ),
      ],
    );
  }
}

final class _ChannelRow extends StatelessWidget {
  const _ChannelRow(
      {required this.channel, required this.state, required this.onTap});

  final KaedeChannel channel;
  final MobileState state;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final unread = state.unreadCounts[channel.ref] ?? 0;
    final mentions = state.mentionCounts[channel.ref] ?? 0;
    final active = channel.ref == state.selectedChannel;
    final highlighted = unread > 0 || mentions > 0;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 1),
      child: Material(
        color: active ? KaedeColors.selected : Colors.transparent,
        borderRadius: BorderRadius.circular(KaedeRadius.small),
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(KaedeRadius.small),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(6, 9, 10, 9),
            child: Row(
              children: [
                _UnreadMarker(visible: highlighted && !active),
                const SizedBox(width: 6),
                Icon(
                  channel.type == ChannelType.voice
                      ? Icons.volume_up_rounded
                      : channel.type == ChannelType.announcement
                          ? Icons.campaign_rounded
                          : Icons.tag_rounded,
                  size: 19,
                  color: highlighted || active
                      ? KaedeColors.text
                      : KaedeColors.muted,
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    channel.name ?? 'channel',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: 14.5,
                      color: active
                          ? KaedeColors.text
                          : highlighted
                              ? KaedeColors.text
                              : KaedeColors.muted,
                      fontWeight: highlighted || active
                          ? FontWeight.w600
                          : FontWeight.w500,
                    ),
                  ),
                ),
                if (channel.encryptionMode == 'e2ee') ...[
                  const Icon(Icons.lock_rounded,
                      size: 13, color: KaedeColors.muted),
                  const SizedBox(width: 4),
                ],
                _ChannelUnread(unread: unread, mentions: mentions),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

final class _ConversationRow extends StatelessWidget {
  const _ConversationRow({
    required this.avatar,
    required this.title,
    required this.subtitle,
    required this.active,
    required this.unread,
    required this.mentions,
    required this.onTap,
  });

  final Widget avatar;
  final String title;
  final String subtitle;
  final bool active;
  final int unread;
  final int mentions;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final highlighted = unread > 0 || mentions > 0;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 1),
      child: Material(
        color: active ? KaedeColors.selected : Colors.transparent,
        borderRadius: BorderRadius.circular(KaedeRadius.medium),
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(KaedeRadius.medium),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(8, 8, 10, 8),
            child: Row(
              children: [
                SizedBox.square(dimension: 40, child: avatar),
                const SizedBox(width: 11),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        title,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          fontWeight:
                              highlighted ? FontWeight.w700 : FontWeight.w600,
                          fontSize: 15,
                          color: highlighted || active
                              ? KaedeColors.text
                              : KaedeColors.textSoft,
                        ),
                      ),
                      const SizedBox(height: 1),
                      Text(
                        subtitle,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: KaedeColors.muted,
                          fontSize: 12.5,
                        ),
                      ),
                    ],
                  ),
                ),
                _DmUnread(unread: unread, mentions: mentions),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

final class _NavRow extends StatelessWidget {
  const _NavRow({
    required this.icon,
    required this.title,
    required this.onTap,
    this.subtitle,
    this.badge = 0,
  });

  final IconData icon;
  final String title;
  final String? subtitle;
  final VoidCallback onTap;
  final int badge;

  @override
  Widget build(BuildContext context) => Material(
        color: KaedeColors.panel,
        borderRadius: BorderRadius.circular(KaedeRadius.medium),
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(KaedeRadius.medium),
          child: Container(
            padding: const EdgeInsets.fromLTRB(12, 10, 12, 10),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(KaedeRadius.medium),
              border: Border.all(color: KaedeColors.border),
            ),
            child: Row(
              children: [
                Icon(icon, color: KaedeColors.muted, size: 20),
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        title,
                        style: const TextStyle(
                          fontWeight: FontWeight.w600,
                          fontSize: 14.5,
                        ),
                      ),
                      if (subtitle case final detail?)
                        Text(
                          detail,
                          style: const TextStyle(
                            color: KaedeColors.coralText,
                            fontSize: 11.5,
                          ),
                        ),
                    ],
                  ),
                ),
                if (badge > 0)
                  Badge(label: Text(badge > 99 ? '99+' : '$badge')),
              ],
            ),
          ),
        ),
      );
}

final class _SquareAction extends StatelessWidget {
  const _SquareAction({
    required this.tooltip,
    required this.icon,
    required this.onTap,
    this.filled = false,
    this.size = 40,
  });

  final String tooltip;
  final IconData icon;
  final VoidCallback onTap;
  final bool filled;
  final double size;

  @override
  Widget build(BuildContext context) => Tooltip(
        message: tooltip,
        child: Material(
          color: filled ? KaedeColors.coral : KaedeColors.panel,
          borderRadius: BorderRadius.circular(KaedeRadius.medium),
          child: InkWell(
            onTap: onTap,
            borderRadius: BorderRadius.circular(KaedeRadius.medium),
            child: Container(
              width: size,
              height: size,
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(KaedeRadius.medium),
                border: filled ? null : Border.all(color: KaedeColors.border),
              ),
              child: Icon(
                icon,
                size: 19,
                color: filled ? KaedeColors.onCoral : KaedeColors.textSoft,
              ),
            ),
          ),
        ),
      );
}

final class _AccountBar extends ConsumerWidget {
  const _AccountBar(
      {required this.user, required this.presence, required this.onTap});

  final KaedeUser? user;
  final PresenceStatus presence;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final voice = ref.watch(voiceSessionProvider);
    return SafeArea(
      top: false,
      child: DecoratedBox(
        decoration: const BoxDecoration(
          color: KaedeColors.canvas,
          border: Border(top: BorderSide(color: KaedeColors.border)),
        ),
        child: Row(
          children: [
            Expanded(
              child: InkWell(
                onTap: () => _showPresenceMenu(context, ref),
                child: Padding(
                  padding: const EdgeInsets.fromLTRB(10, 8, 6, 8),
                  child: Row(
                    children: [
                      if (user != null)
                        UserAvatar(
                          user: user!,
                          radius: 17,
                          presence: presence,
                          ringColor: KaedeColors.canvas,
                        )
                      else
                        const CircleAvatar(
                          radius: 17,
                          backgroundColor: KaedeColors.raised,
                          child: Icon(Icons.person_rounded, size: 18),
                        ),
                      const SizedBox(width: 9),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              user?.name ?? 'Account',
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(
                                fontWeight: FontWeight.w700,
                                fontSize: 13.5,
                              ),
                            ),
                            Text(
                              presenceLabel(presence),
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(
                                color: KaedeColors.muted,
                                fontSize: 11,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
            if (voice.joined)
              IconButton(
                tooltip: voice.muted ? 'Unmute' : 'Mute',
                visualDensity: VisualDensity.compact,
                onPressed: voice.canSpeak
                    ? () => _runVisibleAction(
                          context,
                          'Could not change the microphone state',
                          voice.toggleMute,
                        )
                    : null,
                style: IconButton.styleFrom(
                  foregroundColor:
                      voice.muted ? KaedeColors.danger : KaedeColors.textSoft,
                ),
                icon: Icon(
                  voice.muted ? Icons.mic_off_rounded : Icons.mic_rounded,
                  size: 19,
                ),
              ),
            IconButton(
              tooltip: 'Settings',
              visualDensity: VisualDensity.compact,
              onPressed: onTap,
              icon: const Icon(Icons.settings_rounded, size: 19),
            ),
            const SizedBox(width: 2),
          ],
        ),
      ),
    );
  }

  Future<void> _showPresenceMenu(BuildContext context, WidgetRef ref) async {
    await showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      builder: (sheetContext) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (user case final account?)
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 0, 20, 12),
                child: Row(
                  children: [
                    UserAvatar(
                      user: account,
                      radius: 21,
                      presence: presence,
                      ringColor: KaedeColors.panel,
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            account.name,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              fontWeight: FontWeight.w700,
                              fontSize: 15,
                            ),
                          ),
                          Text(
                            account.handle,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: KaedeColors.muted,
                              fontSize: 12,
                            ),
                          ),
                        ],
                      ),
                    ),
                  ],
                ),
              ),
            const Divider(),
            for (final status in const <PresenceStatus>[
              PresenceStatus.online,
              PresenceStatus.idle,
              PresenceStatus.dnd,
              PresenceStatus.invisible,
            ])
              ListTile(
                leading: Icon(
                  presenceIcon(status),
                  size: 18,
                  color: presenceColor(status),
                ),
                title: Text(presenceLabel(status)),
                subtitle: status == PresenceStatus.dnd
                    ? const Text('Notifications stay silent')
                    : status == PresenceStatus.invisible
                        ? const Text('Appear offline to everyone')
                        : null,
                trailing:
                    status == presence ? const Icon(Icons.check_rounded) : null,
                onTap: () {
                  Navigator.pop(sheetContext);
                  ref
                      .read(mobileControllerProvider.notifier)
                      .setPresence(status);
                },
              ),
            const Divider(),
            ListTile(
              leading: const Icon(Icons.settings_outlined),
              title: const Text('Account settings'),
              onTap: () {
                Navigator.pop(sheetContext);
                onTap();
              },
            ),
          ],
        ),
      ),
    );
  }
}

/// A one line notice pinned above the shell: offline, realtime, push and
/// federation states all share this treatment.
/// Connection and account warnings shown above the shell's pages.
///
/// Kept in its own consumer because these fields change often; rebuilding a
/// banner must not rebuild the page view underneath an in-flight swipe.
final class _ShellBanners extends ConsumerWidget {
  const _ShellBanners({required this.onOpenSettings});

  final VoidCallback onOpenSettings;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    // Rebuild only when a banner-relevant field changes. copyWith preserves
    // the previous instance of every field it does not replace, so the tuple
    // below is stable across message, cache, and composer updates.
    ref.watch(mobileControllerProvider.select((state) => (
          state.offline,
          state.phase,
          state.gatewayHealth,
          state.gatewayProtocolWarning,
          state.degradedWarnings,
          state.pushWarning,
        )));
    final state = ref.read(mobileControllerProvider);
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (state.offline)
          _StatusBanner(
            icon: Icons.cloud_off_rounded,
            background: KaedeColors.coralSoft,
            foreground: KaedeColors.coralText,
            title: 'Offline · showing saved conversations',
            actionLabel: 'Retry',
            onAction: () =>
                ref.read(mobileControllerProvider.notifier).refreshNavigation(),
          ),
        if (state.phase == SessionPhase.ready &&
            !state.gatewayHealth.isConnected)
          _StatusBanner(
            icon: state.gatewayHealth.phase == GatewayConnectionPhase.offline
                ? Icons.sync_problem_rounded
                : Icons.sync_rounded,
            background: KaedeColors.warningSoft,
            foreground: KaedeColors.warning,
            busy: state.gatewayHealth.phase != GatewayConnectionPhase.offline,
            title: state.gatewayHealth.message ??
                'Realtime updates are temporarily unavailable.',
            actionLabel: 'Retry',
            actionKey: const ValueKey('retry-realtime-button'),
            onAction: () =>
                ref.read(mobileControllerProvider.notifier).retryRealtime(),
          ),
        if (state.gatewayProtocolWarning case final warning?)
          _StatusBanner(
            icon: Icons.warning_amber_rounded,
            background: KaedeColors.warningSoft,
            foreground: KaedeColors.warning,
            title: warning,
          ),
        if (state.degradedWarnings.isNotEmpty)
          _StatusBanner(
            icon: Icons.cloud_sync_outlined,
            background: KaedeColors.warningSoft,
            foreground: KaedeColors.warning,
            title: state.degradedWarnings.values.first,
            subtitle: state.degradedWarnings.length > 1
                ? '${state.degradedWarnings.length} account areas need to '
                    'resync.'
                : null,
            actionLabel: 'Retry',
            onAction: () =>
                ref.read(mobileControllerProvider.notifier).retryDegradedData(),
          ),
        if (state.pushWarning case final warning?)
          _StatusBanner(
            icon: Icons.notifications_off_outlined,
            background: KaedeColors.warningSoft,
            foreground: KaedeColors.warning,
            title: warning,
            actionLabel: 'Settings',
            onAction: () => onOpenSettings(),
          ),
      ],
    );
  }
}

/// Voice call strip, shown while the call is happening somewhere the reader is
/// not currently looking.
final class _VoiceStatusStrip extends ConsumerWidget {
  const _VoiceStatusStrip({required this.onOpenChannel});

  final void Function(KaedeChannel channel) onOpenChannel;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final voice = ref.watch(voiceSessionProvider);
    final openChannel = ref.watch(
      mobileControllerProvider.select((state) => state.activeChannel?.ref),
    );
    if (!voice.joined || voice.channel?.ref == openChannel) {
      return const SizedBox.shrink();
    }
    return _VoiceStatusBar(
      voice: voice,
      onOpen: () {
        final channel = voice.channel;
        if (channel != null) onOpenChannel(channel);
      },
      onToggleMute: voice.canSpeak
          ? () => _runVisibleAction(
                context,
                'Could not change the microphone state',
                voice.toggleMute,
              )
          : null,
      onLeave: () => _runVisibleAction(
        context,
        'Could not leave voice',
        voice.leave,
      ),
    );
  }
}

final class _StatusBanner extends StatelessWidget {
  const _StatusBanner({
    required this.icon,
    required this.title,
    required this.background,
    required this.foreground,
    this.subtitle,
    this.actionLabel,
    this.onAction,
    this.actionKey,
    this.busy = false,
  });

  final IconData icon;
  final String title;
  final String? subtitle;
  final Color background;
  final Color foreground;
  final String? actionLabel;
  final VoidCallback? onAction;
  final Key? actionKey;
  final bool busy;

  @override
  Widget build(BuildContext context) => Material(
        color: background,
        child: Padding(
          padding: EdgeInsets.fromLTRB(14, 8, onAction == null ? 14 : 4, 8),
          child: Row(
            children: [
              if (busy)
                SizedBox.square(
                  dimension: 15,
                  child: CircularProgressIndicator(
                    strokeWidth: 2,
                    color: foreground,
                  ),
                )
              else
                Icon(icon, size: 17, color: foreground),
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      title,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: foreground,
                        fontSize: 12.5,
                        fontWeight: FontWeight.w600,
                        height: 1.3,
                      ),
                    ),
                    if (subtitle case final detail?)
                      Text(
                        detail,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: KaedeColors.textSoft,
                          fontSize: 11.5,
                          height: 1.3,
                        ),
                      ),
                  ],
                ),
              ),
              if (actionLabel != null && onAction != null)
                TextButton(
                  key: actionKey,
                  onPressed: onAction,
                  style: TextButton.styleFrom(
                    minimumSize: const Size(0, 34),
                    padding: const EdgeInsets.symmetric(horizontal: 12),
                    foregroundColor: foreground,
                  ),
                  child: Text(actionLabel!),
                ),
            ],
          ),
        ),
      );
}

/// Persistent call bar shown while a voice room is connected in the
/// background, with the controls people reach for most.
final class _VoiceStatusBar extends StatelessWidget {
  const _VoiceStatusBar({
    required this.voice,
    required this.onOpen,
    required this.onToggleMute,
    required this.onLeave,
  });

  final VoiceSession voice;
  final VoidCallback onOpen;
  final VoidCallback? onToggleMute;
  final VoidCallback onLeave;

  @override
  Widget build(BuildContext context) => Material(
        color: KaedeColors.mintSoft,
        child: InkWell(
          onTap: onOpen,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(14, 7, 6, 7),
            child: Row(
              children: [
                if (voice.reconnecting)
                  const SizedBox.square(
                    dimension: 16,
                    child: CircularProgressIndicator(
                      strokeWidth: 2,
                      color: KaedeColors.mint,
                    ),
                  )
                else
                  const Icon(Icons.graphic_eq_rounded,
                      size: 18, color: KaedeColors.mint),
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        voice.channel?.name ?? 'Voice room',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: KaedeColors.mint,
                          fontWeight: FontWeight.w700,
                          fontSize: 13,
                        ),
                      ),
                      Text(
                        voice.reconnecting
                            ? 'Reconnecting · keeping your place'
                            : '${voice.participants.length} connected',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: KaedeColors.textSoft,
                          fontSize: 11.5,
                        ),
                      ),
                    ],
                  ),
                ),
                IconButton(
                  tooltip: voice.muted ? 'Unmute' : 'Mute',
                  onPressed: onToggleMute,
                  visualDensity: VisualDensity.compact,
                  style: IconButton.styleFrom(
                    foregroundColor:
                        voice.muted ? KaedeColors.danger : KaedeColors.text,
                  ),
                  icon: Icon(
                    voice.muted ? Icons.mic_off_rounded : Icons.mic_rounded,
                    size: 20,
                  ),
                ),
                IconButton(
                  tooltip: 'Leave voice',
                  onPressed: onLeave,
                  visualDensity: VisualDensity.compact,
                  style: IconButton.styleFrom(
                    foregroundColor: KaedeColors.danger,
                  ),
                  icon: const Icon(Icons.call_end_rounded, size: 20),
                ),
              ],
            ),
          ),
        ),
      );
}

final class _NoConversationSelected extends StatelessWidget {
  const _NoConversationSelected();

  @override
  Widget build(BuildContext context) => const Center(
        child: Padding(
          padding: EdgeInsets.all(32),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.forum_outlined, size: 38, color: KaedeColors.muted),
              SizedBox(height: 14),
              Text(
                'No conversation open',
                style: TextStyle(fontWeight: FontWeight.w700, fontSize: 15),
              ),
              SizedBox(height: 4),
              Text(
                'Pick a channel or direct message to start reading.',
                textAlign: TextAlign.center,
                style: TextStyle(color: KaedeColors.muted),
              ),
            ],
          ),
        ),
      );
}

final class _EmptyNavigation extends StatelessWidget {
  const _EmptyNavigation(
      {required this.icon, required this.title, required this.body});
  final IconData icon;
  final String title;
  final String body;

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(28),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                width: 54,
                height: 54,
                decoration: BoxDecoration(
                  color: KaedeColors.panel,
                  shape: BoxShape.circle,
                  border: Border.all(color: KaedeColors.border),
                ),
                child: Icon(icon, size: 24, color: KaedeColors.muted),
              ),
              const SizedBox(height: 14),
              Text(
                title,
                textAlign: TextAlign.center,
                style: const TextStyle(
                  fontWeight: FontWeight.w700,
                  fontSize: 14.5,
                ),
              ),
              const SizedBox(height: 4),
              Text(
                body,
                textAlign: TextAlign.center,
                style: const TextStyle(
                  color: KaedeColors.muted,
                  fontSize: 13,
                  height: 1.35,
                ),
              ),
            ],
          ),
        ),
      );
}

Future<void> _showGuildActions(BuildContext context, WidgetRef ref) async {
  await showModalBottomSheet<void>(
    context: context,
    showDragHandle: true,
    builder: (sheetContext) => SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(20, 0, 20, 24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              'Add a guild',
              style: Theme.of(context).textTheme.headlineSmall,
            ),
            const SizedBox(height: 4),
            const Text(
              'Guilds are communities. Create your own or join one with an '
              'invite from any Kaede server.',
              style: TextStyle(color: KaedeColors.muted, fontSize: 13),
            ),
            const SizedBox(height: 18),
            FilledButton.icon(
              onPressed: () {
                Navigator.pop(sheetContext);
                _textAction(context, 'Create a guild', 'Guild name',
                    (value) async {
                  await ref
                      .read(mobileControllerProvider.notifier)
                      .repository
                      .createGuild(value);
                  await ref
                      .read(mobileControllerProvider.notifier)
                      .refreshNavigation();
                });
              },
              icon: const Icon(Icons.add_rounded),
              label: const Text('Create a guild'),
            ),
            const SizedBox(height: 10),
            OutlinedButton.icon(
              onPressed: () {
                Navigator.pop(sheetContext);
                _textAction(
                    context, 'Join with an invite', 'Invite code or URL',
                    (value) async {
                  final code =
                      Uri.tryParse(value)?.pathSegments.lastOrNull ?? value;
                  await ref
                      .read(mobileControllerProvider.notifier)
                      .repository
                      .acceptInvite(code);
                  await ref
                      .read(mobileControllerProvider.notifier)
                      .refreshNavigation();
                });
              },
              icon: const Icon(Icons.public_rounded),
              label: const Text('Join with an invite'),
            ),
          ],
        ),
      ),
    ),
  );
}

Future<void> _createAndShowInvite(BuildContext context,
    MobileController controller, KaedeGuild guild, KaedeChannel channel) async {
  final restrictions = await showInviteRestrictions(context);
  if (restrictions == null || !context.mounted) return;
  try {
    final result = await controller.repository.createInvite(guild.ref, {
      'channel_id': channel.ref.id.value,
      'max_age_seconds': restrictions.$1,
      'max_uses': restrictions.$2,
    });
    if (!context.mounted) return;
    final code = '${result['code'] ?? ''}';
    final instance = controller.api.tokens?.instance.value;
    final link = code.isEmpty || instance == null
        ? null
        : 'https://$instance/invite/$code';
    await showDialog<void>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        icon: const Icon(Icons.person_add_alt_1_rounded),
        title: const Text('Invite people'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
                'Anyone with this link can join #${channel.name ?? 'channel'}.'),
            const SizedBox(height: 14),
            Container(
              width: double.infinity,
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
              decoration: BoxDecoration(
                color: KaedeColors.raised,
                borderRadius: BorderRadius.circular(KaedeRadius.medium),
                border: Border.all(color: KaedeColors.border),
              ),
              child: SelectableText(
                link ?? (code.isEmpty ? 'Invite created.' : code),
                style: const TextStyle(
                  fontFamily: 'monospace',
                  fontSize: 13,
                ),
              ),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext),
            child: const Text('Done'),
          ),
          if (link != null || code.isNotEmpty)
            FilledButton.icon(
              onPressed: () async {
                await Clipboard.setData(ClipboardData(text: link ?? code));
                if (dialogContext.mounted) Navigator.pop(dialogContext);
                if (context.mounted) {
                  ScaffoldMessenger.of(context).showSnackBar(
                    const SnackBar(content: Text('Invite link copied.')),
                  );
                }
              },
              icon: const Icon(Icons.copy_rounded),
              label: const Text('Copy link'),
            ),
        ],
      ),
    );
  } on Object catch (error) {
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(userFacingError(
          error,
          summary: 'Could not create the invite',
        )),
      ));
    }
  }
}

final class ConversationCompactHeader extends StatelessWidget {
  const ConversationCompactHeader({
    super.key,
    this.leading,
    this.avatar,
    required this.title,
    this.subtitle,
    this.actions = const [],
  });

  final Widget? leading;
  final Widget? avatar;
  final String title;
  final String? subtitle;
  final List<Widget> actions;

  @override
  Widget build(BuildContext context) => DecoratedBox(
        decoration: const BoxDecoration(
          color: KaedeColors.canvas,
          border: Border(bottom: BorderSide(color: KaedeColors.border)),
        ),
        child: SizedBox(
          height: 58,
          child: Row(
            children: [
              if (leading != null) leading!,
              if (avatar != null) ...[
                if (leading == null) const SizedBox(width: 14),
                SizedBox.square(dimension: 34, child: avatar),
                const SizedBox(width: 10),
              ] else if (leading == null)
                const SizedBox(width: 16),
              Expanded(
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        fontSize: 17,
                        fontWeight: FontWeight.w700,
                        letterSpacing: -.2,
                      ),
                    ),
                    if (subtitle?.isNotEmpty == true)
                      Text(
                        subtitle!,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: KaedeColors.muted,
                          fontSize: 11.5,
                          height: 1.25,
                        ),
                      ),
                  ],
                ),
              ),
              ...actions,
              const SizedBox(width: 4),
            ],
          ),
        ),
      );
}

/// Rail entry with the selection pill and unread dot conventions people know
/// from every other guild list. The pill is painted at the rail's left edge
/// while the icon stays centred, so nothing shifts when selection changes.
final class _RailButton extends StatelessWidget {
  const _RailButton({
    required this.label,
    required this.active,
    required this.onTap,
    this.badge = 0,
    this.unread = false,
    this.child,
    this.size = 48,
    this.activeColor,
    this.idleColor,
    this.border = false,
  });

  final String label;
  final bool active;
  final VoidCallback onTap;
  final int badge;
  final bool unread;
  final Widget? child;
  final double size;

  /// Fill used while selected. Guild icons paint themselves, so they leave it
  /// unset and rely on the pill plus the squared corners instead.
  final Color? activeColor;
  final Color? idleColor;
  final bool border;

  @override
  Widget build(BuildContext context) {
    final radius = BorderRadius.circular(active ? size * .31 : size * .5);
    return SizedBox(
      height: size + 8,
      child: Stack(
        alignment: Alignment.center,
        children: [
          Align(
            alignment: Alignment.centerLeft,
            child: AnimatedContainer(
              duration: const Duration(milliseconds: 180),
              curve: Curves.easeOutCubic,
              width: 4,
              height: active ? size * .6 : (unread || badge > 0 ? 8.0 : 0.0),
              decoration: const BoxDecoration(
                color: KaedeColors.text,
                borderRadius: BorderRadius.horizontal(
                  right: Radius.circular(4),
                ),
              ),
            ),
          ),
          Tooltip(
            message: label,
            child: Badge(
              isLabelVisible: badge > 0,
              offset: const Offset(-2, 2),
              alignment: Alignment.bottomRight,
              label: Text(badge > 99 ? '99+' : '$badge'),
              child: Material(
                color: active
                    ? activeColor ?? KaedeColors.selected
                    : idleColor ?? KaedeColors.panel,
                clipBehavior: Clip.antiAlias,
                shape: RoundedRectangleBorder(
                  borderRadius: radius,
                  side: border
                      ? const BorderSide(color: KaedeColors.border)
                      : BorderSide.none,
                ),
                child: InkWell(
                  onTap: onTap,
                  child: SizedBox.square(
                    dimension: size,
                    child: child ??
                        Center(
                          child: Text(
                            label,
                            style: const TextStyle(
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        ),
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

final class _ChannelUnread extends StatelessWidget {
  const _ChannelUnread({required this.unread, required this.mentions});

  final int unread;
  final int mentions;

  @override
  Widget build(BuildContext context) {
    if (mentions > 0) {
      return Badge(
        backgroundColor: KaedeColors.danger,
        textColor: Colors.white,
        label: Text(mentions > 99 ? '99+' : '$mentions'),
      );
    }
    return const SizedBox.shrink();
  }
}

final class _DmUnread extends StatelessWidget {
  const _DmUnread({required this.unread, required this.mentions});

  final int unread;
  final int mentions;

  @override
  Widget build(BuildContext context) {
    final count = unread > 0 ? unread : mentions;
    if (count <= 0) return const SizedBox.shrink();
    return Badge(
      backgroundColor: KaedeColors.danger,
      textColor: Colors.white,
      label: Text(count > 99 ? '99+' : '$count'),
    );
  }
}

final class _UnreadMarker extends StatelessWidget {
  const _UnreadMarker({required this.visible});

  final bool visible;

  @override
  Widget build(BuildContext context) => SizedBox(
        width: 4,
        child: Center(
          child: AnimatedContainer(
            duration: const Duration(milliseconds: 140),
            curve: Curves.easeOut,
            width: 4,
            height: visible ? 20 : 0,
            decoration: BoxDecoration(
              color: Colors.white,
              borderRadius: BorderRadius.circular(4),
            ),
          ),
        ),
      );
}

int _guildMentions(MobileState state, KaedeGuild guild) => guild.channels.fold(
      0,
      (count, channel) => count + (state.mentionCounts[channel.ref] ?? 0),
    );

bool _guildUnread(MobileState state, KaedeGuild guild) => guild.channels.any(
      (channel) => (state.unreadCounts[channel.ref] ?? 0) > 0,
    );

final class _FriendsPage extends ConsumerWidget {
  const _FriendsPage({required this.onOpenChat});

  final VoidCallback onOpenChat;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    ref.watch(mobileControllerProvider.select((state) => state.relationships));
    final state = ref.read(mobileControllerProvider);
    final sections = <String, List<Map<String, Object?>>>{
      'friend': <Map<String, Object?>>[],
      'pending_in': <Map<String, Object?>>[],
      'pending_out': <Map<String, Object?>>[],
      'blocked': <Map<String, Object?>>[],
    };
    for (final relationship in state.relationships) {
      sections['${relationship['type']}']?.add(relationship);
    }
    final empty = sections.values.every((items) => items.isEmpty);
    return RefreshIndicator(
      onRefresh: ref.read(mobileControllerProvider.notifier).refreshNavigation,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(14, 12, 14, 32),
        children: [
          FilledButton.icon(
            onPressed: () => _textAction(
                context, 'Add a friend', '@friend@example.net', (value) async {
              await ref
                  .read(mobileControllerProvider.notifier)
                  .repository
                  .requestFriend(value);
              await ref
                  .read(mobileControllerProvider.notifier)
                  .refreshNavigation();
            }),
            icon: const Icon(Icons.person_add_alt_1_rounded),
            label: const Text('Add friend'),
          ),
          const SizedBox(height: 6),
          const Padding(
            padding: EdgeInsets.symmetric(horizontal: 4),
            child: Text(
              'Friends live on their own home server. Use their full address, '
              'like @maple@kaede.chat.',
              style: TextStyle(color: KaedeColors.muted, fontSize: 12.5),
            ),
          ),
          if (empty)
            const Padding(
              padding: EdgeInsets.only(top: 40),
              child: _EmptyNavigation(
                icon: Icons.people_outline_rounded,
                title: 'No friends yet',
                body: 'Send a request to start a conversation.',
              ),
            ),
          if (sections['pending_in']!.isNotEmpty)
            _RelationshipSection(
              title: 'Incoming requests',
              relationships: sections['pending_in']!,
              onOpenChat: onOpenChat,
            ),
          if (sections['pending_out']!.isNotEmpty)
            _RelationshipSection(
              title: 'Sent requests',
              relationships: sections['pending_out']!,
              onOpenChat: onOpenChat,
            ),
          if (sections['friend']!.isNotEmpty)
            _RelationshipSection(
              title: 'Friends — ${sections['friend']!.length}',
              relationships: sections['friend']!,
              onOpenChat: onOpenChat,
            ),
          if (sections['blocked']!.isNotEmpty)
            _RelationshipSection(
              title: 'Blocked',
              relationships: sections['blocked']!,
              onOpenChat: onOpenChat,
            ),
        ],
      ),
    );
  }
}

final class _RelationshipSection extends ConsumerWidget {
  const _RelationshipSection({
    required this.title,
    required this.relationships,
    required this.onOpenChat,
  });

  final String title;
  final List<Map<String, Object?>> relationships;
  final VoidCallback onOpenChat;

  @override
  Widget build(BuildContext context, WidgetRef ref) => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(4, 20, 4, 6),
            child: Text(
              title.toUpperCase(),
              style: const TextStyle(
                color: KaedeColors.muted,
                fontSize: 11,
                fontWeight: FontWeight.w800,
                letterSpacing: .9,
              ),
            ),
          ),
          for (final relationship in relationships)
            _RelationshipTile(
                relationship: relationship, onOpenChat: onOpenChat),
        ],
      );
}

final class _RelationshipTile extends ConsumerWidget {
  const _RelationshipTile(
      {required this.relationship, required this.onOpenChat});

  final Map<String, Object?> relationship;
  final VoidCallback onOpenChat;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final snapshot = _relationshipUser(relationship);
    if (snapshot == null) return const SizedBox.shrink();
    // Rebuild only when this relationship's profile or presence changes;
    // unrelated profile and presence updates leave this row untouched.
    final projection = ref.watch(mobileControllerProvider.select((state) {
      final user = state.userProfiles[snapshot.ref] ?? snapshot;
      final presence = user.ref == state.user?.ref
          ? state.presencePreference
          : state.presenceByUser[user.ref] ?? user.presence;
      return (user: user, presence: presence);
    }));
    final user = projection.user;
    final presence = projection.presence;
    final type = '${relationship['type']}';
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Material(
        color: KaedeColors.panel,
        borderRadius: BorderRadius.circular(KaedeRadius.medium),
        child: InkWell(
          onTap: () => _showProfile(context, ref, user, type, onOpenChat),
          borderRadius: BorderRadius.circular(KaedeRadius.medium),
          child: Container(
            padding: const EdgeInsets.fromLTRB(12, 9, 6, 9),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(KaedeRadius.medium),
              border: Border.all(color: KaedeColors.border),
            ),
            child: Row(
              children: [
                UserAvatar(
                  user: user,
                  radius: 19,
                  presence: presence,
                  ringColor: KaedeColors.panel,
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        user.name,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          fontWeight: FontWeight.w600,
                          fontSize: 14.5,
                        ),
                      ),
                      Text(
                        user.profileResolved
                            ? user.handle
                            : 'Profile unavailable · refreshes automatically',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: KaedeColors.muted,
                          fontSize: 12,
                        ),
                      ),
                    ],
                  ),
                ),
                switch (type) {
                  'pending_in' => Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        IconButton(
                          tooltip: 'Accept',
                          style: IconButton.styleFrom(
                            foregroundColor: KaedeColors.mint,
                          ),
                          onPressed: () => _relationshipAction(
                              context,
                              ref,
                              () => ref
                                  .read(mobileControllerProvider.notifier)
                                  .repository
                                  .acceptFriend(user.ref)),
                          icon: const Icon(Icons.check_circle_rounded),
                        ),
                        IconButton(
                          tooltip: 'Decline',
                          onPressed: () => _relationshipAction(
                              context,
                              ref,
                              () => ref
                                  .read(mobileControllerProvider.notifier)
                                  .repository
                                  .removeRelationship(user.ref)),
                          icon: const Icon(Icons.cancel_outlined),
                        ),
                      ],
                    ),
                  'friend' => IconButton(
                      tooltip: 'Message',
                      onPressed: user.profileResolved
                          ? () => _openDm(context, ref, user, onOpenChat)
                          : null,
                      icon: const Icon(Icons.chat_bubble_outline_rounded),
                    ),
                  'blocked' => TextButton(
                      onPressed: () => _relationshipAction(
                          context,
                          ref,
                          () => ref
                              .read(mobileControllerProvider.notifier)
                              .repository
                              .unblock(user.ref)),
                      child: const Text('Unblock'),
                    ),
                  _ => IconButton(
                      tooltip: 'Cancel request',
                      onPressed: () => _relationshipAction(
                          context,
                          ref,
                          () => ref
                              .read(mobileControllerProvider.notifier)
                              .repository
                              .removeRelationship(user.ref)),
                      icon: const Icon(Icons.close_rounded),
                    ),
                },
              ],
            ),
          ),
        ),
      ),
    );
  }
}

KaedeUser? _relationshipUser(Map<String, Object?> relationship) {
  final raw = relationship['user'];
  if (raw is! Map<Object?, Object?>) return null;
  try {
    return KaedeUser.fromJson(raw.map((key, value) => MapEntry('$key', value)));
  } on Object {
    return null;
  }
}

Future<void> _relationshipAction(BuildContext context, WidgetRef ref,
    Future<Object?> Function() action) async {
  try {
    await action();
    await ref.read(mobileControllerProvider.notifier).refreshNavigation();
  } on Object catch (error) {
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(userFacingError(
          error,
          summary: 'Could not update the relationship',
        )),
      ));
    }
  }
}

Future<void> _openDm(BuildContext context, WidgetRef ref, KaedeUser user,
    VoidCallback onOpenChat) async {
  if (!user.profileResolved) return;
  try {
    final dm = await ref
        .read(mobileControllerProvider.notifier)
        .repository
        .openDm(user.handle);
    await ref.read(mobileControllerProvider.notifier).refreshNavigation();
    await ref.read(mobileControllerProvider.notifier).selectDm(dm);
    onOpenChat();
  } on Object catch (error) {
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(userFacingError(
          error,
          summary: 'Could not open the direct message',
        )),
      ));
    }
  }
}

Future<void> _showProfile(BuildContext context, WidgetRef ref, KaedeUser user,
    String relationshipType, VoidCallback onOpenChat) async {
  var resolved = user;
  if (user.profileResolved) {
    try {
      resolved = await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .lookupUser(user.handle);
    } on Object {
      // The relationship snapshot remains usable while a remote instance is
      // temporarily unavailable.
    }
  }
  if (!context.mounted) return;
  final presence =
      ref.read(mobileControllerProvider.notifier).presenceFor(resolved);
  final profile = resolved;
  await showUserProfile(
    context,
    profile,
    presence,
    actions: <Widget>[
      if (profile.profileResolved)
        FilledButton.icon(
          onPressed: () {
            Navigator.pop(context);
            _openDm(context, ref, profile, onOpenChat);
          },
          icon: const Icon(Icons.chat_bubble_outline_rounded),
          label: const Text('Message'),
        ),
      if (profile.profileResolved &&
          relationshipType != 'friend' &&
          relationshipType != 'pending_out' &&
          relationshipType != 'blocked')
        OutlinedButton.icon(
          onPressed: () {
            Navigator.pop(context);
            _relationshipAction(
                context,
                ref,
                () => ref
                    .read(mobileControllerProvider.notifier)
                    .repository
                    .requestFriend(profile.handle));
          },
          icon: const Icon(Icons.person_add_alt_1_rounded),
          label: const Text('Send friend request'),
        ),
      if (relationshipType == 'friend')
        OutlinedButton.icon(
          onPressed: () {
            Navigator.pop(context);
            _relationshipAction(
                context,
                ref,
                () => ref
                    .read(mobileControllerProvider.notifier)
                    .repository
                    .removeRelationship(profile.ref));
          },
          style: OutlinedButton.styleFrom(
            foregroundColor: KaedeColors.danger,
            side: const BorderSide(color: KaedeColors.dangerSoft),
          ),
          icon: const Icon(Icons.person_remove_alt_1_rounded),
          label: const Text('Remove friend'),
        ),
      if (relationshipType == 'blocked')
        OutlinedButton.icon(
          onPressed: () {
            Navigator.pop(context);
            _relationshipAction(
                context,
                ref,
                () => ref
                    .read(mobileControllerProvider.notifier)
                    .repository
                    .unblock(profile.ref));
          },
          icon: const Icon(Icons.lock_open_rounded),
          label: const Text('Unblock'),
        ),
    ],
  );
}

final class _DmAvatar extends StatelessWidget {
  const _DmAvatar({required this.channel, required this.self, this.presence});

  final KaedeChannel channel;
  final KaedeUser? self;
  final PresenceStatus? presence;

  @override
  Widget build(BuildContext context) {
    final recipients = channel.recipients
        .where((user) => self == null || user.ref != self!.ref)
        .toList();
    if (channel.conversationType == 'group') {
      if (recipients.length < 2) {
        return const CircleAvatar(
          radius: 20,
          backgroundColor: KaedeColors.raised,
          foregroundColor: KaedeColors.textSoft,
          child: Icon(Icons.group_rounded, size: 20),
        );
      }
      // Two overlapping avatars read as a group at a glance.
      return SizedBox.square(
        dimension: 40,
        child: Stack(
          children: [
            Positioned(
              right: 0,
              bottom: 0,
              child: UserAvatar(user: recipients[1], radius: 13),
            ),
            Positioned(
              left: 0,
              top: 0,
              child: Container(
                padding: const EdgeInsets.all(1.5),
                decoration: const BoxDecoration(
                  color: KaedeColors.sidebar,
                  shape: BoxShape.circle,
                ),
                child: UserAvatar(user: recipients.first, radius: 13),
              ),
            ),
          ],
        ),
      );
    }
    if (recipients.isNotEmpty) {
      return UserAvatar(
        user: recipients.first,
        radius: 20,
        presence: presence,
      );
    }
    return const CircleAvatar(
      radius: 20,
      backgroundColor: KaedeColors.raised,
      foregroundColor: KaedeColors.textSoft,
      child: Icon(Icons.group_rounded, size: 20),
    );
  }
}

Future<void> _newConversationAction(
  BuildContext context,
  WidgetRef ref,
  MobileState state,
  VoidCallback onOpenChannel,
) async {
  final mode = await showModalBottomSheet<String>(
    context: context,
    builder: (context) => SafeArea(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          ListTile(
            leading: const Icon(Icons.person_outline_rounded),
            title: const Text('Direct message'),
            subtitle:
                const Text('Start a private conversation with one person'),
            onTap: () => Navigator.pop(context, 'direct'),
          ),
          ListTile(
            leading: const Icon(Icons.group_outlined),
            title: const Text('Create group DM'),
            subtitle: const Text('Choose two or more friends'),
            onTap: () => Navigator.pop(context, 'group'),
          ),
        ],
      ),
    ),
  );
  if (!context.mounted || mode == null) return;
  if (mode == 'direct') {
    await _textAction(context, 'New message', '@friend@example.net',
        (handle) async {
      final dm = await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .openDm(handle);
      await ref.read(mobileControllerProvider.notifier).refreshNavigation();
      onOpenChannel();
      unawaited(ref.read(mobileControllerProvider.notifier).selectDm(dm));
    });
    return;
  }
  final friends = state.relationships
      .where((item) => '${item['type']}' == 'friend' && item['user'] is Map)
      .map((item) => KaedeUser.fromJson(
            Map<String, Object?>.from(item['user']! as Map),
          ))
      .toList();
  final name = TextEditingController();
  final selected = <EntityRef>{};
  final submitted = await showDialog<bool>(
    context: context,
    builder: (context) => StatefulBuilder(
      builder: (context, setDialogState) => AlertDialog(
        title: const Text('Create group DM'),
        content: SizedBox(
          width: 440,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                controller: name,
                maxLength: 100,
                decoration: const InputDecoration(
                  labelText: 'Group name',
                  hintText: 'Optional',
                ),
              ),
              Align(
                alignment: Alignment.centerLeft,
                child: Text('${selected.length} of 9 friends selected'),
              ),
              const SizedBox(height: 8),
              SizedBox(
                height: 320,
                child: ListView(
                  children: [
                    for (final friend in friends)
                      CheckboxListTile(
                        value: selected.contains(friend.ref),
                        secondary: UserAvatar(user: friend),
                        title: Text(friend.name),
                        subtitle: Text(friend.handle),
                        onChanged: selected.length >= 9 &&
                                !selected.contains(friend.ref)
                            ? null
                            : (_) => setDialogState(() {
                                  if (!selected.add(friend.ref)) {
                                    selected.remove(friend.ref);
                                  }
                                }),
                      ),
                  ],
                ),
              ),
            ],
          ),
        ),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel')),
          FilledButton(
            onPressed:
                selected.length < 2 ? null : () => Navigator.pop(context, true),
            child: const Text('Create'),
          ),
        ],
      ),
    ),
  );
  if (submitted != true || !context.mounted) return;
  try {
    final handles = friends
        .where((friend) => selected.contains(friend.ref))
        .map((friend) => friend.handle)
        .toList();
    final dm = await ref
        .read(mobileControllerProvider.notifier)
        .repository
        .createGroupDm(
          handles,
          name: name.text.trim().isEmpty ? null : name.text.trim(),
        );
    await ref.read(mobileControllerProvider.notifier).refreshNavigation();
    onOpenChannel();
    unawaited(ref.read(mobileControllerProvider.notifier).selectDm(dm));
  } on Object catch (error) {
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(userFacingError(error))),
      );
    }
  } finally {
    name.dispose();
  }
}

Future<void> _textAction(
  BuildContext context,
  String title,
  String hint,
  Future<void> Function(String value) action,
) async {
  final controller = TextEditingController();
  final value = await showDialog<String>(
    context: context,
    builder: (context) => AlertDialog(
      title: Text(title),
      content: TextField(
          controller: controller,
          autofocus: true,
          decoration: InputDecoration(hintText: hint)),
      actions: [
        TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('Cancel')),
        FilledButton(
            onPressed: () {
              final value = controller.text.trim();
              if (value.isEmpty) {
                ScaffoldMessenger.of(context).showSnackBar(
                  SnackBar(content: Text('Enter $hint.')),
                );
                return;
              }
              Navigator.pop(context, value);
            },
            child: const Text('Continue')),
      ],
    ),
  );
  if (value?.isNotEmpty == true) {
    try {
      await action(value!);
    } on Object catch (error) {
      if (!context.mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(userFacingError(
          error,
          summary: 'Could not complete “$title”',
        )),
      ));
    }
  }
}

Future<void> _runVisibleAction(
  BuildContext context,
  String summary,
  Future<void> Function() action,
) async {
  try {
    await action();
  } on Object catch (error) {
    if (!context.mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(userFacingError(error, summary: summary)),
    ));
  }
}

extension _LastOrNull<T> on List<T> {
  T? get lastOrNull => isEmpty ? null : last;
}
