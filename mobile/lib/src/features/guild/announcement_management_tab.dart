import 'dart:async';
import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/announcement_repository.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/announcements.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';
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
          summary: L10n.of(context)
              .ui_could_not_load_announcement_followers_289f21ff,
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
        _notice = L10n.of(context)
            .ui_new_announcements_can_now_be_published_to_tha_ae551f77;
      });
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        _error = userFacingError(
          error,
          summary: L10n.of(context)
              .ui_could_not_add_that_follower_manage_webhooks_i_bf8af7b1,
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
        title: Text(L10n.of(context).ui_remove_follower_944364a6),
        content: Text(L10n.of(context)
            .ui_stop_publishing_new_announcements_to_value0_5baa0d7b(
                (label).toString())),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: Text(L10n.of(context).ui_cancel_35afca3b),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: Text(L10n.of(context).ui_remove_21a5901d),
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
        _notice = L10n.of(context)
            .ui_stopped_publishing_announcements_to_value0_02d57979(
                (label).toString());
      });
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        _error = userFacingError(
          error,
          summary: L10n.of(context)
              .ui_could_not_remove_that_follower_manage_webhook_0a0ee780,
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
                ? L10n.of(context).ui_follow_value0_0f6d45e8(
                    (widget.sourceChannel?.name ?? 'announcements').toString())
                : L10n.of(context).ui_channels_followed_3396f983,
            style: TextStyle(fontSize: 20, fontWeight: FontWeight.w800),
          ),
          SizedBox(height: 6),
          Text(
            widget.createOnly
                ? L10n.of(context)
                    .ui_choose_a_text_channel_where_you_can_manage_we_489a086a
                : L10n.of(context)
                    .ui_choose_which_text_channels_receive_posts_when_f83ffe28,
            style: TextStyle(color: context.kaede.muted, height: 1.4),
          ),
          SizedBox(height: 18),
          if (sources.isEmpty)
            Card(
              child: ListTile(
                leading: Icon(Icons.lock_outline_rounded),
                title: Text(
                    L10n.of(context).ui_followers_are_unavailable_3fb2d79c),
                subtitle: Text(
                  L10n.of(context)
                      .ui_view_channel_and_read_message_history_are_req_cdb857e6,
                ),
              ),
            )
          else ...[
            if (widget.sourceChannel == null)
              DropdownButtonFormField<EntityRef>(
                key: ValueKey('announcement-source-picker-${_source?.wire}'),
                initialValue: _source,
                decoration: InputDecoration(
                  labelText: L10n.of(context).ui_announcement_channel_ec8b9fb9,
                  prefixIcon: Icon(Icons.campaign_outlined),
                ),
                items: [
                  for (final source in sources)
                    DropdownMenuItem(
                      value: source.ref,
                      child: Text(L10n.of(context).ui_value0_ea2f080f(
                          (source.name ?? 'announcement').toString())),
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
                      labelText: L10n.of(context).ui_publish_into_b1c4c262,
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
                  label: Text(L10n.of(context).ui_follow_8aad7d90),
                ),
              ],
            ),
            SizedBox(height: 8),
            Text(
              targets.isEmpty
                  ? L10n.of(context)
                      .ui_no_eligible_destination_is_available_manage_w_98334063
                  : L10n.of(context)
                      .ui_destinations_can_be_in_another_kaede_guild_or_44ef8be4,
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
                L10n.of(context).ui_follower_channels_cb39dcf5,
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
                    title: Text(
                        L10n.of(context).ui_no_follower_channels_yet_9846839a),
                    subtitle: Text(
                      L10n.of(context)
                          .ui_messages_stay_only_in_this_channel_until_a_de_4b30c6b0,
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
                        L10n.of(context).ui_value0_value1_07eda8d4(
                            (follow.targetChannel.wire).toString(),
                            (follow.federated ? ' · Federated' : '')
                                .toString()),
                      ),
                      trailing: Tooltip(
                        message: canDeleteAnnouncementFollow(
                          follow,
                          _guilds,
                          _currentUser,
                        )
                            ? L10n.of(context).ui_remove_follower_5eb208bd
                            : L10n.of(context)
                                .ui_manage_webhooks_is_required_in_the_destinatio_bbcabccb,
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
