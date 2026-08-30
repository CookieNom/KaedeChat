import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';
import 'package:intl/intl.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/api/scheduled_events_repository.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/scheduled_events.dart';
import 'package:kaede_mobile/src/domain/stage_permissions.dart';
import 'package:kaede_mobile/src/features/shared/settings_ui.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

final class GuildScheduledEventsTab extends StatefulWidget {
  const GuildScheduledEventsTab({
    required this.guild,
    required this.repository,
    required this.currentUser,
    required this.canCreateExternal,
    required this.canManageExternal,
    this.startCreating = false,
    super.key,
  });

  final KaedeGuild guild;
  final KaedeRepository repository;
  final KaedeUser? currentUser;
  final bool canCreateExternal;
  final bool canManageExternal;
  final bool startCreating;

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

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant GuildScheduledEventsTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.guild.ref != widget.guild.ref) {
      _events = const [];
      _subscriptions.clear();
      _loading = true;
      _load();
    }
  }

  Future<void> _load() async {
    try {
      final events = await widget.repository.scheduledEvents(widget.guild.ref);
      if (!mounted) return;
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
      if (!mounted) return;
      setState(() => _loading = false);
      _showError('Could not load scheduled events', error);
    }
  }

  List<GuildScheduledEvent> _sorted(Iterable<GuildScheduledEvent> events) =>
      events.toList(growable: false)
        ..sort((left, right) {
          final time = left.startTime.compareTo(right.startTime);
          return time != 0 ? time : left.ref.wire.compareTo(right.ref.wire);
        });

  bool get _canCreate =>
      widget.canCreateExternal || _eventChannelsFor().isNotEmpty;

  List<KaedeChannel> _eventChannelsFor([GuildScheduledEvent? event]) {
    final own = event?.creatorRef == widget.currentUser?.ref;
    return widget.guild.channels.where((channel) {
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
    final own = event.creatorRef == widget.currentUser?.ref;
    if (event.entityType == ScheduledEventEntityType.external) {
      return widget.canManageExternal || (widget.canCreateExternal && own);
    }
    final channel = widget.guild.channels
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
  Widget build(BuildContext context) => Scaffold(
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
                          'No upcoming events',
                          textAlign: TextAlign.center,
                          style: TextStyle(
                            fontSize: 18,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                        SizedBox(height: 6),
                        Text(
                          _canCreate
                              ? 'Create one when your community has something planned.'
                              : 'Nothing is scheduled yet.',
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
                label: Text('Create event'),
              )
            : null,
      );

  Widget _eventCard(GuildScheduledEvent event) {
    final channel = widget.guild.channels
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
                        '${event.startTime.day}',
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
                            ? 'Stage · ${channel?.name ?? 'Unavailable channel'}'
                            : event.entityType == ScheduledEventEntityType.voice
                                ? 'Voice · ${channel?.name ?? 'Unavailable channel'}'
                                : 'External · ${event.location ?? 'Location unavailable'}',
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
                          OutlinedButton.icon(
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
                                  ? 'Following'
                                  : 'Notify me',
                            ),
                          ),
                          TextButton.icon(
                            onPressed:
                                busy ? null : () => _showSubscribers(event),
                            icon: Icon(Icons.people_outline_rounded, size: 17),
                            label: Text('${event.userCount} interested'),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
                if (_canManageEvent(event))
                  PopupMenuButton<String>(
                    enabled: !busy,
                    tooltip: 'Event actions',
                    onSelected: (action) => _eventAction(event, action),
                    itemBuilder: (_) => [
                      PopupMenuItem(value: 'edit', child: Text('Edit')),
                      if (event.status == ScheduledEventStatus.scheduled) ...[
                        PopupMenuItem(value: 'start', child: Text('Start now')),
                        PopupMenuItem(
                          value: 'cancel',
                          child: Text('Cancel event'),
                        ),
                      ],
                      if (event.status == ScheduledEventStatus.active)
                        PopupMenuItem(
                          value: 'complete',
                          child: Text('Complete event'),
                        ),
                      PopupMenuDivider(),
                      PopupMenuItem(
                        value: 'delete',
                        child: Text(
                          'Delete permanently',
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
            ScheduledEventStatus.active => 'LIVE',
            ScheduledEventStatus.completed => 'COMPLETED',
            ScheduledEventStatus.canceled => 'CANCELED',
            ScheduledEventStatus.scheduled => 'SCHEDULED',
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
    final result = await showScheduledEventEditor(
      context,
      event: event,
      eventChannels: _eventChannelsFor(event),
      allowExternal: widget.canCreateExternal ||
          event?.entityType == ScheduledEventEntityType.external,
    );
    if (result == null || !mounted) return;
    setState(() => _busy = event?.ref ?? widget.guild.ref);
    var detailsSaved = false;
    try {
      var saved = event == null
          ? await widget.repository
              .createScheduledEvent(widget.guild.ref, result.draft)
          : await widget.repository
              .updateScheduledEvent(widget.guild.ref, event, result.draft);
      if (!mounted) return;
      setState(() {
        _events = _sorted([
          ..._events.where((candidate) => candidate.ref != saved.ref),
          saved,
        ]);
      });
      detailsSaved = true;
      if (result.coverFile case final cover?) {
        saved = await widget.repository.uploadScheduledEventImage(
          guild: widget.guild.ref,
          event: saved,
          filename: cover.name,
          contentType: cover.mimeType,
          file: File(cover.path),
        );
      } else if (result.removeCover && saved.imageHash != null) {
        saved = await widget.repository.deleteScheduledEventImage(
          widget.guild.ref,
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
      _showNotice(event == null ? 'Event created.' : 'Event updated.');
    } on Object catch (error) {
      _showError(
        detailsSaved
            ? 'The event was saved, but its cover could not be updated'
            : 'Could not save the scheduled event',
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
    setState(() => _busy = event.ref);
    try {
      final updated = await widget.repository.transitionScheduledEvent(
        widget.guild.ref,
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
        ScheduledEventStatus.active => 'Event started.',
        ScheduledEventStatus.completed => 'Event completed.',
        ScheduledEventStatus.canceled => 'Event canceled.',
        ScheduledEventStatus.scheduled => 'Event updated.',
      });
    } on Object catch (error) {
      _showError('Could not update the event status', error);
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  Future<void> _delete(GuildScheduledEvent event) async {
    setState(() => _busy = event.ref);
    try {
      await widget.repository.deleteScheduledEvent(widget.guild.ref, event);
      if (!mounted) return;
      setState(() => _events =
          _events.where((candidate) => candidate.ref != event.ref).toList());
      _showNotice('Scheduled event deleted.');
    } on Object catch (error) {
      _showError('Could not delete the scheduled event', error);
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  Future<void> _toggleSubscription(GuildScheduledEvent event) async {
    final known = _subscriptions[event.ref] ?? event.meSubscribed;
    final next = !known;
    setState(() => _busy = event.ref);
    try {
      await widget.repository.setScheduledEventSubscription(
        widget.guild.ref,
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
            ? 'You will be notified about this event.'
            : 'Event notifications turned off.',
      );
    } on Object catch (error) {
      _showError('Could not update event notifications', error);
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  Future<void> _showSubscribers(GuildScheduledEvent event) async {
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
          widget.guild.ref,
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
        if (subscribers
            .any((item) => item.user.ref == widget.currentUser?.ref)) {
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
                    title: Text('Interested members'),
                    subtitle: Text('${event.userCount} total'),
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
                                      summary: 'Could not load subscribers',
                                    ),
                                    textAlign: TextAlign.center,
                                  ),
                                  SizedBox(height: 12),
                                  FilledButton(
                                    onPressed:
                                        loading ? null : () => loadMore(update),
                                    child: Text('Try again'),
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
                                    'No one has followed this event yet.',
                                    textAlign: TextAlign.center,
                                    style:
                                        TextStyle(color: context.kaede.muted),
                                  ),
                                ),
                              if (subscribers.length < event.userCount)
                                Padding(
                                  padding: EdgeInsets.all(16),
                                  child: OutlinedButton(
                                    onPressed:
                                        loading ? null : () => loadMore(update),
                                    child: Text(
                                      loading ? 'Loading…' : 'Load more',
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
            TextButton(
              onPressed: () => Navigator.pop(dialogContext, false),
              child: Text('Keep event'),
            ),
            FilledButton(
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
                  summary: 'Check the event details',
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
                    event == null ? 'Create event' : 'Edit event',
                    style: Theme.of(context).textTheme.headlineSmall,
                  ),
                  SizedBox(height: 4),
                  Text(
                    'Choose a Stage, voice channel, or external location.',
                    style: TextStyle(color: context.kaede.muted),
                  ),
                  SizedBox(height: 16),
                  TextField(
                    controller: name,
                    maxLength: 100,
                    decoration: InputDecoration(labelText: 'Name'),
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
                      OutlinedButton.icon(
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
                              ? 'Add cover image'
                              : 'Change cover image',
                        ),
                      ),
                      if (coverFile != null ||
                          (existingCover != null && !removeCover))
                        TextButton(
                          onPressed: () => update(() {
                            coverFile = null;
                            removeCover = event?.imageHash != null;
                          }),
                          child: Text('Remove cover'),
                        ),
                    ],
                  ),
                  Text(
                    'PNG, JPEG, GIF, or WebP · up to 10 MiB',
                    style: TextStyle(color: context.kaede.muted, fontSize: 12),
                  ),
                  SizedBox(height: 10),
                  TextField(
                    controller: description,
                    maxLength: 1000,
                    minLines: 2,
                    maxLines: 4,
                    decoration: InputDecoration(
                      labelText: 'Description (optional)',
                    ),
                  ),
                  SizedBox(height: 10),
                  DropdownButtonFormField<ScheduledEventEntityType>(
                    initialValue: entityType,
                    decoration: InputDecoration(labelText: 'Event type'),
                    items: [
                      DropdownMenuItem(
                        value: ScheduledEventEntityType.stage,
                        child: Text('Stage channel'),
                      ),
                      DropdownMenuItem(
                        value: ScheduledEventEntityType.voice,
                        child: Text('Voice channel'),
                      ),
                      if (allowExternal)
                        DropdownMenuItem(
                          value: ScheduledEventEntityType.external,
                          child: Text('External'),
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
                            ? 'Stage channel'
                            : 'Voice channel',
                        helperText: matchingChannels.isEmpty
                            ? 'You need Create/Manage Events, View Channel, and Connect.'
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
                        labelText: 'Location or link',
                      ),
                    ),
                  SizedBox(height: 10),
                  ListTile(
                    contentPadding: EdgeInsets.zero,
                    title: Text('Starts'),
                    subtitle:
                        Text(DateFormat.yMMMd().add_jm().format(startTime)),
                    trailing: Icon(Icons.edit_calendar_outlined),
                    onTap: event?.status == ScheduledEventStatus.active
                        ? null
                        : pickStart,
                  ),
                  DropdownButtonFormField<ScheduledEventRecurrencePreset>(
                    initialValue: recurrence,
                    decoration: InputDecoration(labelText: 'Repeat'),
                    items: const [
                      DropdownMenuItem(
                        value: ScheduledEventRecurrencePreset.none,
                        child: Text('Does not repeat'),
                      ),
                      DropdownMenuItem(
                        value: ScheduledEventRecurrencePreset.daily,
                        child: Text('Daily'),
                      ),
                      DropdownMenuItem(
                        value: ScheduledEventRecurrencePreset.weekly,
                        child: Text('Weekly'),
                      ),
                      DropdownMenuItem(
                        value: ScheduledEventRecurrencePreset.biweekly,
                        child: Text('Every 2 weeks'),
                      ),
                      DropdownMenuItem(
                        value: ScheduledEventRecurrencePreset.monthly,
                        child: Text('Monthly'),
                      ),
                      DropdownMenuItem(
                        value: ScheduledEventRecurrencePreset.yearly,
                        child: Text('Yearly'),
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
                          ? 'Ends (optional)'
                          : 'Ends',
                    ),
                    subtitle: Text(
                      endTime == null
                          ? 'No end time'
                          : DateFormat.yMMMd().add_jm().format(endTime!),
                    ),
                    trailing: Wrap(
                      children: [
                        if (endTime != null &&
                            entityType != ScheduledEventEntityType.external)
                          IconButton(
                            tooltip: 'Clear end time',
                            onPressed: () => update(() => endTime = null),
                            icon: Icon(Icons.clear_rounded),
                          ),
                        IconButton(
                          tooltip: 'Choose end time',
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
                      TextButton(
                        onPressed: () => Navigator.pop(sheetContext),
                        child: Text('Cancel'),
                      ),
                      SizedBox(width: 8),
                      FilledButton(
                        onPressed: submit,
                        child: Text(event == null ? 'Create event' : 'Save'),
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
