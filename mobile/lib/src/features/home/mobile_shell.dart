import 'dart:async';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/guild_navigation.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/chat/channel_view.dart';
import 'package:kaede_mobile/src/features/chat/message_search_screen.dart';
import 'package:kaede_mobile/src/features/guild/guild_management_screen.dart';
import 'package:kaede_mobile/src/features/settings/settings_screen.dart';
import 'package:kaede_mobile/src/features/shared/remote_media.dart';
import 'package:kaede_mobile/src/features/voice/voice_session.dart';
import 'package:kaede_mobile/src/gateway/gateway_client.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:uuid/uuid.dart';

final class MobileShell extends ConsumerStatefulWidget {
  const MobileShell({super.key});

  @override
  ConsumerState<MobileShell> createState() => _MobileShellState();
}

final class _MobileShellState extends ConsumerState<MobileShell>
    with WidgetsBindingObserver {
  var _section = _ShellSection.messages;
  late final PageController _pages;
  var _messagePage = 0;

  @override
  void initState() {
    super.initState();
    _pages = PageController();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    ref
        .read(mobileControllerProvider.notifier)
        .setAppActive(state == AppLifecycleState.resumed);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _pages.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(mobileControllerProvider);
    final activeChannel = state.activeChannel;
    final voice = ref.watch(voiceSessionProvider);
    final body = SafeArea(
        child: Column(
      children: [
        if (state.offline)
          Material(
            color: KaedeColors.coralDark,
            child: SafeArea(
              top: false,
              child: ListTile(
                dense: true,
                leading: const Icon(Icons.cloud_off_rounded),
                title: const Text('Offline · showing saved conversations'),
                trailing: TextButton(
                  onPressed: () => ref
                      .read(mobileControllerProvider.notifier)
                      .refreshNavigation(),
                  child: const Text('Retry'),
                ),
              ),
            ),
          ),
        if (state.phase == SessionPhase.ready &&
            !state.gatewayHealth.isConnected)
          Material(
            color: const Color(0xFF4A391B),
            child: ListTile(
              dense: true,
              leading: Icon(
                state.gatewayHealth.phase == GatewayConnectionPhase.offline
                    ? Icons.sync_problem_rounded
                    : Icons.sync_rounded,
                color: KaedeColors.warning,
              ),
              title: Text(
                state.gatewayHealth.message ??
                    'Realtime updates are temporarily unavailable.',
              ),
              trailing:
                  state.gatewayHealth.phase == GatewayConnectionPhase.connecting
                      ? const SizedBox.square(
                          dimension: 18,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : TextButton(
                          onPressed: () => ref
                              .read(mobileControllerProvider.notifier)
                              .retryRealtime(),
                          child: const Text('Retry'),
                        ),
            ),
          ),
        if (state.gatewayProtocolWarning case final warning?)
          Material(
            color: const Color(0xFF4A391B),
            child: ListTile(
              dense: true,
              leading: const Icon(
                Icons.warning_amber_rounded,
                color: KaedeColors.warning,
              ),
              title: Text(warning),
            ),
          ),
        if (state.degradedWarnings.isNotEmpty)
          Material(
            color: const Color(0xFF4A391B),
            child: ListTile(
              dense: true,
              leading: const Icon(
                Icons.cloud_sync_outlined,
                color: KaedeColors.warning,
              ),
              title: Text(state.degradedWarnings.values.first),
              subtitle: state.degradedWarnings.length > 1
                  ? Text(
                      '${state.degradedWarnings.length} account areas need to resync.',
                    )
                  : null,
              trailing: TextButton(
                onPressed: () => ref
                    .read(mobileControllerProvider.notifier)
                    .retryDegradedData(),
                child: const Text('Retry'),
              ),
            ),
          ),
        if (state.pushWarning case final warning?)
          Material(
            color: const Color(0xFF4A391B),
            child: ListTile(
              dense: true,
              leading: const Icon(
                Icons.notifications_off_outlined,
                color: KaedeColors.warning,
              ),
              title: Text(warning),
              trailing: TextButton(
                onPressed: () => _showSection(_ShellSection.settings),
                child: const Text('Settings'),
              ),
            ),
          ),
        if (voice.connected && voice.channel?.ref != activeChannel?.ref)
          Material(
            color: const Color(0xFF174C3E),
            child: ListTile(
              dense: true,
              leading: const Icon(Icons.graphic_eq_rounded),
              title: Text('Voice connected · ${voice.channel?.name ?? 'Room'}'),
              subtitle: Text('${voice.participants.length} connected'),
              onTap: () async {
                final channel = voice.channel;
                if (channel == null) return;
                await ref
                    .read(mobileControllerProvider.notifier)
                    .selectChannel(channel);
                if (mounted) {
                  setState(() {
                    _section = _ShellSection.messages;
                    _openConversation();
                  });
                }
              },
              trailing: Wrap(
                spacing: 2,
                children: [
                  IconButton(
                    tooltip: voice.muted ? 'Unmute' : 'Mute',
                    onPressed: voice.canSpeak
                        ? () => _runVisibleAction(
                              context,
                              'Could not change the microphone state',
                              voice.toggleMute,
                            )
                        : null,
                    icon: Icon(voice.muted
                        ? Icons.mic_off_rounded
                        : Icons.mic_rounded),
                  ),
                  IconButton(
                    tooltip: 'Leave voice',
                    onPressed: () => _runVisibleAction(
                      context,
                      'Could not leave voice',
                      voice.leave,
                    ),
                    icon: const Icon(Icons.call_end_rounded),
                  ),
                ],
              ),
            ),
          ),
        Expanded(
          child: switch (_section) {
            _ShellSection.messages => PageView(
                controller: _pages,
                onPageChanged: (page) {
                  setState(() => _messagePage = page);
                  ref
                      .read(mobileControllerProvider.notifier)
                      .setConversationPaneVisible(page == 1);
                },
                children: [
                  _ChatBrowser(
                    onOpenChannel: _openConversation,
                    onOpenFriends: () => _showSection(_ShellSection.friends),
                    onOpenSettings: () => _showSection(_ShellSection.settings),
                  ),
                  activeChannel == null
                      ? const Center(child: Text('Choose a conversation.'))
                      : _ConversationScreen(
                          channel: activeChannel,
                          onBack: _openNavigation,
                          onMembers: activeChannel.guildRef == null
                              ? null
                              : _openMembers,
                        ),
                  if (activeChannel?.guildRef != null &&
                      state.activeGuild != null)
                    _GuildMemberPane(
                      guild: state.activeGuild!,
                      onBack: _openConversation,
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
                title: 'You',
                onBack: () => _showSection(_ShellSection.messages),
                child: const SettingsScreen(),
              ),
          },
        ),
      ],
    ));
    return PopScope(
      canPop: _section == _ShellSection.messages && _messagePage == 0,
      onPopInvokedWithResult: (didPop, _) {
        if (didPop) return;
        if (_messagePage > 0) {
          if (_messagePage == 2) {
            _openConversation();
          } else {
            _openNavigation();
          }
        } else {
          _showSection(_ShellSection.messages);
        }
      },
      child: Scaffold(body: body),
    );
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
    if (!_pages.hasClients) return;
    _pages.animateToPage(2,
        duration: const Duration(milliseconds: 220),
        curve: Curves.easeOutCubic);
  }

  void _showSection(_ShellSection section) {
    setState(() => _section = section);
    ref.read(mobileControllerProvider.notifier).setConversationPaneVisible(
          section == _ShellSection.messages && _messagePage == 1,
        );
  }
}

enum _ShellSection { messages, friends, settings }

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
  Widget build(BuildContext context) => Column(
        children: [
          _CompactHeader(
            leading: IconButton(
              onPressed: onBack,
              icon: const Icon(Icons.arrow_back_rounded),
            ),
            title: title,
          ),
          Expanded(child: child),
        ],
      );
}

final class _ConversationScreen extends StatelessWidget {
  const _ConversationScreen({
    required this.channel,
    required this.onBack,
    this.onMembers,
  });

  final KaedeChannel channel;
  final VoidCallback onBack;
  final VoidCallback? onMembers;

  @override
  Widget build(BuildContext context) {
    final recipient =
        channel.recipients.isEmpty ? null : channel.recipients.first;
    return Column(
      children: [
        _CompactHeader(
          leading: IconButton(
            onPressed: onBack,
            icon: const Icon(Icons.arrow_back_rounded),
          ),
          avatar: recipient == null
              ? null
              : GestureDetector(
                  onTap: () => showUserProfile(
                    context,
                    recipient,
                    recipient.presence,
                  ),
                  child: UserAvatar(user: recipient),
                ),
          title: channel.name ?? recipient?.name ?? 'Conversation',
          subtitle: channel.topic,
          actions: [
            if (channel.type == ChannelType.dm)
              IconButton(
                tooltip: 'Start voice call',
                onPressed: null,
                icon: const Icon(Icons.call_outlined),
              ),
            IconButton(
              tooltip: 'Search',
              onPressed: () => Navigator.of(context).push(
                MaterialPageRoute<void>(
                  builder: (_) => MessageSearchScreen(
                    repository: ProviderScope.containerOf(context)
                        .read(mobileControllerProvider.notifier)
                        .repository,
                    scope: channel.guildRef == null ? 'channel' : 'guild',
                    scopeRef: channel.guildRef ?? channel.ref,
                    channel: channel,
                    accountRef: ProviderScope.containerOf(context)
                        .read(mobileControllerProvider.notifier)
                        .api
                        .tokens
                        ?.userRef,
                    users: ProviderScope.containerOf(context)
                        .read(mobileControllerProvider)
                        .userProfiles
                        .values
                        .toList(growable: false),
                    onJump: (result) async {
                      final controller = ProviderScope.containerOf(context)
                          .read(mobileControllerProvider.notifier);
                      if (result.channel.guildRef == null) {
                        await controller.selectDm(result.channel);
                      } else {
                        await controller.selectChannel(result.channel);
                      }
                      await controller.loadAround(result.message.ref);
                      if (context.mounted) Navigator.of(context).pop();
                    },
                  ),
                ),
              ),
              icon: const Icon(Icons.search_rounded),
            ),
            if (onMembers != null)
              IconButton(
                tooltip: 'Member list',
                onPressed: onMembers,
                icon: const Icon(Icons.people_alt_outlined),
              ),
          ],
        ),
        const Expanded(child: ChannelView()),
      ],
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

final class _GuildMemberPaneState extends ConsumerState<_GuildMemberPane>
    with AutomaticKeepAliveClientMixin {
  List<GuildMember>? _members;
  String? _error;
  var _partial = false;

  @override
  bool get wantKeepAlive => true;

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
      _load();
    }
  }

  Future<void> _load() async {
    final requestedGuild = widget.guild;
    try {
      final members = await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .members(requestedGuild.ref);
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

  @override
  Widget build(BuildContext context) {
    super.build(context);
    final mobile = ref.watch(mobileControllerProvider);
    return Column(
      children: [
        _CompactHeader(
          leading: IconButton(
            onPressed: widget.onBack,
            icon: const Icon(Icons.arrow_back_rounded),
          ),
          title: 'Members',
          subtitle: widget.guild.name,
        ),
        Expanded(
          child: _error != null
              ? Center(
                  child: Padding(
                    padding: const EdgeInsets.all(28),
                    child: Text(_error!, textAlign: TextAlign.center),
                  ),
                )
              : _members == null
                  ? const Center(child: CircularProgressIndicator())
                  : RefreshIndicator(
                      onRefresh: _load,
                      child: ListView.builder(
                        padding: const EdgeInsets.symmetric(vertical: 8),
                        itemCount: _members!.length + (_partial ? 1 : 0),
                        itemBuilder: (context, index) {
                          if (_partial && index == 0) {
                            return const ListTile(
                              leading: Icon(
                                Icons.info_outline_rounded,
                                color: KaedeColors.warning,
                              ),
                              title: Text('Partial member list'),
                              subtitle: Text(
                                'Kaede could not load the full roster. These are only members seen in cached messages. Pull down to retry.',
                              ),
                            );
                          }
                          final member = _members![index - (_partial ? 1 : 0)];
                          final user = mobile.userProfiles[member.user.ref] ??
                              member.user;
                          final presence =
                              mobile.presenceByUser[user.ref] ?? user.presence;
                          return ListTile(
                            leading: UserAvatar(user: user),
                            title: Text(member.nickname ?? user.name),
                            subtitle: Text(_presenceLabel(presence)),
                            onTap: () => showUserProfile(
                              context,
                              user,
                              presence,
                            ),
                          );
                        },
                      ),
                    ),
        ),
      ],
    );
  }
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
    final state = ref.watch(mobileControllerProvider);
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
        const VerticalDivider(width: 1),
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
          child: Column(
            children: [
              const SizedBox(height: 10),
              _RailButton(
                label: 'Messages',
                active: state.selectedGuild == null,
                onTap: onOpenHome,
                badge: state.dms.fold(
                  0,
                  (total, dm) => total + (state.unreadCounts[dm.ref] ?? 0),
                ),
                unread: state.dms.any(
                  (dm) => (state.unreadCounts[dm.ref] ?? 0) > 0,
                ),
                child: const Icon(Icons.chat_bubble_rounded, size: 26),
              ),
              const Padding(
                padding: EdgeInsets.symmetric(horizontal: 14),
                child: Divider(height: 12),
              ),
              Expanded(
                child: ReorderableListView.builder(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 9, vertical: 6),
                  itemCount: navigation.items.length,
                  onReorder: (oldIndex, newIndex) => ref
                      .read(mobileControllerProvider.notifier)
                      .saveGuildNavigation(
                        reorderGuildNavigation(navigation, oldIndex, newIndex),
                      ),
                  itemBuilder: (context, index) {
                    final item = navigation.items[index];
                    return switch (item) {
                      GuildNavigationGuildItem() => Builder(
                          key: ValueKey('guild:${item.guild.wire}'),
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
                                    child: GuildIcon(guild: guild, size: 54),
                                  );
                          },
                        ),
                      GuildNavigationGroupItem() => _GuildRailFolder(
                          key: ValueKey('group:${item.id}'),
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
                    };
                  },
                ),
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
                child: const Icon(Icons.create_new_folder_outlined, size: 24),
              ),
              _RailButton(
                label: 'Add a guild',
                active: false,
                onTap: onAddGuild,
                child: const Icon(Icons.add_rounded,
                    color: KaedeColors.mint, size: 28),
              ),
              const SizedBox(height: 2),
            ],
          ),
        ),
      ),
    );
  }
}

final class _GuildRailFolder extends StatelessWidget {
  const _GuildRailFolder({
    super.key,
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
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(4),
      decoration: BoxDecoration(
        color: KaedeColors.raised.withValues(alpha: .72),
        borderRadius: BorderRadius.circular(20),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          _RailButton(
            label: group.name,
            active: guilds.any((guild) => guild.ref == state.selectedGuild),
            onTap: onToggle,
            badge: mentions,
            unread: guilds.any((guild) => _guildUnread(state, guild)),
            child: Icon(
              group.collapsed
                  ? Icons.folder_rounded
                  : Icons.folder_open_rounded,
              color: KaedeColors.coral,
              size: 29,
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
                child: GuildIcon(guild: guild, size: 54),
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

  @override
  Widget build(BuildContext context, WidgetRef ref) => ColoredBox(
        color: KaedeColors.canvas,
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(18, 17, 12, 10),
              child: Row(
                children: [
                  const Expanded(
                    child: Text('Messages',
                        style: TextStyle(
                            fontSize: 28,
                            fontWeight: FontWeight.w900,
                            fontStyle: FontStyle.italic)),
                  ),
                  _SquareAction(
                    tooltip: 'Search',
                    icon: Icons.search_rounded,
                    onTap: () => Navigator.of(context).push(
                      MaterialPageRoute<void>(
                        builder: (_) => MessageSearchScreen(
                          repository: ref
                              .read(mobileControllerProvider.notifier)
                              .repository,
                          scope: 'dms',
                          scopeRef: null,
                          channel: null,
                          accountRef: ref
                              .read(mobileControllerProvider.notifier)
                              .api
                              .tokens
                              ?.userRef,
                          users:
                              state.userProfiles.values.toList(growable: false),
                          onJump: (result) async {
                            final controller =
                                ref.read(mobileControllerProvider.notifier);
                            await controller.selectDm(result.channel);
                            await controller.loadAround(result.message.ref);
                            if (context.mounted) Navigator.of(context).pop();
                            onOpenChannel();
                          },
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  _SquareAction(
                    tooltip: 'New message',
                    icon: Icons.add_rounded,
                    filled: true,
                    onTap: () => _textAction(
                      context,
                      'New message',
                      '@friend@example.net',
                      (handle) async {
                        final dm = await ref
                            .read(mobileControllerProvider.notifier)
                            .repository
                            .openDm(handle);
                        await ref
                            .read(mobileControllerProvider.notifier)
                            .refreshNavigation();
                        onOpenChannel();
                        unawaited(ref
                            .read(mobileControllerProvider.notifier)
                            .selectDm(dm));
                      },
                    ),
                  ),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12),
              child: _NavRow(
                icon: Icons.people_alt_rounded,
                title: 'Friends & requests',
                badge: state.relationships
                    .where((item) => '${item['type']}' == 'pending_in')
                    .length,
                onTap: onOpenFriends,
              ),
            ),
            const Padding(
              padding: EdgeInsets.fromLTRB(18, 18, 18, 6),
              child: Align(
                alignment: Alignment.centerLeft,
                child: Text('DIRECT MESSAGES',
                    style: TextStyle(
                        color: KaedeColors.muted,
                        fontSize: 11,
                        fontWeight: FontWeight.w800,
                        letterSpacing: 1.1)),
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
                        final title = users.isEmpty
                            ? 'Conversation'
                            : users.map((user) => user.name).join(', ');
                        final person = users.isEmpty ? null : users.first;
                        return _ConversationRow(
                          avatar: _DmAvatar(channel: dm, self: state.user),
                          title: title,
                          subtitle: person?.customStatus ??
                              (person == null
                                  ? 'Direct message'
                                  : _presenceLabel(person.presence)),
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
      color: KaedeColors.canvas,
      child: Column(
        children: [
          SizedBox(
            height: 142,
            child: Stack(
              fit: StackFit.expand,
              children: [
                if (banner != null)
                  CachedNetworkImage(
                    imageUrl: '$banner',
                    fit: BoxFit.cover,
                    errorWidget: (_, __, ___) => const SizedBox.shrink(),
                  )
                else
                  const DecoratedBox(
                    decoration: BoxDecoration(
                      gradient: LinearGradient(
                        begin: Alignment.topLeft,
                        end: Alignment.bottomRight,
                        colors: [Color(0xFF243B36), Color(0xFF17181B)],
                      ),
                    ),
                  ),
                const DecoratedBox(
                  decoration: BoxDecoration(
                    gradient: LinearGradient(
                      begin: Alignment.topCenter,
                      end: Alignment.bottomCenter,
                      colors: [Colors.transparent, Color(0xE608090B)],
                    ),
                  ),
                ),
                Positioned(
                  left: 16,
                  right: 10,
                  bottom: 12,
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.end,
                    children: [
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Text(guild.name,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                    fontSize: 23, fontWeight: FontWeight.w900)),
                            Text(guild.description ?? guild.ref.domain.value,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                    color: KaedeColors.muted, fontSize: 12)),
                          ],
                        ),
                      ),
                      if (controller.api.tokens?.instance == guild.ref.domain)
                        IconButton.filledTonal(
                          tooltip: 'Guild settings',
                          onPressed: () => Navigator.of(context).push(
                            MaterialPageRoute<void>(
                              builder: (_) =>
                                  GuildManagementScreen(guild: guild),
                            ),
                          ),
                          icon: const Icon(Icons.settings_rounded, size: 20),
                        ),
                    ],
                  ),
                ),
              ],
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(10, 8, 10, 4),
            child: Row(
              children: [
                Expanded(
                  child: _NavRow(
                    icon: Icons.search_rounded,
                    title: 'Search',
                    onTap: () => Navigator.of(context).push(
                      MaterialPageRoute<void>(
                        builder: (_) => MessageSearchScreen(
                          repository: controller.repository,
                          scope: 'guild',
                          scopeRef: guild.ref,
                          channel: null,
                          accountRef: controller.api.tokens?.userRef,
                          users:
                              state.userProfiles.values.toList(growable: false),
                          onJump: (result) async {
                            await controller.selectChannel(result.channel);
                            await controller.loadAround(result.message.ref);
                            if (context.mounted) Navigator.of(context).pop();
                            onOpenChannel();
                          },
                        ),
                      ),
                    ),
                  ),
                ),
                const SizedBox(width: 8),
                _SquareAction(
                  tooltip: 'Invite people',
                  icon: Icons.person_add_alt_1_rounded,
                  onTap: () {
                    final target = channels.where((channel) =>
                        channel.type == ChannelType.text ||
                        channel.type == ChannelType.announcement);
                    final channel = target.isEmpty ? null : target.first;
                    if (channel == null) {
                      ScaffoldMessenger.of(context).showSnackBar(
                        const SnackBar(
                          content: Text(
                            'Create a text channel before creating an invite.',
                          ),
                        ),
                      );
                      return;
                    }
                    _createAndShowInvite(context, controller, guild, channel);
                  },
                ),
              ],
            ),
          ),
          Expanded(
            child: ListView(
              padding: const EdgeInsets.fromLTRB(8, 2, 8, 14),
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

  @override
  Widget build(BuildContext context) => Column(
        children: [
          InkWell(
            borderRadius: BorderRadius.circular(8),
            onTap: () => setState(() => expanded = !expanded),
            child: Padding(
              padding: const EdgeInsets.fromLTRB(10, 13, 8, 6),
              child: Row(
                children: [
                  Icon(
                      expanded
                          ? Icons.keyboard_arrow_down_rounded
                          : Icons.keyboard_arrow_right_rounded,
                      size: 18),
                  const SizedBox(width: 3),
                  Expanded(
                    child: Text(
                      (widget.category.name ?? 'Category').toUpperCase(),
                      style: const TextStyle(
                        color: KaedeColors.muted,
                        fontSize: 11,
                        fontWeight: FontWeight.w800,
                        letterSpacing: .8,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
          if (expanded)
            for (final channel in widget.children)
              _ChannelRow(
                channel: channel,
                state: widget.state,
                onTap: () => widget.onOpen(channel),
              ),
        ],
      );
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
        borderRadius: BorderRadius.circular(9),
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(9),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 10),
            child: Row(
              children: [
                _UnreadMarker(visible: highlighted),
                const SizedBox(width: 7),
                Icon(
                  channel.type == ChannelType.voice
                      ? Icons.volume_up_rounded
                      : channel.type == ChannelType.announcement
                          ? Icons.campaign_rounded
                          : Icons.tag_rounded,
                  size: 21,
                  color: highlighted || active
                      ? KaedeColors.text
                      : KaedeColors.muted,
                ),
                const SizedBox(width: 9),
                Expanded(
                  child: Text(channel.name ?? 'channel',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: highlighted || active
                            ? KaedeColors.text
                            : KaedeColors.muted,
                        fontWeight: highlighted || active
                            ? FontWeight.w700
                            : FontWeight.w500,
                      )),
                ),
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
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Material(
          color: active ? KaedeColors.selected : Colors.transparent,
          borderRadius: BorderRadius.circular(12),
          child: InkWell(
            onTap: onTap,
            borderRadius: BorderRadius.circular(12),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 9),
              child: Row(
                children: [
                  SizedBox.square(dimension: 44, child: avatar),
                  const SizedBox(width: 11),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(title,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              fontWeight: unread > 0 || mentions > 0
                                  ? FontWeight.w800
                                  : FontWeight.w600,
                              fontSize: 16,
                            )),
                        const SizedBox(height: 2),
                        Text(subtitle,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                                color: KaedeColors.muted, fontSize: 13)),
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

final class _NavRow extends StatelessWidget {
  const _NavRow({
    required this.icon,
    required this.title,
    required this.onTap,
    this.badge = 0,
  });

  final IconData icon;
  final String title;
  final VoidCallback onTap;
  final int badge;

  @override
  Widget build(BuildContext context) => Material(
        color: KaedeColors.raised,
        borderRadius: BorderRadius.circular(11),
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(11),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 13, vertical: 12),
            child: Row(
              children: [
                Icon(icon, color: KaedeColors.muted, size: 22),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(title,
                      style: const TextStyle(fontWeight: FontWeight.w700)),
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
  });

  final String tooltip;
  final IconData icon;
  final VoidCallback onTap;
  final bool filled;

  @override
  Widget build(BuildContext context) => Tooltip(
        message: tooltip,
        child: Material(
          color: filled ? KaedeColors.coral : KaedeColors.raised,
          borderRadius: BorderRadius.circular(11),
          child: InkWell(
            onTap: onTap,
            borderRadius: BorderRadius.circular(11),
            child: SizedBox.square(
              dimension: 44,
              child: Icon(icon, color: filled ? Colors.black : null),
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
  Widget build(BuildContext context, WidgetRef ref) => SafeArea(
        top: false,
        child: Material(
          color: KaedeColors.panel,
          child: InkWell(
            onTap: () => _showPresenceMenu(context, ref),
            child: Padding(
              padding: const EdgeInsets.fromLTRB(12, 9, 8, 9),
              child: Row(
                children: [
                  if (user != null) UserAvatar(user: user!, radius: 20),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(user?.name ?? 'Account',
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style:
                                const TextStyle(fontWeight: FontWeight.w800)),
                        Text(_presenceLabel(presence),
                            style: const TextStyle(
                                color: KaedeColors.muted, fontSize: 11)),
                      ],
                    ),
                  ),
                  const Icon(Icons.keyboard_arrow_up_rounded,
                      color: KaedeColors.muted),
                ],
              ),
            ),
          ),
        ),
      );

  Future<void> _showPresenceMenu(BuildContext context, WidgetRef ref) async {
    await showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      builder: (sheetContext) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            for (final status in const <PresenceStatus>[
              PresenceStatus.online,
              PresenceStatus.idle,
              PresenceStatus.dnd,
              PresenceStatus.invisible,
            ])
              ListTile(
                leading: Icon(
                  status == PresenceStatus.dnd
                      ? Icons.do_not_disturb_on_rounded
                      : status == PresenceStatus.idle
                          ? Icons.nightlight_round
                          : status == PresenceStatus.invisible
                              ? Icons.radio_button_unchecked_rounded
                              : Icons.circle,
                  color: status == PresenceStatus.online
                      ? KaedeColors.mint
                      : status == PresenceStatus.dnd
                          ? KaedeColors.danger
                          : KaedeColors.muted,
                ),
                title: Text(status == PresenceStatus.invisible
                    ? 'Invisible'
                    : _presenceLabel(status)),
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

final class _EmptyNavigation extends StatelessWidget {
  const _EmptyNavigation(
      {required this.icon, required this.title, required this.body});
  final IconData icon;
  final String title;
  final String body;

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(26),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(icon, size: 36, color: KaedeColors.muted),
              const SizedBox(height: 12),
              Text(title,
                  textAlign: TextAlign.center,
                  style: const TextStyle(fontWeight: FontWeight.w800)),
              const SizedBox(height: 5),
              Text(body,
                  textAlign: TextAlign.center,
                  style: const TextStyle(color: KaedeColors.muted)),
            ],
          ),
        ),
      );
}

String _presenceLabel(PresenceStatus status) => switch (status) {
      PresenceStatus.online => 'Online',
      PresenceStatus.idle => 'Idle',
      PresenceStatus.dnd => 'Do not disturb',
      PresenceStatus.invisible || PresenceStatus.offline => 'Offline',
    };

Future<void> _showGuildActions(BuildContext context, WidgetRef ref) async {
  await showModalBottomSheet<void>(
    context: context,
    showDragHandle: true,
    backgroundColor: KaedeColors.panel,
    builder: (sheetContext) => SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 4, 16, 22),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Text('Add a community',
                style: TextStyle(fontSize: 21, fontWeight: FontWeight.w900)),
            const SizedBox(height: 14),
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
            const SizedBox(height: 8),
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
              label: const Text('Join a guild'),
            ),
          ],
        ),
      ),
    ),
  );
}

Future<void> _createAndShowInvite(BuildContext context,
    MobileController controller, KaedeGuild guild, KaedeChannel channel) async {
  try {
    final result = await controller.repository.createInvite(guild.ref, {
      'channel_id': channel.ref.id.value,
      'channel_domain': channel.ref.domain.value,
    });
    if (!context.mounted) return;
    final code = '${result['code'] ?? ''}';
    await showDialog<void>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('Invite people'),
        content: SelectableText(code.isEmpty ? 'Invite created.' : code),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(dialogContext),
              child: const Text('Done')),
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

final class _CompactHeader extends StatelessWidget {
  const _CompactHeader({
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
          height: 62,
          child: Row(
            children: [
              if (leading != null) leading!,
              if (avatar != null) ...[
                SizedBox.square(dimension: 36, child: avatar),
                const SizedBox(width: 10),
              ],
              Expanded(
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(title,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                            fontSize: 18, fontWeight: FontWeight.w800)),
                    if (subtitle?.isNotEmpty == true)
                      Text(subtitle!,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                              color: KaedeColors.muted, fontSize: 12)),
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

final class _RailButton extends StatelessWidget {
  const _RailButton(
      {required this.label,
      required this.active,
      required this.onTap,
      this.badge = 0,
      this.unread = false,
      this.child});
  final String label;
  final bool active;
  final VoidCallback onTap;
  final int badge;
  final bool unread;
  final Widget? child;
  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Badge(
          isLabelVisible: badge > 0,
          backgroundColor: KaedeColors.danger,
          textColor: Colors.white,
          label: Text(badge > 99 ? '99+' : '$badge'),
          child: Stack(
            clipBehavior: Clip.none,
            children: [
              Material(
                color: active ? KaedeColors.coral : KaedeColors.raised,
                borderRadius: BorderRadius.circular(active ? 16 : 24),
                clipBehavior: Clip.antiAlias,
                child: InkWell(
                  onTap: onTap,
                  child: SizedBox.square(
                    dimension: 54,
                    child: child ??
                        Center(
                          child: Text(label,
                              style:
                                  const TextStyle(fontWeight: FontWeight.w800)),
                        ),
                  ),
                ),
              ),
              if (unread && badge == 0)
                Positioned(
                  left: -8,
                  top: 23,
                  child: Container(
                    width: 6,
                    height: 9,
                    decoration: BoxDecoration(
                      color: Colors.white,
                      borderRadius: BorderRadius.circular(4),
                    ),
                  ),
                ),
            ],
          ),
        ),
      );
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
    final state = ref.watch(mobileControllerProvider);
    final sections = <String, List<Map<String, Object?>>>{
      'friend': <Map<String, Object?>>[],
      'pending_in': <Map<String, Object?>>[],
      'pending_out': <Map<String, Object?>>[],
      'blocked': <Map<String, Object?>>[],
    };
    for (final relationship in state.relationships) {
      sections['${relationship['type']}']?.add(relationship);
    }
    return RefreshIndicator(
      onRefresh: ref.read(mobileControllerProvider.notifier).refreshNavigation,
      child: ListView(
        padding: const EdgeInsets.all(16),
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
          const SizedBox(height: 16),
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
          _RelationshipSection(
            title: 'Friends',
            emptyMessage: 'Friends you accept appear here.',
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
    this.emptyMessage,
  });

  final String title;
  final List<Map<String, Object?>> relationships;
  final VoidCallback onOpenChat;
  final String? emptyMessage;

  @override
  Widget build(BuildContext context, WidgetRef ref) => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(4, 14, 4, 8),
            child: Text(title.toUpperCase(),
                style: const TextStyle(
                    color: KaedeColors.muted,
                    fontWeight: FontWeight.w800,
                    letterSpacing: 1.1)),
          ),
          if (relationships.isEmpty && emptyMessage != null)
            Card(
              child: Padding(
                padding: const EdgeInsets.all(18),
                child: Text(emptyMessage!,
                    style: const TextStyle(color: KaedeColors.muted)),
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
    final user =
        ref.watch(mobileControllerProvider).userProfiles[snapshot.ref] ??
            snapshot;
    final type = '${relationship['type']}';
    return Card(
      child: ListTile(
        contentPadding: const EdgeInsets.fromLTRB(12, 6, 6, 6),
        leading: UserAvatar(user: user),
        title: Text(user.name),
        subtitle: Text(user.profileResolved
            ? user.handle
            : 'Profile unavailable · refreshes automatically'),
        onTap: () => _showProfile(context, ref, user, type, onOpenChat),
        trailing: switch (type) {
          'pending_in' => Wrap(
              children: [
                IconButton(
                  tooltip: 'Accept',
                  onPressed: () => _relationshipAction(
                      context,
                      ref,
                      () => ref
                          .read(mobileControllerProvider.notifier)
                          .repository
                          .acceptFriend(user.ref)),
                  icon: const Icon(Icons.check_circle_outline_rounded,
                      color: KaedeColors.mint),
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
  KaedeUser resolved = user;
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
  await showModalBottomSheet<void>(
    context: context,
    useSafeArea: true,
    isScrollControlled: true,
    backgroundColor: KaedeColors.panel,
    builder: (sheetContext) {
      final banner = publicAssetUri(resolved.ref.domain, resolved.bannerHash,
          variant: 'thumbnail_1024');
      return FractionallySizedBox(
        heightFactor: .76,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            SizedBox(
              height: 150,
              child: banner == null
                  ? const DecoratedBox(
                      decoration: BoxDecoration(
                        gradient: LinearGradient(colors: [
                          KaedeColors.mint,
                          KaedeColors.coral,
                        ]),
                      ),
                    )
                  : CachedNetworkImage(imageUrl: '$banner', fit: BoxFit.cover),
            ),
            Transform.translate(
              offset: const Offset(0, -38),
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 20),
                child: Align(
                    alignment: Alignment.centerLeft,
                    child: UserAvatar(user: resolved, radius: 48)),
              ),
            ),
            Expanded(
              child: ListView(
                padding: const EdgeInsets.fromLTRB(20, 0, 20, 24),
                children: [
                  Text(resolved.name,
                      style: Theme.of(context).textTheme.headlineMedium),
                  Text(
                    resolved.profileResolved
                        ? resolved.handle
                        : 'Profile unavailable · refreshes automatically',
                    style: const TextStyle(color: KaedeColors.muted),
                  ),
                  if (resolved.customStatus?.isNotEmpty == true) ...[
                    const SizedBox(height: 12),
                    Chip(label: Text(resolved.customStatus!)),
                  ],
                  if (resolved.bio?.isNotEmpty == true) ...[
                    const SizedBox(height: 18),
                    const Text('ABOUT ME',
                        style: TextStyle(
                            color: KaedeColors.muted,
                            fontWeight: FontWeight.w800,
                            letterSpacing: 1.1)),
                    const SizedBox(height: 6),
                    Text(resolved.bio!),
                  ],
                  const SizedBox(height: 22),
                  if (resolved.profileResolved) ...[
                    FilledButton.icon(
                      onPressed: () {
                        Navigator.pop(sheetContext);
                        _openDm(context, ref, resolved, onOpenChat);
                      },
                      icon: const Icon(Icons.chat_bubble_outline_rounded),
                      label: const Text('Message'),
                    ),
                    const SizedBox(height: 8),
                  ],
                  if (resolved.profileResolved &&
                      relationshipType != 'friend' &&
                      relationshipType != 'pending_out' &&
                      relationshipType != 'blocked')
                    OutlinedButton.icon(
                      onPressed: () {
                        Navigator.pop(sheetContext);
                        _relationshipAction(
                            context,
                            ref,
                            () => ref
                                .read(mobileControllerProvider.notifier)
                                .repository
                                .requestFriend(resolved.handle));
                      },
                      icon: const Icon(Icons.person_add_alt_1_rounded),
                      label: const Text('Send friend request'),
                    ),
                  if (relationshipType == 'friend')
                    OutlinedButton(
                      onPressed: () {
                        Navigator.pop(sheetContext);
                        _relationshipAction(
                            context,
                            ref,
                            () => ref
                                .read(mobileControllerProvider.notifier)
                                .repository
                                .removeRelationship(resolved.ref));
                      },
                      child: const Text('Remove friend'),
                    ),
                ],
              ),
            ),
          ],
        ),
      );
    },
  );
}

final class _DmAvatar extends StatelessWidget {
  const _DmAvatar({required this.channel, required this.self});

  final KaedeChannel channel;
  final KaedeUser? self;

  @override
  Widget build(BuildContext context) {
    final recipients = channel.recipients
        .where((user) => self == null || user.ref != self!.ref)
        .toList();
    if (recipients.isNotEmpty) return UserAvatar(user: recipients.first);
    return const CircleAvatar(child: Icon(Icons.group_rounded));
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
