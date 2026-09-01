import 'dart:async';

import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/announcement_repository.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/announcements.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

final class AnnouncementManagementTab extends StatefulWidget {
  const AnnouncementManagementTab({
    required this.guild,
    required this.guilds,
    required this.currentUser,
    required this.repository,
    this.sourceChannel,
    this.createOnly = false,
    this.liveController,
    super.key,
  });

  final KaedeGuild guild;
  final List<KaedeGuild> guilds;
  final KaedeUser? currentUser;
  final KaedeRepository repository;
  final KaedeChannel? sourceChannel;
  final bool createOnly;
  final MobileController? liveController;

  @override
  State<AnnouncementManagementTab> createState() =>
      _AnnouncementManagementTabState();
}

final class _AnnouncementManagementTabState
    extends State<AnnouncementManagementTab> {
  EntityRef? _source;
  EntityRef? _target;
  List<AnnouncementFollow> _follows = const <AnnouncementFollow>[];
  var _loading = false;
  String? _busyFollow;
  String? _error;
  String? _notice;
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

  List<KaedeChannel> get _sources {
    final guild = _guild;
    if (guild == null) return const <KaedeChannel>[];
    final candidates = widget.sourceChannel == null
        ? guild.channels
        : guild.channels
            .where((channel) => channel.ref == widget.sourceChannel!.ref)
            .toList(growable: false);
    return candidates
        .where((channel) => canReadAnnouncementChannel(
              guild,
              channel,
              _currentUser,
            ))
        .toList(growable: false)
      ..sort((left, right) => left.position.compareTo(right.position));
  }

  List<KaedeGuild> get _guilds {
    final live = _liveState?.guilds ?? widget.guilds;
    final guild = _guild;
    final available = <EntityRef, KaedeGuild>{
      for (final guild in live) guild.ref: guild,
      if (guild != null) guild.ref: guild,
    };
    return available.values.toList(growable: false);
  }

  List<AnnouncementTarget> get _availableTargets {
    final followed = _follows.map((follow) => follow.targetChannel).toSet();
    return announcementTargets(_guilds, _currentUser)
        .where((target) => !followed.contains(target.ref))
        .toList(growable: false);
  }

  Map<EntityRef, AnnouncementTarget> get _targetByRef =>
      <EntityRef, AnnouncementTarget>{
        for (final target in announcementTargets(_guilds, _currentUser))
          target.ref: target,
      };

  @override
  void initState() {
    super.initState();
    _listenLive();
    _source = widget.sourceChannel?.ref ?? _sources.firstOrNull?.ref;
    if (_source != null) unawaited(_load());
  }

  void _listenLive() {
    _removeLiveListener?.call();
    _removeLiveListener = widget.liveController?.addListener((_) {
      if (!mounted) return;
      setState(() {
        final sources = _sources;
        if (_source == null ||
            !sources.any((channel) => channel.ref == _source)) {
          _requestGeneration += 1;
          _source = sources.firstOrNull?.ref;
          _target = null;
          _follows = const <AnnouncementFollow>[];
          _loading = false;
        }
      });
    }, fireImmediately: false);
  }

  @override
  void didUpdateWidget(covariant AnnouncementManagementTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.liveController, widget.liveController)) {
      _listenLive();
    }
    final sources = _sources;
    if (_source == null || !sources.any((channel) => channel.ref == _source)) {
      _source = sources.firstOrNull?.ref;
      _target = null;
      _follows = const <AnnouncementFollow>[];
      if (_source != null) unawaited(_load());
    }
  }

  @override
  void dispose() {
    _removeLiveListener?.call();
    super.dispose();
  }

  bool _sourceAuthorized(EntityRef source) =>
      _sources.any((channel) => channel.ref == source);

  bool _targetAuthorized(EntityRef target) =>
      _availableTargets.any((candidate) => candidate.ref == target);

  String _followLabel(AnnouncementFollow follow) =>
      _targetByRef[follow.targetChannel]?.label ??
      _guilds
          .expand((guild) => guild.channels.map((channel) => (guild, channel)))
          .where((entry) => entry.$2.ref == follow.targetChannel)
          .map((entry) => '${entry.$1.name} · #${entry.$2.name ?? 'channel'}')
          .firstOrNull ??
      'Channel ${follow.targetChannel.wire}';

  Future<void> _load() async {
    final source = _source;
    if (source == null || !_sourceAuthorized(source)) return;
    final generation = ++_requestGeneration;
    setState(() {
      _loading = true;
      _error = null;
      _notice = null;
      _follows = const <AnnouncementFollow>[];
    });
    try {
      final follows = await widget.repository.announcementFollowers(source);
      if (!mounted ||
          generation != _requestGeneration ||
          _source != source ||
          !_sourceAuthorized(source)) {
        return;
      }
      setState(() => _follows = follows);
    } on Object catch (error) {
      if (!mounted || generation != _requestGeneration || _source != source) {
        return;
      }
      setState(() {
        _error = userFacingError(
          error,
          summary: 'Could not load announcement followers',
        );
      });
    } finally {
      if (mounted && generation == _requestGeneration && _source == source) {
        setState(() => _loading = false);
      }
    }
  }

  Future<void> _create() async {
    final source = _source;
    final target = _target;
    if (source == null ||
        target == null ||
        _busyFollow != null ||
        !_sourceAuthorized(source) ||
        !_targetAuthorized(target)) {
      return;
    }
    setState(() {
      _busyFollow = 'create';
      _error = null;
      _notice = null;
    });
    try {
      if (!_sourceAuthorized(source) || !_targetAuthorized(target)) return;
      final follow = await widget.repository.followAnnouncement(source, target);
      if (!mounted || !_sourceAuthorized(source)) return;
      setState(() {
        _follows = <AnnouncementFollow>[
          ..._follows.where((item) => item.ref != follow.ref),
          follow,
        ];
        _target = null;
        _notice = 'New announcements can now be published to that channel.';
      });
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        _error = userFacingError(
          error,
          summary:
              'Could not add that follower. Manage Webhooks is required in the destination',
        );
      });
    } finally {
      if (mounted) setState(() => _busyFollow = null);
    }
  }

  Future<void> _remove(AnnouncementFollow follow) async {
    if (_busyFollow != null ||
        !canDeleteAnnouncementFollow(
          follow,
          _guilds,
          _currentUser,
        )) {
      return;
    }
    final label = _followLabel(follow);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('Remove follower?'),
        content: Text('Stop publishing new announcements to $label?'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: Text('Remove'),
          ),
        ],
      ),
    );
    if (confirmed != true ||
        !mounted ||
        !_sourceAuthorized(follow.sourceChannel) ||
        !canDeleteAnnouncementFollow(follow, _guilds, _currentUser)) {
      return;
    }
    setState(() {
      _busyFollow = follow.ref.wire;
      _error = null;
      _notice = null;
    });
    try {
      if (!_sourceAuthorized(follow.sourceChannel) ||
          !canDeleteAnnouncementFollow(follow, _guilds, _currentUser)) {
        return;
      }
      await widget.repository.deleteAnnouncementFollow(
        follow.sourceChannel,
        follow.ref,
      );
      if (!mounted) return;
      setState(() {
        _follows = _follows
            .where((item) => item.ref != follow.ref)
            .toList(growable: false);
        _notice = 'Stopped publishing announcements to $label.';
      });
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        _error = userFacingError(
          error,
          summary:
              'Could not remove that follower. Manage Webhooks is required in the destination',
        );
      });
    } finally {
      if (mounted) setState(() => _busyFollow = null);
    }
  }

  @override
  Widget build(BuildContext context) {
    final sources = _sources;
    final targets = _availableTargets;
    return Scaffold(
      backgroundColor: context.kaede.canvas,
      body: ListView(
        padding: EdgeInsets.all(16),
        children: [
          Text(
            widget.createOnly
                ? 'Follow #${widget.sourceChannel?.name ?? 'announcements'}'
                : 'Channels Followed',
            style: TextStyle(fontSize: 20, fontWeight: FontWeight.w800),
          ),
          SizedBox(height: 6),
          Text(
            widget.createOnly
                ? 'Choose a text channel where you can manage webhooks. Published posts will appear there.'
                : 'Choose which text channels receive posts when someone deliberately publishes a message from an announcement channel.',
            style: TextStyle(color: context.kaede.muted, height: 1.4),
          ),
          SizedBox(height: 18),
          if (sources.isEmpty)
            Card(
              child: ListTile(
                leading: Icon(Icons.lock_outline_rounded),
                title: Text('Followers are unavailable'),
                subtitle: Text(
                  'View Channel and Read Message History are required on an '
                  'announcement channel.',
                ),
              ),
            )
          else ...[
            if (widget.sourceChannel == null)
              DropdownButtonFormField<EntityRef>(
                key: ValueKey('announcement-source-picker-${_source?.wire}'),
                initialValue: _source,
                decoration: InputDecoration(
                  labelText: 'Announcement channel',
                  prefixIcon: Icon(Icons.campaign_outlined),
                ),
                items: [
                  for (final source in sources)
                    DropdownMenuItem(
                      value: source.ref,
                      child: Text('#${source.name ?? 'announcement'}'),
                    ),
                ],
                onChanged: _busyFollow != null
                    ? null
                    : (source) {
                        if (source == null || source == _source) return;
                        setState(() {
                          _source = source;
                          _target = null;
                        });
                        unawaited(_load());
                      },
              ),
            if (widget.sourceChannel == null) SizedBox(height: 16),
            Row(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                Expanded(
                  child: DropdownButtonFormField<EntityRef>(
                    key: ValueKey(
                      'announcement-target-picker-${_source?.wire}-${_target?.wire}',
                    ),
                    initialValue: _target,
                    decoration: InputDecoration(
                      labelText: 'Publish into',
                      prefixIcon: Icon(Icons.call_split_rounded),
                    ),
                    items: [
                      for (final target in targets)
                        DropdownMenuItem(
                          value: target.ref,
                          child: Text(
                            target.label,
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                    ],
                    onChanged: _busyFollow != null || targets.isEmpty
                        ? null
                        : (target) => setState(() => _target = target),
                  ),
                ),
                SizedBox(width: 10),
                FilledButton.icon(
                  key: ValueKey('announcement-follow-button'),
                  onPressed:
                      _busyFollow == null && _target != null ? _create : null,
                  icon: _busyFollow == 'create'
                      ? SizedBox.square(
                          dimension: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : Icon(Icons.add_rounded),
                  label: Text('Follow'),
                ),
              ],
            ),
            SizedBox(height: 8),
            Text(
              targets.isEmpty
                  ? 'No eligible destination is available. Manage Webhooks is '
                      'required in a plaintext text channel.'
                  : 'Destinations can be in another Kaede guild or instance.',
              style: TextStyle(color: context.kaede.muted, fontSize: 12.5),
            ),
            if (_error != null) ...[
              SizedBox(height: 12),
              Text(
                _error!,
                key: ValueKey('announcement-error'),
                style: TextStyle(color: context.kaede.danger),
              ),
            ],
            if (_notice != null) ...[
              SizedBox(height: 12),
              Text(
                _notice!,
                key: ValueKey('announcement-notice'),
                style: TextStyle(color: context.kaede.mint),
              ),
            ],
            if (!widget.createOnly) ...[
              SizedBox(height: 18),
              Text(
                'Follower channels',
                style: TextStyle(fontSize: 16, fontWeight: FontWeight.w800),
              ),
              SizedBox(height: 8),
              if (_loading)
                Center(
                  child: Padding(
                    padding: EdgeInsets.all(24),
                    child: CircularProgressIndicator(),
                  ),
                )
              else if (_follows.isEmpty)
                Card(
                  child: ListTile(
                    leading: Icon(Icons.notifications_none_rounded),
                    title: Text('No follower channels yet'),
                    subtitle: Text(
                      'Messages stay only in this channel until a destination '
                      'is added and a message is published.',
                    ),
                  ),
                )
              else
                for (final follow in _follows)
                  Card(
                    key: ValueKey('announcement-follow-${follow.ref.wire}'),
                    child: ListTile(
                      leading: CircleAvatar(
                        child: Icon(Icons.tag_rounded),
                      ),
                      title: Text(_followLabel(follow)),
                      subtitle: Text(
                        '${follow.targetChannel.wire}'
                        '${follow.federated ? ' · Federated' : ''}',
                      ),
                      trailing: Tooltip(
                        message: canDeleteAnnouncementFollow(
                          follow,
                          _guilds,
                          _currentUser,
                        )
                            ? 'Remove follower'
                            : 'Manage Webhooks is required in the destination',
                        child: IconButton(
                          onPressed: _busyFollow == null &&
                                  canDeleteAnnouncementFollow(
                                    follow,
                                    _guilds,
                                    _currentUser,
                                  )
                              ? () => _remove(follow)
                              : null,
                          icon: _busyFollow == follow.ref.wire
                              ? SizedBox.square(
                                  dimension: 18,
                                  child: CircularProgressIndicator(
                                    strokeWidth: 2,
                                  ),
                                )
                              : Icon(Icons.delete_outline_rounded),
                        ),
                      ),
                    ),
                  ),
            ],
          ],
        ],
      ),
    );
  }
}
