import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';
import 'package:intl/intl.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/api/scheduled_events_repository.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/scheduled_events.dart';
import 'package:kaede_mobile/src/domain/stage_permissions.dart';
import 'package:kaede_mobile/src/features/shared/action_feedback.dart';
import 'package:kaede_mobile/src/features/shared/settings_ui.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

final class GuildScheduledEventsTab extends StatefulWidget {
  const GuildScheduledEventsTab({
    required this.guild,
    required this.repository,
    required this.currentUser,
    required this.canCreateExternal,
    required this.canManageExternal,
    this.startCreating = false,
    this.liveController,
    super.key,
  });

  final KaedeGuild guild;
  final KaedeRepository repository;
  final KaedeUser? currentUser;
  final bool canCreateExternal;
  final bool canManageExternal;
  final bool startCreating;
  final MobileController? liveController;

  @override
  State<GuildScheduledEventsTab> createState() =>
      _GuildScheduledEventsTabState();
}

final class _GuildScheduledEventsTabState
    extends State<GuildScheduledEventsTab> {
  var _events = const <GuildScheduledEvent>[];
  var _loading = true;
  EntityRef? _busy;
  final _subscriptions = <EntityRef, bool>{};
  var _startedInitialEditor = false;
  var _requestGeneration = 0;
  void Function()? _removeLiveListener;

  MobileState? get _liveState => widget.liveController?.currentState;

  KaedeUser? get _currentUser => _liveState?.user ?? widget.currentUser;

  KaedeGuild? get _guild {
    final state = _liveState;
    if (state == null) return widget.guild;
    return state.guilds
        .where((guild) => guild.ref == widget.guild.ref)
        .firstOrNull;
  }

  bool get _canCreateExternal {
    final guild = _guild;
    if (guild == null) return false;
    if (_liveState == null) return widget.canCreateExternal;
    return _currentUser?.ref == guild.ownerRef ||
        guild.allows(Permission.createEvents);
  }

  bool get _canManageExternal {
    final guild = _guild;
    if (guild == null) return false;
    if (_liveState == null) return widget.canManageExternal;
    return _currentUser?.ref == guild.ownerRef ||
        guild.allows(Permission.manageEvents);
  }

  @override
  void initState() {
    super.initState();
    _listenLive();
    _load();
  }

  void _listenLive() {
    _removeLiveListener?.call();
    _removeLiveListener = widget.liveController?.addListener((_) {
      if (!mounted) return;
      setState(() {
        if (_guild == null) {
          _requestGeneration += 1;
          _events = const <GuildScheduledEvent>[];
          _subscriptions.clear();
          _loading = false;
          _busy = null;
        }
      });
    }, fireImmediately: false);
  }

  @override
  void didUpdateWidget(covariant GuildScheduledEventsTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.liveController, widget.liveController)) {
      _listenLive();
    }
    if (oldWidget.guild.ref != widget.guild.ref) {
      _events = const [];
      _subscriptions.clear();
      _loading = true;
      _load();
    }
  }

  @override
  void dispose() {
    _removeLiveListener?.call();
    super.dispose();
  }

  Future<void> _load() async {
    final guild = _guild;
    if (guild == null) return;
    final generation = ++_requestGeneration;
    try {
      final events = await widget.repository.scheduledEvents(guild.ref);
      if (!mounted || generation != _requestGeneration || _guild == null) {
        return;
      }
      setState(() {
        _events = _sorted(events);
        _subscriptions
          ..clear()
          ..addEntries(
            events.map((event) => MapEntry(event.ref, event.meSubscribed)),
          );
        _loading = false;
      });
      if (widget.startCreating && !_startedInitialEditor && _canCreate) {
        _startedInitialEditor = true;
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (mounted) unawaited(_openEditor());
        });
      }
    } on Object catch (error) {
      if (!mounted || generation != _requestGeneration || _guild == null) {
        return;
      }
      setState(() => _loading = false);
      _showError(
          L10n.of(context).ui_could_not_load_scheduled_events_99abfad3, error);
    }
  }

  List<GuildScheduledEvent> _sorted(Iterable<GuildScheduledEvent> events) =>
      events.toList(growable: false)
        ..sort((left, right) {
          final time = left.startTime.compareTo(right.startTime);
          return time != 0 ? time : left.ref.wire.compareTo(right.ref.wire);
        });

  bool get _canCreate => _canCreateExternal || _eventChannelsFor().isNotEmpty;

  List<KaedeChannel> _eventChannelsFor([GuildScheduledEvent? event]) {
    final guild = _guild;
    if (guild == null) return const <KaedeChannel>[];
    final own = event?.creatorRef == _currentUser?.ref;
    return guild.channels.where((channel) {
      final expectedType = event?.entityType == ScheduledEventEntityType.stage
          ? ChannelType.stage
          : ChannelType.voice;
      if (event != null
          ? channel.type != expectedType
          : channel.type != ChannelType.voice &&
              channel.type != ChannelType.stage) {
        return false;
      }
      if (event == null) return canCreateScheduledEventInChannel(channel);
      return canManageScheduledEventInChannel(channel, ownEvent: own);
    }).toList()
      ..sort((left, right) => left.position.compareTo(right.position));
  }

  bool _canManageEvent(GuildScheduledEvent event) {
    final guild = _guild;
    if (guild == null) return false;
    final own = event.creatorRef == _currentUser?.ref;
    if (event.entityType == ScheduledEventEntityType.external) {
      return _canManageExternal || (_canCreateExternal && own);
    }
    final channel = guild.channels
        .where((candidate) => candidate.ref == event.channelRef)
        .firstOrNull;
    final expectedType = event.entityType == ScheduledEventEntityType.stage
        ? ChannelType.stage
        : ChannelType.voice;
    return channel != null &&
        channel.type == expectedType &&
        canManageScheduledEventInChannel(channel, ownEvent: own);
  }

  @override
  Widget build(BuildContext context) => _guild == null
      ? Center(
          child: Text(
              L10n.of(context).ui_this_guild_is_no_longer_available_d15ae27d))
      : Scaffold(
          backgroundColor: settingsSurface(context),
          body: RefreshIndicator(
            onRefresh: _load,
            child: _loading
                ? Center(child: CircularProgressIndicator())
                : _events.isEmpty
                    ? ListView(
                        padding: EdgeInsets.all(32),
                        children: [
                          SizedBox(height: 80),
                          Icon(
                            Icons.event_available_outlined,
                            size: 48,
                            color: Theme.of(context).colorScheme.primary,
                          ),
                          SizedBox(height: 14),
                          Text(
                            L10n.of(context).ui_no_upcoming_events_42e68679,
                            textAlign: TextAlign.center,
                            style: TextStyle(
                              fontSize: 18,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                          SizedBox(height: 6),
                          Text(
                            _canCreate
                                ? L10n.of(context)
                                    .ui_create_one_when_your_community_has_something__e8ca7f59
                                : L10n.of(context)
                                    .ui_nothing_is_scheduled_yet_a16eab07,
                            textAlign: TextAlign.center,
                            style: TextStyle(color: context.kaede.muted),
                          ),
                        ],
                      )
                    : ListView.separated(
                        padding: EdgeInsets.fromLTRB(14, 14, 14, 100),
                        itemCount: _events.length,
                        separatorBuilder: (_, __) => SizedBox(height: 10),
                        itemBuilder: (context, index) =>
                            _eventCard(_events[index]),
                      ),
          ),
          floatingActionButton: _canCreate
              ? FloatingActionButton.extended(
                  onPressed: _busy == null ? () => _openEditor() : null,
                  icon: Icon(Icons.add_rounded),
                  label: Text(L10n.of(context).ui_create_event_d0c67bc5),
                )
              : null,
        );

  Widget _eventCard(GuildScheduledEvent event) {
    final channel = _guild!.channels
        .where((candidate) => candidate.ref == event.channelRef)
        .firstOrNull;
    final live = event.status == ScheduledEventStatus.active;
    final busy = _busy == event.ref;
    final coverUri = publicAssetUri(
      event.ref.domain,
      event.imageHash,
      variant: 'thumbnail_1024',
    );
    final recurrence = scheduledEventRecurrenceLabel(event.recurrenceRule);
    return Card(
      margin: EdgeInsets.zero,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(14),
        side: live
            ? BorderSide(color: Theme.of(context).colorScheme.primary)
            : BorderSide.none,
      ),
      child: Padding(
        padding: EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (coverUri != null) ...[
              ClipRRect(
                borderRadius: BorderRadius.circular(10),
                child: Image.network(
                  coverUri.toString(),
                  height: 180,
                  fit: BoxFit.cover,
                  errorBuilder: (_, __, ___) => SizedBox.shrink(),
                ),
              ),
              SizedBox(height: 12),
            ],
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Container(
                  width: 48,
                  padding: EdgeInsets.symmetric(vertical: 8),
                  decoration: BoxDecoration(
                    color: Theme.of(context)
                        .colorScheme
                        .primary
                        .withValues(alpha: .13),
                    borderRadius: BorderRadius.circular(10),
                  ),
                  child: Column(
                    children: [
                      Text(
                        DateFormat.MMM().format(event.startTime).toUpperCase(),
                        style: TextStyle(
                          color: Theme.of(context).colorScheme.primary,
                          fontSize: 10,
                          fontWeight: FontWeight.w800,
                        ),
                      ),
                      Text(
                        L10n.of(context).ui_value0_26e9163c(
                            (event.startTime.day).toString()),
                        style: TextStyle(
                          fontSize: 20,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ],
                  ),
                ),
                SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Wrap(
                        spacing: 8,
                        crossAxisAlignment: WrapCrossAlignment.center,
                        children: [
                          Text(
                            event.name,
                            style: TextStyle(
                              fontSize: 16,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                          _statusChip(event.status),
                        ],
                      ),
                      SizedBox(height: 3),
                      Text(
                        DateFormat.yMMMd().add_jm().format(event.startTime),
                        style: TextStyle(fontWeight: FontWeight.w600),
                      ),
                      if (recurrence != null) ...[
                        SizedBox(height: 2),
                        Text(
                          recurrence,
                          style: TextStyle(color: context.kaede.muted),
                        ),
                      ],
                      SizedBox(height: 3),
                      Text(
                        event.entityType == ScheduledEventEntityType.stage
                            ? L10n.of(context).ui_stage_value0_8c82c30d(
                                (channel?.name ?? 'Unavailable channel')
                                    .toString())
                            : event.entityType == ScheduledEventEntityType.voice
                                ? L10n.of(context).ui_voice_value0_1a9aab79(
                                    (channel?.name ?? 'Unavailable channel')
                                        .toString())
                                : L10n.of(context).ui_external_value0_4ec4fb6a(
                                    (event.location ?? 'Location unavailable')
                                        .toString()),
                        style: TextStyle(color: context.kaede.muted),
                      ),
                      if (event.description?.trim().isNotEmpty == true) ...[
                        SizedBox(height: 6),
                        Text(event.description!),
                      ],
                      SizedBox(height: 10),
                      Wrap(
                        spacing: 6,
                        runSpacing: 6,
                        children: [
                          ActionButton(
                            kind: ActionButtonKind.outlined,
                            onPressed:
                                busy ? null : () => _toggleSubscription(event),
                            icon: Icon(
                              _subscriptions[event.ref] == true
                                  ? Icons.notifications_active_outlined
                                  : Icons.notifications_none_rounded,
                              size: 17,
                            ),
                            label: Text(
                              _subscriptions[event.ref] == true
                                  ? L10n.of(context).ui_following_9fa2c938
                                  : L10n.of(context).ui_notify_me_33a8c85a,
                            ),
                          ),
                          ActionButton(
                            kind: ActionButtonKind.text,
                            onPressed:
                                busy ? null : () => _showSubscribers(event),
                            icon: Icon(Icons.people_outline_rounded, size: 17),
                            label: Text(L10n.of(context)
                                .ui_value0_interested_33671f07(
                                    (event.userCount).toString())),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
                if (_canManageEvent(event))
                  PopupMenuButton<String>(
                    enabled: !busy,
                    tooltip: L10n.of(context).ui_event_actions_802fbccc,
                    onSelected: (action) => _eventAction(event, action),
                    itemBuilder: (_) => [
                      PopupMenuItem(
                          value: 'edit',
                          child: Text(L10n.of(context).ui_edit_c2c76cb1)),
                      if (event.status == ScheduledEventStatus.scheduled) ...[
                        PopupMenuItem(
                            value: 'start',
                            child:
                                Text(L10n.of(context).ui_start_now_8235342f)),
                        PopupMenuItem(
                          value: 'cancel',
                          child:
                              Text(L10n.of(context).ui_cancel_event_5413821b),
                        ),
                      ],
                      if (event.status == ScheduledEventStatus.active)
                        PopupMenuItem(
                          value: 'complete',
                          child:
                              Text(L10n.of(context).ui_complete_event_4468bef2),
                        ),
                      PopupMenuDivider(),
                      PopupMenuItem(
                        value: 'delete',
                        child: Text(
                          L10n.of(context).ui_delete_permanently_d8100383,
                          style: TextStyle(color: context.kaede.danger),
                        ),
                      ),
                    ],
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _statusChip(ScheduledEventStatus status) => Container(
        padding: EdgeInsets.symmetric(horizontal: 7, vertical: 2),
        decoration: BoxDecoration(
          color: Theme.of(context).colorScheme.primary.withValues(alpha: .13),
          borderRadius: BorderRadius.circular(99),
        ),
        child: Text(
          switch (status) {
            ScheduledEventStatus.active => L10n.of(context).ui_live_45424caf,
            ScheduledEventStatus.completed =>
              L10n.of(context).ui_completed_61fa0f72,
            ScheduledEventStatus.canceled =>
              L10n.of(context).ui_canceled_b57caada,
            ScheduledEventStatus.scheduled =>
              L10n.of(context).ui_scheduled_993163d8,
          },
          style: TextStyle(
            color: Theme.of(context).colorScheme.primary,
            fontSize: 10,
            fontWeight: FontWeight.w800,
          ),
        ),
      );

  Future<void> _eventAction(GuildScheduledEvent event, String action) async {
    switch (action) {
      case 'edit':
        await _openEditor(event);
        return;
      case 'start':
        await _transition(event, ScheduledEventStatus.active);
        return;
      case 'complete':
        await _transition(event, ScheduledEventStatus.completed);
        return;
      case 'cancel':
        if (await _confirm(
          'Cancel event?',
          'Subscribers will no longer see “${event.name}”.',
          'Cancel event',
        )) {
          await _transition(event, ScheduledEventStatus.canceled);
        }
        return;
      case 'delete':
        if (await _confirm(
          'Delete event?',
          '“${event.name}” will be deleted permanently.',
          'Delete',
        )) {
          await _delete(event);
        }
        return;
    }
  }

  Future<void> _openEditor([GuildScheduledEvent? event]) async {
    if (event != null && !_canManageEvent(event) ||
        event == null && !_canCreate) {
      return;
    }
    final result = await showScheduledEventEditor(
      context,
      event: event,
      eventChannels: _eventChannelsFor(event),
      allowExternal: _canCreateExternal ||
          event?.entityType == ScheduledEventEntityType.external,
    );
    if (result == null || !mounted) return;
    final guild = _guild;
    final targetAllowed =
        result.draft.entityType == ScheduledEventEntityType.external
            ? event == null
                ? _canCreateExternal
                : _canManageEvent(event)
            : _eventChannelsFor(event).any(
                (channel) => channel.ref == result.draft.channelRef,
              );
    if (guild == null ||
        !targetAllowed ||
        (event != null && !_canManageEvent(event))) {
      _showError(
        L10n.of(context).ui_could_not_save_the_scheduled_event_dd7ef894,
        UserInputException('Event permissions changed. Try again.'),
      );
      return;
    }
    setState(() => _busy = event?.ref ?? guild.ref);
    var detailsSaved = false;
    try {
      var saved = event == null
          ? await widget.repository
              .createScheduledEvent(guild.ref, result.draft)
          : await widget.repository
              .updateScheduledEvent(guild.ref, event, result.draft);
      if (!mounted) return;
      setState(() {
        _events = _sorted([
          ..._events.where((candidate) => candidate.ref != saved.ref),
          saved,
        ]);
      });
      detailsSaved = true;
      if (!_canManageEvent(saved)) return;
      if (result.coverFile case final cover?) {
        saved = await widget.repository.uploadScheduledEventImage(
          guild: guild.ref,
          event: saved,
          filename: cover.name,
          contentType: cover.mimeType,
          file: File(cover.path),
        );
      } else if (result.removeCover && saved.imageHash != null) {
        saved = await widget.repository.deleteScheduledEventImage(
          guild.ref,
          saved,
        );
      }
      if (!mounted) return;
      setState(() {
        _events = _sorted([
          ..._events.where((candidate) => candidate.ref != saved.ref),
          saved,
        ]);
      });
      _showNotice(event == null
          ? L10n.of(context).ui_event_created_af2b8d87
          : L10n.of(context).ui_event_updated_06a28672);
    } on Object catch (error) {
      _showError(
        detailsSaved
            ? L10n.of(context)
                .ui_the_event_was_saved_but_its_cover_could_not_b_a6bb3e92
            : L10n.of(context).ui_could_not_save_the_scheduled_event_dd7ef894,
        error,
      );
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  Future<void> _transition(
    GuildScheduledEvent event,
    ScheduledEventStatus status,
  ) async {
    final guild = _guild;
    if (guild == null || !_canManageEvent(event)) return;
    setState(() => _busy = event.ref);
    try {
      if (!_canManageEvent(event)) return;
      final updated = await widget.repository.transitionScheduledEvent(
        guild.ref,
        event,
        status,
      );
      if (!mounted) return;
      setState(() {
        _events = status == ScheduledEventStatus.completed ||
                status == ScheduledEventStatus.canceled
            ? _events.where((candidate) => candidate.ref != event.ref).toList()
            : _sorted([
                ..._events.where((candidate) => candidate.ref != event.ref),
                updated,
              ]);
      });
      _showNotice(switch (status) {
        ScheduledEventStatus.active =>
          L10n.of(context).ui_event_started_be79e1a8,
        ScheduledEventStatus.completed =>
          L10n.of(context).ui_event_completed_9f1611fc,
        ScheduledEventStatus.canceled =>
          L10n.of(context).ui_event_canceled_d1a5a2c4,
        ScheduledEventStatus.scheduled =>
          L10n.of(context).ui_event_updated_06a28672,
      });
    } on Object catch (error) {
      _showError(L10n.of(context).ui_could_not_update_the_event_status_2b5833ff,
          error);
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  Future<void> _delete(GuildScheduledEvent event) async {
    final guild = _guild;
    if (guild == null || !_canManageEvent(event)) return;
    setState(() => _busy = event.ref);
    try {
      if (!_canManageEvent(event)) return;
      await widget.repository.deleteScheduledEvent(guild.ref, event);
      if (!mounted) return;
      setState(() => _events =
          _events.where((candidate) => candidate.ref != event.ref).toList());
      _showNotice(L10n.of(context).ui_scheduled_event_deleted_db644ba9);
    } on Object catch (error) {
      _showError(
          L10n.of(context).ui_could_not_delete_the_scheduled_event_6b996da6,
          error);
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  Future<void> _toggleSubscription(GuildScheduledEvent event) async {
    final guild = _guild;
    if (guild == null) return;
    final known = _subscriptions[event.ref] ?? event.meSubscribed;
    final next = !known;
    setState(() => _busy = event.ref);
    try {
      await widget.repository.setScheduledEventSubscription(
        guild.ref,
        event,
        subscribed: next,
      );
      if (!mounted) return;
      setState(() {
        _subscriptions[event.ref] = next;
        final delta = next ? 1 : -1;
        _events = _events
            .map((candidate) => candidate.ref == event.ref
                ? candidate.copyWith(
                    userCount:
                        (candidate.userCount + delta).clamp(0, 1 << 31).toInt(),
                    meSubscribed: next,
                  )
                : candidate)
            .toList();
      });
      _showNotice(
        next
            ? L10n.of(context).ui_you_will_be_notified_about_this_event_9a9b8144
            : L10n.of(context).ui_event_notifications_turned_off_09c23258,
      );
    } on Object catch (error) {
      _showError(
          L10n.of(context).ui_could_not_update_event_notifications_487032b0,
          error);
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  Future<void> _showSubscribers(GuildScheduledEvent event) async {
    final guild = _guild;
    if (guild == null) return;
    var subscribers = <ScheduledEventSubscriber>[];
    var loading = true;
    var initialLoadRequested = false;
    Object? failure;
    Future<void> loadMore(StateSetter update) async {
      update(() {
        loading = true;
        failure = null;
      });
      try {
        final page = await widget.repository.scheduledEventSubscribers(
          guild.ref,
          event,
          after: subscribers.lastOrNull?.user.ref,
        );
        subscribers = [
          ...subscribers,
          ...page.where(
            (item) => !subscribers
                .any((existing) => existing.user.ref == item.user.ref),
          ),
        ];
        if (subscribers.any((item) => item.user.ref == _currentUser?.ref)) {
          _subscriptions[event.ref] = true;
        } else if (page.length < 100 || subscribers.length >= event.userCount) {
          _subscriptions[event.ref] = false;
        }
      } on Object catch (error) {
        failure = error;
      } finally {
        loading = false;
        update(() {});
      }
    }

    if (!mounted) return;
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (sheetContext) => StatefulBuilder(
        builder: (context, update) {
          if (!initialLoadRequested) {
            initialLoadRequested = true;
            WidgetsBinding.instance
                .addPostFrameCallback((_) => loadMore(update));
          }
          return SafeArea(
            child: SizedBox(
              height: MediaQuery.sizeOf(context).height * .65,
              child: Column(
                children: [
                  ListTile(
                    title:
                        Text(L10n.of(context).ui_interested_members_3674e2e9),
                    subtitle: Text(L10n.of(context).ui_value0_total_7e2f23b6(
                        (event.userCount).toString())),
                  ),
                  Expanded(
                    child: failure != null && subscribers.isEmpty
                        ? Center(
                            child: Padding(
                              padding: EdgeInsets.all(24),
                              child: Column(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  Text(
                                    userFacingError(
                                      failure!,
                                      summary: L10n.of(context)
                                          .ui_could_not_load_subscribers_947f38b2,
                                    ),
                                    textAlign: TextAlign.center,
                                  ),
                                  SizedBox(height: 12),
                                  ActionButton(
                                    onPressed:
                                        loading ? null : () => loadMore(update),
                                    child: Text(
                                        L10n.of(context).ui_try_again_213e90fa),
                                  ),
                                ],
                              ),
                            ),
                          )
                        : ListView(
                            children: [
                              for (final subscriber in subscribers)
                                ListTile(
                                  leading: CircleAvatar(
                                    child: Icon(Icons.person_outline_rounded),
                                  ),
                                  title: Text(
                                    subscriber.member?.nickname ??
                                        subscriber.user.name,
                                  ),
                                  subtitle: Text(subscriber.user.handle),
                                ),
                              if (subscribers.isEmpty && !loading)
                                Padding(
                                  padding: EdgeInsets.all(28),
                                  child: Text(
                                    L10n.of(context)
                                        .ui_no_one_has_followed_this_event_yet_d98f29ba,
                                    textAlign: TextAlign.center,
                                    style:
                                        TextStyle(color: context.kaede.muted),
                                  ),
                                ),
                              if (subscribers.length < event.userCount)
                                Padding(
                                  padding: EdgeInsets.all(16),
                                  child: ActionButton(
                                    kind: ActionButtonKind.outlined,
                                    onPressed:
                                        loading ? null : () => loadMore(update),
                                    child: Text(
                                      loading
                                          ? L10n.of(context).ui_loading_c4e2f181
                                          : L10n.of(context)
                                              .ui_load_more_2b7b053e,
                                    ),
                                  ),
                                ),
                            ],
                          ),
                  ),
                ],
              ),
            ),
          );
        },
      ),
    );
    if (mounted) setState(() {});
  }

  Future<bool> _confirm(
    String title,
    String message,
    String label,
  ) async =>
      await showDialog<bool>(
        context: context,
        builder: (dialogContext) => AlertDialog(
          title: Text(title),
          content: Text(message),
          actions: [
            ActionButton(
              kind: ActionButtonKind.text,
              onPressed: () => Navigator.pop(dialogContext, false),
              child: Text(L10n.of(context).ui_keep_event_fcbe4da0),
            ),
            ActionButton(
              onPressed: () => Navigator.pop(dialogContext, true),
              child: Text(label),
            ),
          ],
        ),
      ) ??
      false;

  void _showNotice(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
  }

  void _showError(String summary, Object error) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(userFacingError(error, summary: summary)),
      backgroundColor: context.kaede.danger,
    ));
  }
}

final class ScheduledEventEditorResult {
  ScheduledEventEditorResult({
    required this.draft,
    this.coverFile,
    this.removeCover = false,
  });

  final ScheduledEventDraft draft;
  final XFile? coverFile;
  final bool removeCover;
}

Future<ScheduledEventEditorResult?> showScheduledEventEditor(
  BuildContext context, {
  GuildScheduledEvent? event,
  required List<KaedeChannel> eventChannels,
  bool allowExternal = true,
}) async {
  final name = TextEditingController(text: event?.name ?? '');
  final description = TextEditingController(text: event?.description ?? '');
  final location = TextEditingController(text: event?.location ?? '');
  var entityType = event?.entityType ?? ScheduledEventEntityType.voice;
  var channelRef = event?.channelRef ??
      eventChannels
          .where((channel) => channel.type == ChannelType.voice)
          .firstOrNull
          ?.ref;
  var startTime = event?.startTime ??
      DateTime.now().add(Duration(hours: 1)).copyWith(
            second: 0,
            millisecond: 0,
            microsecond: 0,
          );
  DateTime? endTime = event?.endTime;
  var recurrence = scheduledEventRecurrencePreset(event?.recurrenceRule);
  XFile? coverFile;
  var removeCover = false;
  final existingCover = event == null
      ? null
      : publicAssetUri(
          event.ref.domain,
          event.imageHash,
          variant: 'thumbnail_1024',
        );
  String draftState() => jsonEncode([
        name.text.trim(),
        description.text.trim(),
        location.text.trim(),
        entityType.name,
        channelRef?.wire,
        startTime.toIso8601String(),
        endTime?.toIso8601String(),
        recurrence.name
      ]);
  final savedDraft = draftState();
  String? validation;
  final result = await showModalBottomSheet<ScheduledEventEditorResult>(
    context: context,
    isScrollControlled: true,
    showDragHandle: true,
    builder: (sheetContext) => StatefulBuilder(
      builder: (context, update) {
        final matchingChannels = eventChannels
            .where((channel) =>
                channel.type ==
                (entityType == ScheduledEventEntityType.stage
                    ? ChannelType.stage
                    : ChannelType.voice))
            .toList(growable: false);
        Future<void> pickStart() async {
          final value = await _pickDateTime(context, startTime);
          if (value != null) update(() => startTime = value);
        }

        Future<void> pickEnd() async {
          final value = await _pickDateTime(
            context,
            endTime ?? startTime.add(Duration(hours: 1)),
          );
          if (value != null) update(() => endTime = value);
        }

        void submit() {
          try {
            final draft = ScheduledEventDraft(
              name: name.text,
              description: description.text,
              entityType: entityType,
              channelRef: entityType != ScheduledEventEntityType.external
                  ? channelRef
                  : null,
              location: location.text,
              startTime: startTime,
              endTime: endTime,
              recurrence: recurrence,
            );
            draft.toCreateJson();
            Navigator.pop(
              sheetContext,
              ScheduledEventEditorResult(
                draft: draft,
                coverFile: coverFile,
                removeCover: removeCover,
              ),
            );
          } on Object catch (error) {
            update(() => validation = userFacingError(
                  error,
                  summary: L10n.of(context).ui_check_the_event_details_0a3aa752,
                ));
          }
        }

        return SafeArea(
          child: Padding(
            padding: EdgeInsets.only(
              left: 18,
              right: 18,
              bottom: MediaQuery.viewInsetsOf(context).bottom + 18,
            ),
            child: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    event == null
                        ? L10n.of(context).ui_create_event_d0c67bc5
                        : L10n.of(context).ui_edit_event_041c41b1,
                    style: Theme.of(context).textTheme.headlineSmall,
                  ),
                  SizedBox(height: 4),
                  Text(
                    L10n.of(context)
                        .ui_choose_a_stage_voice_channel_or_external_loca_539c15cd,
                    style: TextStyle(color: context.kaede.muted),
                  ),
                  SizedBox(height: 16),
                  TextField(
                    controller: name,
                    maxLength: 100,
                    decoration: InputDecoration(
                        labelText: L10n.of(context).ui_name_0fe07306),
                  ),
                  SizedBox(height: 10),
                  if (coverFile != null ||
                      (existingCover != null && !removeCover)) ...[
                    ClipRRect(
                      borderRadius: BorderRadius.circular(10),
                      child: coverFile != null
                          ? Image.file(
                              File(coverFile!.path),
                              height: 170,
                              width: double.infinity,
                              fit: BoxFit.cover,
                            )
                          : Image.network(
                              existingCover.toString(),
                              height: 170,
                              width: double.infinity,
                              fit: BoxFit.cover,
                              errorBuilder: (_, __, ___) => SizedBox.shrink(),
                            ),
                    ),
                    SizedBox(height: 8),
                  ],
                  Wrap(
                    spacing: 8,
                    children: [
                      ActionButton(
                        kind: ActionButtonKind.outlined,
                        onPressed: () async {
                          final selected = await ImagePicker().pickImage(
                            source: ImageSource.gallery,
                          );
                          if (selected != null) {
                            update(() {
                              coverFile = selected;
                              removeCover = false;
                            });
                          }
                        },
                        icon: Icon(Icons.image_outlined),
                        label: Text(
                          coverFile == null && existingCover == null
                              ? L10n.of(context).ui_add_cover_image_7de4d79a
                              : L10n.of(context).ui_change_cover_image_b8e5c25f,
                        ),
                      ),
                      if (coverFile != null ||
                          (existingCover != null && !removeCover))
                        ActionButton(
                          kind: ActionButtonKind.text,
                          onPressed: () => update(() {
                            coverFile = null;
                            removeCover = event?.imageHash != null;
                          }),
                          child:
                              Text(L10n.of(context).ui_remove_cover_4c8c602e),
                        ),
                    ],
                  ),
                  Text(
                    L10n.of(context)
                        .ui_png_jpeg_gif_or_webp_up_to_10_mib_98f2b31b,
                    style: TextStyle(color: context.kaede.muted, fontSize: 12),
                  ),
                  SizedBox(height: 10),
                  TextField(
                    controller: description,
                    maxLength: 1000,
                    minLines: 2,
                    maxLines: 4,
                    decoration: InputDecoration(
                      labelText:
                          L10n.of(context).ui_description_optional_31e71764,
                    ),
                  ),
                  SizedBox(height: 10),
                  DropdownButtonFormField<ScheduledEventEntityType>(
                    initialValue: entityType,
                    decoration: InputDecoration(
                        labelText: L10n.of(context).ui_event_type_015adcc5),
                    items: [
                      DropdownMenuItem(
                        value: ScheduledEventEntityType.stage,
                        child: Text(L10n.of(context).ui_stage_channel_8ff46126),
                      ),
                      DropdownMenuItem(
                        value: ScheduledEventEntityType.voice,
                        child: Text(L10n.of(context).ui_voice_channel_d243a4f2),
                      ),
                      if (allowExternal)
                        DropdownMenuItem(
                          value: ScheduledEventEntityType.external,
                          child: Text(L10n.of(context).ui_external_29951daa),
                        ),
                    ],
                    onChanged: event?.status == ScheduledEventStatus.active
                        ? null
                        : (value) => update(() {
                              entityType = value ?? entityType;
                              final expected =
                                  entityType == ScheduledEventEntityType.stage
                                      ? ChannelType.stage
                                      : ChannelType.voice;
                              channelRef = eventChannels
                                  .where((channel) => channel.type == expected)
                                  .firstOrNull
                                  ?.ref;
                              validation = null;
                            }),
                  ),
                  SizedBox(height: 10),
                  if (entityType != ScheduledEventEntityType.external)
                    DropdownButtonFormField<EntityRef>(
                      initialValue: matchingChannels
                              .any((channel) => channel.ref == channelRef)
                          ? channelRef
                          : null,
                      decoration: InputDecoration(
                        labelText: entityType == ScheduledEventEntityType.stage
                            ? L10n.of(context).ui_stage_channel_8ff46126
                            : L10n.of(context).ui_voice_channel_d243a4f2,
                        helperText: matchingChannels.isEmpty
                            ? L10n.of(context)
                                .ui_you_need_create_manage_events_view_channel_an_fddba802
                            : null,
                      ),
                      items: [
                        for (final channel in matchingChannels)
                          DropdownMenuItem(
                            value: channel.ref,
                            child: Text(channel.name ?? 'Voice channel'),
                          ),
                      ],
                      onChanged: (value) => update(() => channelRef = value),
                    )
                  else
                    TextField(
                      controller: location,
                      maxLength: 100,
                      decoration: InputDecoration(
                        labelText:
                            L10n.of(context).ui_location_or_link_8ad43dc1,
                      ),
                    ),
                  SizedBox(height: 10),
                  ListTile(
                    contentPadding: EdgeInsets.zero,
                    title: Text(L10n.of(context).ui_starts_37463de4),
                    subtitle:
                        Text(DateFormat.yMMMd().add_jm().format(startTime)),
                    trailing: Icon(Icons.edit_calendar_outlined),
                    onTap: event?.status == ScheduledEventStatus.active
                        ? null
                        : pickStart,
                  ),
                  DropdownButtonFormField<ScheduledEventRecurrencePreset>(
                    initialValue: recurrence,
                    decoration: InputDecoration(
                        labelText: L10n.of(context).ui_repeat_06626d4a),
                    items: [
                      DropdownMenuItem(
                        value: ScheduledEventRecurrencePreset.none,
                        child:
                            Text(L10n.of(context).ui_does_not_repeat_9cd5f984),
                      ),
                      DropdownMenuItem(
                        value: ScheduledEventRecurrencePreset.daily,
                        child: Text(L10n.of(context).ui_daily_bc3889be),
                      ),
                      DropdownMenuItem(
                        value: ScheduledEventRecurrencePreset.weekly,
                        child: Text(L10n.of(context).ui_weekly_417cc4ce),
                      ),
                      DropdownMenuItem(
                        value: ScheduledEventRecurrencePreset.biweekly,
                        child: Text(L10n.of(context).ui_every_2_weeks_500cd833),
                      ),
                      DropdownMenuItem(
                        value: ScheduledEventRecurrencePreset.monthly,
                        child: Text(L10n.of(context).ui_monthly_1640a356),
                      ),
                      DropdownMenuItem(
                        value: ScheduledEventRecurrencePreset.yearly,
                        child: Text(L10n.of(context).ui_yearly_3e5922ab),
                      ),
                    ],
                    onChanged: event?.status == ScheduledEventStatus.active
                        ? null
                        : (value) => update(
                              () => recurrence = value ?? recurrence,
                            ),
                  ),
                  SizedBox(height: 10),
                  ListTile(
                    contentPadding: EdgeInsets.zero,
                    title: Text(
                      entityType != ScheduledEventEntityType.external
                          ? L10n.of(context).ui_ends_optional_a76279e6
                          : L10n.of(context).ui_ends_fe231bbb,
                    ),
                    subtitle: Text(
                      endTime == null
                          ? L10n.of(context).ui_no_end_time_8247d188
                          : DateFormat.yMMMd().add_jm().format(endTime!),
                    ),
                    trailing: Wrap(
                      children: [
                        if (endTime != null &&
                            entityType != ScheduledEventEntityType.external)
                          ActionButton(
                            kind: ActionButtonKind.icon,
                            tooltip:
                                L10n.of(context).ui_clear_end_time_053f4070,
                            onPressed: () => update(() => endTime = null),
                            icon: Icon(Icons.clear_rounded),
                          ),
                        ActionButton(
                          kind: ActionButtonKind.icon,
                          tooltip: L10n.of(context).ui_choose_end_time_8fb40fee,
                          onPressed: pickEnd,
                          icon: Icon(Icons.edit_calendar_outlined),
                        ),
                      ],
                    ),
                  ),
                  if (validation != null) ...[
                    SizedBox(height: 8),
                    Text(
                      validation!,
                      style: TextStyle(color: context.kaede.danger),
                    ),
                  ],
                  SizedBox(height: 16),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.end,
                    children: [
                      ActionButton(
                        kind: ActionButtonKind.text,
                        onPressed: () => Navigator.pop(sheetContext),
                        child: Text(L10n.of(context).ui_cancel_35afca3b),
                      ),
                      SizedBox(width: 8),
                      SaveButton(
                        controllers: [name, description, location],
                        hasChanges: () =>
                            event == null ||
                            draftState() != savedDraft ||
                            coverFile != null ||
                            removeCover,
                        onPressed: submit,
                        child: Text(event == null
                            ? L10n.of(context).ui_create_event_d0c67bc5
                            : L10n.of(context).ui_save_4d2d5d68),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
        );
      },
    ),
  );
  name.dispose();
  description.dispose();
  location.dispose();
  return result;
}

Future<DateTime?> _pickDateTime(
  BuildContext context,
  DateTime initial,
) async {
  final date = await showDatePicker(
    context: context,
    initialDate: initial,
    firstDate: DateTime.now().subtract(Duration(days: 1)),
    lastDate: DateTime.now().add(Duration(days: 3650)),
  );
  if (date == null || !context.mounted) return null;
  final time = await showTimePicker(
    context: context,
    initialTime: TimeOfDay.fromDateTime(initial),
  );
  if (time == null) return null;
  return DateTime(date.year, date.month, date.day, time.hour, time.minute);
}
