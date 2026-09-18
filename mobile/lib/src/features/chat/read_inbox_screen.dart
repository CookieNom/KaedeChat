import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/shared/action_feedback.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';

class ReadInboxScreen extends ConsumerStatefulWidget {
  const ReadInboxScreen({super.key});
  @override
  ConsumerState<ReadInboxScreen> createState() => _ReadInboxScreenState();
}

class _ReadInboxScreenState extends ConsumerState<ReadInboxScreen> {
  bool _mentions = false,
      _everyone = true,
      _roles = true,
      _busy = false,
      _more = false;
  bool _showMuted = false;
  Timer? _refreshTimer;
  EntityRef? _guild;
  String? _error;
  List<Map<String, Object?>> _entries = [];
  @override
  void initState() {
    super.initState();
    Future.microtask(_load);
    _refreshTimer = Timer.periodic(const Duration(seconds: 30), (_) {
      if (!_busy && mounted && ModalRoute.of(context)?.isCurrent == true) {
        _load();
      }
    });
  }

  @override
  void dispose() {
    _refreshTimer?.cancel();
    super.dispose();
  }

  EntityRef _channel(Map<String, Object?> row) => EntityRef(
      Snowflake('${row['channel_id']}'), Domain('${row['channel_domain']}'));
  EntityRef _message(Map<String, Object?> row) => EntityRef(
      Snowflake('${row['message_id']}'), Domain('${row['message_domain']}'));
  Future<void> _load({bool append = false}) async {
    if (!mounted) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final controller = ref.read(mobileControllerProvider.notifier);
      final entries = _mentions
          ? await controller.api
              .getList('/api/v1/users/@me/inbox/mentions', query: {
              'include_everyone': _everyone,
              'include_roles': _roles,
              if (_guild != null) 'guild': _guild!.wire,
              if (append && _entries.isNotEmpty)
                'before': _message(_entries.last).wire,
            })
          : await controller.repository.readStates();
      if (!mounted) return;
      setState(() {
        _entries = append ? [..._entries, ...entries] : entries;
        _more = _mentions && entries.length == 50;
      });
    } on Object catch (error) {
      if (mounted) {
        setState(() =>
            _error = userFacingError(error, summary: 'Could not load inbox'));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _action(Future<void> Function() action) async {
    setState(() => _busy = true);
    try {
      await action();
      await _load();
    } on Object catch (error) {
      if (mounted) {
        setState(() =>
            _error = userFacingError(error, summary: 'Could not update inbox'));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _jump(Map<String, Object?> row) async {
    await _action(() async {
      final controller = ref.read(mobileControllerProvider.notifier);
      var state = ref.read(mobileControllerProvider);
      KaedeChannel? find() => [
            ...state.dms,
            ...state.guilds.expand((g) => g.channels)
          ].where((c) => c.ref == _channel(row)).firstOrNull;
      var channel = find();
      if (channel == null) {
        await controller.refreshNavigation();
        state = ref.read(mobileControllerProvider);
        channel = find();
      }
      channel ??= await controller.fetchThread(_channel(row));
      if (!mounted) return;
      if (row['message_id'] != null) {
        await controller.selectAndJumpToMessage(channel, _message(row));
      } else if (channel.guildRef == null) {
        await controller.selectDm(channel);
      } else {
        await controller.selectChannel(channel);
      }
      if (mounted) Navigator.pop(context, true);
    });
  }

  @override
  Widget build(BuildContext context) {
    final l = L10n.of(context);
    final state = ref.watch(mobileControllerProvider);
    final controller = ref.read(mobileControllerProvider.notifier);
    final names = {
      for (final channel in state.dms)
        channel.ref:
            channel.name ?? channel.recipients.map((u) => u.name).join(', ')
    };
    final visible = _entries
        .where((r) =>
            (_mentions ||
                (r['unread'] == true && (_showMuted || r['muted'] != true))) &&
            (_guild == null ||
                '${r['guild_id']}@${r['guild_domain']}' == _guild!.wire))
        .toList();
    return Scaffold(
      appBar: AppBar(title: Text(l.chat_inbox), actions: [
        ActionButton(
            kind: ActionButtonKind.icon,
            tooltip: l.chat_refresh,
            onPressed: _busy ? null : _load,
            icon: const Icon(Icons.refresh))
      ]),
      body: Column(children: [
        Padding(
            padding: const EdgeInsets.all(12),
            child: Wrap(
                spacing: 12,
                runSpacing: 8,
                crossAxisAlignment: WrapCrossAlignment.center,
                children: [
                  SegmentedButton<bool>(
                      segments: [
                        ButtonSegment(
                            value: false, label: Text(l.chat_unreads)),
                        ButtonSegment(value: true, label: Text(l.chat_mentions))
                      ],
                      selected: {
                        _mentions
                      },
                      onSelectionChanged: _busy
                          ? null
                          : (v) {
                              setState(() => _mentions = v.first);
                              _load();
                            }),
                  DropdownButton<EntityRef?>(
                      value: _guild,
                      hint: Text(l.chat_all_conversations),
                      items: [
                        DropdownMenuItem(
                            value: null, child: Text(l.chat_all_conversations)),
                        ...state.guilds.map((g) =>
                            DropdownMenuItem(value: g.ref, child: Text(g.name)))
                      ],
                      onChanged: _busy
                          ? null
                          : (v) {
                              setState(() => _guild = v);
                              _load();
                            }),
                  if (!_mentions)
                    FilterChip(
                        label: Text(l.chat_show_muted),
                        selected: _showMuted,
                        onSelected: (v) => setState(() => _showMuted = v)),
                  if (_mentions) ...[
                    FilterChip(
                        label: const Text('@everyone'),
                        selected: _everyone,
                        onSelected: _busy
                            ? null
                            : (v) {
                                setState(() => _everyone = v);
                                _load();
                              }),
                    FilterChip(
                        label: const Text('@roles'),
                        selected: _roles,
                        onSelected: _busy
                            ? null
                            : (v) {
                                setState(() => _roles = v);
                                _load();
                              }),
                  ],
                  ActionButton(
                      kind: ActionButtonKind.text,
                      onPressed: _busy
                          ? null
                          : () => _action(
                              () => controller.markChannelsRead(guild: _guild)),
                      icon: const Icon(Icons.done_all),
                      label: Text(l.chat_mark_all_read)),
                ])),
        if (_busy) const LinearProgressIndicator(),
        if (_error != null)
          Padding(
              padding: const EdgeInsets.all(12),
              child: Text(_error!, semanticsLabel: _error)),
        Expanded(
            child: RefreshIndicator(
                onRefresh: _load,
                child: ListView(children: [
                  if (visible.isEmpty && !_busy)
                    ListTile(title: Text(l.chat_caught_up)),
                  for (final row in visible)
                    ListTile(
                      title: Text(
                          '${row['channel_name'] ?? names[_channel(row)] ?? l.chat_direct_message}'),
                      subtitle: Text(_mentions
                          ? l.chat_jump_to_mention
                          : '${row['unread_count'] ?? ''} ${l.chat_unreads}'),
                      onTap: _busy ? null : () => _jump(row),
                      trailing: ActionButton(
                          kind: ActionButtonKind.icon,
                          tooltip:
                              _mentions ? l.chat_dismiss : l.chat_mark_read,
                          icon: Icon(_mentions ? Icons.close : Icons.done_all),
                          onPressed: _busy
                              ? null
                              : () => _action(() async {
                                    if (_mentions) {
                                      await controller.api.sendJson('POST',
                                          '/api/v1/users/@me/inbox/dismiss',
                                          data: {
                                            'message_id': _message(row).wire
                                          });
                                    } else {
                                      await controller.markChannelsRead(
                                          channel: _channel(row));
                                    }
                                  })),
                    ),
                  if (_more)
                    ActionButton(
                        kind: ActionButtonKind.text,
                        onPressed: _busy ? null : () => _load(append: true),
                        child: Text(l.chat_load_more)),
                ]))),
      ]),
    );
  }
}
