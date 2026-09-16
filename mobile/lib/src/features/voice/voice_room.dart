import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart' hide ConnectionState;
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/api/guild_admin_repository.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/stage_instances_repository.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/app/providers.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/guild_admin.dart';
import 'package:kaede_mobile/src/domain/guild_hierarchy.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/stage_instances.dart';
import 'package:kaede_mobile/src/domain/stage_permissions.dart';
import 'package:kaede_mobile/src/features/chat/composer_pickers.dart';
import 'package:kaede_mobile/src/features/voice/media_quality.dart';
import 'package:kaede_mobile/src/features/voice/soundboard_access.dart';
import 'package:kaede_mobile/src/features/voice/voice_elapsed.dart';
import 'package:kaede_mobile/src/features/voice/voice_session.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:livekit_client/livekit_client.dart';

// One presentation per source, regardless of the number of codec variants.
List<({Participant participant, TrackSource source})> _voiceVideoTiles(
  List<Participant> participants,
) =>
    [
      for (final participant in participants) ...[
        (participant: participant, source: TrackSource.camera),
        if (participant.videoTrackPublications.any((p) =>
            p.source == TrackSource.screenShareVideo &&
            !p.muted &&
            p.track != null))
          (participant: participant, source: TrackSource.screenShareVideo),
      ],
    ];

String voiceParticipantLabel({
  required String liveName,
  required String identity,
  String? knownName,
}) {
  final resolvedLiveName = liveName.trim();
  if (resolvedLiveName.isNotEmpty) return resolvedLiveName;
  final resolvedKnownName = knownName?.trim() ?? '';
  if (resolvedKnownName.isNotEmpty) return resolvedKnownName;
  return identity.split('@').first;
}

bool voiceChannelAllows(KaedeChannel channel, int permission) =>
    channel.allows(Permission.administrator) || channel.allows(permission);

bool canSetVoiceChannelStatusNow(
  KaedeChannel channel, {
  required bool joined,
}) =>
    channel.type == ChannelType.voice &&
    voiceChannelAllows(channel, Permission.setVoiceChannelStatus) &&
    (joined || voiceChannelAllows(channel, Permission.manageChannels));

bool canStartStageNow(KaedeChannel channel, {required bool notify}) =>
    canManageStageChannel(channel) &&
    (!notify || voiceChannelAllows(channel, Permission.mentionEveryone));

List<KaedeChannel> voiceMoveDestinationChannels(
  KaedeGuild guild,
  KaedeChannel source,
) =>
    guild.channels
        .where((channel) =>
            channel.type.isVoiceLike &&
            channel.ref != source.ref &&
            voiceChannelAllows(channel, Permission.moveMembers))
        .toList(growable: false);

String? _knownVoiceParticipantName(MobileState state, String identity) {
  EntityRef reference;
  try {
    reference = EntityRef.parse(identity);
  } on Object {
    return null;
  }
  if (state.user?.ref == reference) return state.user!.name;
  for (final dm in state.dms) {
    for (final user in dm.recipients) {
      if (user.ref == reference) return user.name;
    }
  }
  for (final messages in state.messageStore.values) {
    for (final message in messages) {
      if (message.author case final author? when author.ref == reference) {
        return author.name;
      }
    }
  }
  return null;
}

typedef _SoundboardGroup = ({
  String key,
  String label,
  List<SoundboardSound> sounds
});

Future<List<_SoundboardGroup>> _loadSoundboardGroups(
  KaedeRepository repository,
  List<KaedeGuild> availableGuilds,
  EntityRef? currentGuildRef, {
  required bool canUseExternalSounds,
}) async {
  final guilds = availableGuilds
      .where(
        (guild) => soundboardSourceAllowed(
          targetGuildRef: currentGuildRef,
          sourceGuildRef: guild.ref,
          canUseExternalSounds: canUseExternalSounds,
        ),
      )
      .toList()
    ..sort((left, right) {
      final leftCurrent = left.ref == currentGuildRef;
      final rightCurrent = right.ref == currentGuildRef;
      if (leftCurrent != rightCurrent) return leftCurrent ? -1 : 1;
      return left.name.compareTo(right.name);
    });
  final defaultFuture = () async {
    try {
      return await repository.defaultSoundboardSounds();
    } on Object {
      return const <SoundboardSound>[];
    }
  }();
  final guildFutures = guilds.map((guild) async {
    try {
      return await repository.soundboardSounds(guild.ref);
    } on Object {
      return const <SoundboardSound>[];
    }
  }).toList(growable: false);
  final results = await Future.wait([defaultFuture, ...guildFutures]);
  final groups = <_SoundboardGroup>[];
  if (results.first.isNotEmpty) {
    groups.add((
      key: 'default',
      label: L10n.current.ui_discord_sounds_e639de1d,
      sounds: results.first
    ));
  }
  for (var index = 0; index < guilds.length; index += 1) {
    final sounds = results[index + 1];
    if (sounds.isEmpty) continue;
    final guild = guilds[index];
    groups.add((
      key: guild.ref.wire,
      label: guild.ref == currentGuildRef
          ? L10n.current
              .ui_value0_current_server_cbb9b711((guild.name).toString())
          : guild.name,
      sounds: sounds,
    ));
  }
  return groups;
}

/// A real LiveKit room rather than a UI-only connection placeholder.
///
/// LiveKit owns capture and playback on mobile so the operating system's AEC,
/// noise suppression, audio routing and call lifecycle remain coherent.
final class VoiceRoom extends ConsumerStatefulWidget {
  const VoiceRoom(
      {required this.channel, this.callRef, this.onApps, super.key});

  final KaedeChannel channel;
  final EntityRef? callRef;
  final Future<void> Function()? onApps;

  @override
  ConsumerState<VoiceRoom> createState() => _VoiceRoomState();
}

final class _VoiceRoomState extends ConsumerState<VoiceRoom> {
  StageInstance? _stageInstance;
  var _stageLoading = false;
  var _stageLoaded = false;
  String? _voiceStatus;
  int? _voiceStartedAt;
  var _voiceStatusBusy = false;
  Timer? _voiceElapsedTimer;
  StreamSubscription<Map<String, Object?>>? _stageSubscription;
  StreamSubscription<Map<String, Object?>>? _voiceStatusSubscription;
  StreamSubscription<Map<String, Object?>>? _voiceChannelInfoSubscription;

  KaedeChannel get channel => widget.channel;
  EntityRef? get callRef => widget.callRef;

  bool get _canManageStage => canManageStageChannel(channel);

  @override
  void initState() {
    super.initState();
    if (channel.type == ChannelType.voice) {
      Future<void>.microtask(_loadVoiceStatus);
    }
    _stageSubscription = ref
        .read(mobileControllerProvider.notifier)
        .stageEvents
        .listen(_onStageEvent);
    _voiceStatusSubscription = ref
        .read(mobileControllerProvider.notifier)
        .voiceStatusEvents
        .listen((event) {
      if ('${event['id']}@${event['origin_domain'] ?? event['guild_domain']}' ==
              channel.ref.wire &&
          mounted) {
        setState(() => _voiceStatus = event['status'] as String?);
      }
    });
    _voiceChannelInfoSubscription = ref
        .read(mobileControllerProvider.notifier)
        .voiceChannelInfoEvents
        .listen(_onVoiceChannelInfo);
    final guildRef = channel.guildRef;
    if (guildRef != null) {
      final controller = ref.read(mobileControllerProvider.notifier);
      controller
        ..requestVoiceChannelInfo(guildRef)
        ..requestGuildMembers(guildRef);
    }
    if (channel.type == ChannelType.stage) {
      Future<void>.microtask(_loadStage);
    }
  }

  @override
  void dispose() {
    _voiceElapsedTimer?.cancel();
    unawaited(_stageSubscription?.cancel());
    unawaited(_voiceStatusSubscription?.cancel());
    unawaited(_voiceChannelInfoSubscription?.cancel());
    super.dispose();
  }

  @override
  void didUpdateWidget(covariant VoiceRoom oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.channel.ref != channel.ref) {
      _voiceStatus = null;
      _voiceStartedAt = null;
      _voiceElapsedTimer?.cancel();
      _voiceElapsedTimer = null;
      if (channel.type == ChannelType.voice) {
        Future<void>.microtask(_loadVoiceStatus);
      }
      _stageInstance = null;
      _stageLoaded = false;
      if (channel.type == ChannelType.stage) Future<void>.microtask(_loadStage);
      final guildRef = channel.guildRef;
      if (guildRef != null) {
        final controller = ref.read(mobileControllerProvider.notifier);
        controller
          ..requestVoiceChannelInfo(guildRef)
          ..requestGuildMembers(guildRef);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final session = ref.watch(voiceSessionProvider);
    final mobile = ref.watch(mobileControllerProvider);
    final guild = mobile.activeGuild;
    final canConnect = callRef != null || channel.allows(Permission.connect);
    final thisRoom =
        session.channel?.ref == channel.ref && session.callRef == callRef;
    final connected = thisRoom && session.connected;
    final joined = thisRoom && session.joined;
    final canSetVoiceStatus =
        canSetVoiceChannelStatusNow(channel, joined: joined);
    final reconnecting = thisRoom && session.reconnecting;
    final participants = joined ? session.participants : const <Participant>[];
    final videoTiles = _voiceVideoTiles(participants);
    final voiceElapsed = voiceElapsedLabel(_voiceStartedAt);
    final roomSummary = channel.type == ChannelType.stage && !_stageLoaded
        ? 'Loading Stage…'
        : channel.type == ChannelType.stage && _stageInstance == null
            ? 'The Stage has not started'
            : connected
                ? '${participants.length} connected'
                : reconnecting
                    ? 'Reconnecting… your place in the call is being kept'
                    : 'Join to talk, listen, and share video';
    final roomSummaryWithElapsed = voiceElapsed == null ||
            (channel.type == ChannelType.stage && _stageInstance == null)
        ? roomSummary
        : connected
            ? '$roomSummary · $voiceElapsed'
            : 'Active for $voiceElapsed · $roomSummary';
    if (thisRoom && callRef == null) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        session.reconcilePermissions(channel);
      });
    }

    return SafeArea(
      child: Column(
        children: [
          Padding(
            padding: EdgeInsets.fromLTRB(16, 14, 16, 8),
            child: Row(
              children: [
                CircleAvatar(
                  backgroundColor: context.kaede.coralDark,
                  child: Icon(Icons.graphic_eq_rounded),
                ),
                SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                          channel.type == ChannelType.stage
                              ? (_stageInstance?.topic ??
                                  channel.name ??
                                  'Stage channel')
                              : channel.name ??
                                  (callRef == null
                                      ? L10n.of(context)
                                          .ui_voice_channel_d243a4f2
                                      : L10n.of(context).ui_call_341dd0c9),
                          style: Theme.of(context).textTheme.titleLarge),
                      if (channel.type == ChannelType.voice &&
                          _voiceStatus?.isNotEmpty == true)
                        Text(
                          _voiceStatus!,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(color: context.kaede.muted),
                        ),
                      Text(
                        roomSummaryWithElapsed,
                        style: TextStyle(color: context.kaede.muted),
                      ),
                    ],
                  ),
                ),
                if (canSetVoiceStatus)
                  IconButton(
                    tooltip:
                        L10n.of(context).ui_set_voice_channel_status_ee4e27cc,
                    onPressed: _voiceStatusBusy ? null : _editVoiceStatus,
                    icon: Icon(Icons.edit_note_rounded),
                  ),
                if (!joined &&
                    (channel.type != ChannelType.stage ||
                        _stageInstance != null))
                  FilledButton.icon(
                    onPressed: canConnect && !session.connecting
                        ? () => session.connect(channel, callRef: callRef)
                        : null,
                    icon: session.connecting && thisRoom
                        ? SizedBox.square(
                            dimension: 18,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : Icon(Icons.call_rounded),
                    label: Text(session.connecting && thisRoom
                        ? L10n.of(context).ui_connecting_4d2cf951
                        : channel.type == ChannelType.stage
                            ? L10n.of(context).ui_join_audience_49b9c9c9
                            : L10n.of(context).ui_join_voice_f86abe8f),
                  ),
              ],
            ),
          ),
          if (channel.type == ChannelType.stage)
            _stageControls(context, session, guild),
          if (!canConnect)
            const _VoiceNotice(
              icon: Icons.lock_outline_rounded,
              text: 'You do not have permission to join this voice channel.',
            ),
          if (thisRoom)
            if (session.activeElsewhereClient case final activeClient?)
              _VoiceTakeoverNotice(
                activeClient: activeClient,
                moving: session.connecting,
                onMove: () => session.connect(
                  channel,
                  callRef: callRef,
                  force: true,
                  takeover: true,
                ),
                onCancel: () => session.leave(),
              )
            else if (session.error case final error?)
              _VoiceNotice(icon: Icons.error_outline_rounded, text: error),
          if (thisRoom && session.videoNotice != null)
            _VoiceNotice(
                icon: Icons.videocam_outlined, text: session.videoNotice!),
          Expanded(
            child: !joined
                ? _VoiceEmpty(canConnect: canConnect)
                : reconnecting
                    ? Center(
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            CircularProgressIndicator(),
                            SizedBox(height: 14),
                            Text(L10n.of(context)
                                .ui_restoring_voice_connection_7ca9f98c),
                          ],
                        ),
                      )
                    : channel.type == ChannelType.stage
                        ? _StageParticipantRoster(
                            participants: participants,
                            session: session,
                            mobile: mobile,
                            guild: guild,
                            channel: channel,
                          )
                        : GridView.builder(
                            padding: EdgeInsets.all(12),
                            gridDelegate:
                                SliverGridDelegateWithMaxCrossAxisExtent(
                              maxCrossAxisExtent: 420,
                              childAspectRatio: 16 / 10,
                              crossAxisSpacing: 10,
                              mainAxisSpacing: 10,
                            ),
                            itemCount: videoTiles.length,
                            itemBuilder: (context, index) => _ParticipantTile(
                              participant: videoTiles[index].participant,
                              source: videoTiles[index].source,
                              knownName: _knownVoiceParticipantName(
                                mobile,
                                videoTiles[index].participant.identity,
                              ),
                              session: session,
                              guild: guild,
                              actorRef: mobile.user?.ref,
                              targetMember: guild == null
                                  ? null
                                  : guildMemberByIdentity(
                                      mobile.guildMembers[guild.ref] ??
                                          const <GuildMember>[],
                                      videoTiles[index].participant.identity,
                                    ),
                              channel: channel,
                            ),
                          ),
          ),
          if (joined) _controls(context, ref, mobile, session),
        ],
      ),
    );
  }

  Future<void> _editVoiceStatus() async {
    final controller = TextEditingController(text: _voiceStatus ?? '');
    final value = await showDialog<String?>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(L10n.of(context).ui_voice_channel_status_7c33c678),
        content: StatefulBuilder(
          builder: (context, setDialogState) => Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Wrap(
                spacing: 8,
                children: [
                  for (final emoji in const ['😊', '🎮', '🎵', '🎉'])
                    ActionChip(
                      label: Text(emoji),
                      onPressed: () => setDialogState(() {
                        final next = '$emoji ${controller.text}';
                        controller.text =
                            next.length > 500 ? next.substring(0, 500) : next;
                        controller.selection = TextSelection.collapsed(
                          offset: controller.text.length,
                        );
                      }),
                    ),
                  ActionChip(
                    label: Text(L10n.of(context).ui_more_06a440fc),
                    avatar: Icon(Icons.emoji_emotions_outlined, size: 18),
                    onPressed: () async {
                      final emoji = await showComposerEmojiPicker(
                        dialogContext,
                        repository: ref.read(repositoryProvider),
                        channel: channel,
                        categories: defaultComposerEmojiCategories,
                      );
                      if (emoji == null) return;
                      setDialogState(() {
                        final inserted = insertComposerText(
                          controller.value,
                          '$emoji ',
                          maxLength: 500,
                        );
                        if (inserted != null) controller.value = inserted;
                      });
                    },
                  ),
                ],
              ),
              TextField(
                controller: controller,
                autofocus: true,
                maxLength: 500,
                decoration: InputDecoration(
                    hintText:
                        L10n.of(context).ui_what_is_happening_here_4bc6e776),
              ),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext),
            child: Text(L10n.of(context).ui_cancel_35afca3b),
          ),
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, ''),
            child: Text(L10n.of(context).ui_clear_04a57fc2),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(dialogContext, controller.text),
            child: Text(L10n.of(context).ui_save_4d2d5d68),
          ),
        ],
      ),
    );
    controller.dispose();
    if (value == null || !mounted) return;
    final session = ref.read(voiceSessionProvider);
    final joined = session.channel?.ref == channel.ref && session.joined;
    if (!canSetVoiceChannelStatusNow(channel, joined: joined)) {
      _showVoiceError(
        UserInputException('Voice channel permissions changed. Try again.'),
        'Could not update the voice channel status',
      );
      return;
    }
    setState(() => _voiceStatusBusy = true);
    try {
      final normalized = value.trim().isEmpty ? null : value.trim();
      await ref
          .read(repositoryProvider)
          .setVoiceChannelStatus(channel.ref, normalized);
      if (mounted) setState(() => _voiceStatus = normalized);
    } on Object catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(userFacingError(error))),
        );
      }
    } finally {
      if (mounted) setState(() => _voiceStatusBusy = false);
    }
  }

  void _onVoiceChannelInfo(Map<String, Object?> event) {
    final channels = event['channels'];
    if (channels is List) {
      for (final raw in channels) {
        if (raw is! Map) continue;
        _applyVoiceChannelInfo(
          Map<String, Object?>.from(raw),
          fallbackDomain: event['guild_domain'],
        );
      }
      return;
    }
    _applyVoiceChannelInfo(event, fallbackDomain: event['guild_domain']);
  }

  void _applyVoiceChannelInfo(
    Map<String, Object?> event, {
    Object? fallbackDomain,
  }) {
    if ('${event['id']}@${event['origin_domain'] ?? fallbackDomain}' !=
            channel.ref.wire ||
        !event.containsKey('voice_start_time')) {
      return;
    }
    final raw = event['voice_start_time'];
    final startedAt = voiceStartTimeSeconds(raw);
    if (raw != null && startedAt == null) return;
    _setVoiceStartedAt(startedAt);
  }

  void _setVoiceStartedAt(int? startedAt) {
    if (_voiceStartedAt == startedAt) return;
    _voiceElapsedTimer?.cancel();
    _voiceElapsedTimer = null;
    if (mounted) {
      setState(() => _voiceStartedAt = startedAt);
    } else {
      _voiceStartedAt = startedAt;
    }
    if (startedAt != null) {
      _voiceElapsedTimer = Timer.periodic(Duration(seconds: 1), (_) {
        if (mounted) setState(() {});
      });
    }
  }

  Future<void> _loadVoiceStatus() async {
    final expected = channel.ref;
    try {
      final status =
          await ref.read(repositoryProvider).voiceChannelStatus(expected);
      if (mounted && channel.ref == expected) {
        setState(() => _voiceStatus = status);
      }
    } on Object {
      // A later gateway update or successful edit remains authoritative.
    }
  }

  Widget _stageControls(
    BuildContext context,
    VoiceSession session,
    KaedeGuild? guild,
  ) {
    if (!_stageLoaded) return SizedBox.shrink();
    if (_stageInstance == null) {
      return Padding(
        padding: EdgeInsets.fromLTRB(16, 0, 16, 8),
        child: Row(
          children: [
            Expanded(
              child: Text(
                L10n.of(context).ui_this_stage_hasn_t_started_yet_4fb4ee3f,
                style: TextStyle(color: context.kaede.muted),
              ),
            ),
            if (_canManageStage)
              FilledButton.icon(
                onPressed: _stageLoading ? null : _startStage,
                icon: Icon(Icons.mic_rounded),
                label: Text(L10n.of(context).ui_start_stage_1d128645),
              ),
          ],
        ),
      );
    }
    final localParticipant =
        session.participants.whereType<LocalParticipant>().firstOrNull;
    final localOccupancy = localParticipant == null
        ? null
        : session.occupant(localParticipant.identity);
    final suppressed = localOccupancy?['suppressed'] == true;
    final requested = localOccupancy?['request_to_speak_timestamp'] != null;
    if (!_canManageStage && (!session.joined || guild == null)) {
      return SizedBox.shrink();
    }
    return Padding(
      padding: EdgeInsets.fromLTRB(16, 0, 16, 8),
      child: Wrap(
        alignment: WrapAlignment.end,
        spacing: 8,
        children: [
          if (session.joined && guild != null)
            if (suppressed)
              TextButton.icon(
                onPressed:
                    requested || channel.allows(Permission.requestToSpeak)
                        ? () => _runVoiceAction(
                              context,
                              () => session.requestToSpeak(
                                guild.ref,
                                requested: !requested,
                              ),
                            )
                        : null,
                icon: Icon(requested
                    ? Icons.pan_tool_alt_outlined
                    : Icons.front_hand_outlined),
                label: Text(requested
                    ? L10n.of(context).ui_cancel_request_28568fb6
                    : L10n.of(context).ui_request_to_speak_1e2c1da5),
              )
            else
              TextButton.icon(
                onPressed: () => _runVoiceAction(
                  context,
                  () => session.moveSelfToStageAudience(guild.ref),
                ),
                icon: Icon(Icons.hearing_outlined),
                label: Text(L10n.of(context).ui_move_to_audience_de051085),
              ),
          if (_canManageStage) ...[
            TextButton.icon(
              onPressed: _stageLoading ? null : _editStage,
              icon: Icon(Icons.edit_rounded),
              label: Text(L10n.of(context).ui_edit_topic_93089e9e),
            ),
            TextButton.icon(
              onPressed: _stageLoading ? null : _endStage,
              icon: Icon(Icons.stop_circle_outlined),
              label: Text(L10n.of(context).ui_end_stage_2bd54b88),
              style: TextButton.styleFrom(
                foregroundColor: Theme.of(context).colorScheme.error,
              ),
            ),
          ],
        ],
      ),
    );
  }

  Future<void> _loadStage() async {
    if (_stageLoading || channel.type != ChannelType.stage) return;
    setState(() => _stageLoading = true);
    try {
      final instance = await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .stageInstance(channel.ref);
      if (mounted) setState(() => _stageInstance = instance);
    } on KaedeException catch (error) {
      if (error.status != 404 && mounted) {
        _showVoiceError(error, 'Could not load this Stage');
      }
      if (error.status == 404 && mounted) {
        setState(() => _stageInstance = null);
      }
    } finally {
      if (mounted) {
        setState(() {
          _stageLoading = false;
          _stageLoaded = true;
        });
      }
    }
  }

  Future<String?> _stageTopicDialog({
    required String title,
    String initial = '',
  }) async {
    final controller = TextEditingController(text: initial);
    final topic = await showDialog<String>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(title),
        content: TextField(
          controller: controller,
          autofocus: true,
          maxLength: 120,
          decoration: InputDecoration(
            labelText: L10n.of(context).ui_topic_68501b64,
            hintText: L10n.of(context).ui_what_is_this_stage_about_d623a541,
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext),
            child: Text(L10n.of(context).ui_cancel_35afca3b),
          ),
          FilledButton(
            onPressed: () {
              final value = controller.text.trim();
              if (value.isNotEmpty) Navigator.pop(dialogContext, value);
            },
            child: Text(L10n.of(context).ui_save_4d2d5d68),
          ),
        ],
      ),
    );
    controller.dispose();
    return topic;
  }

  Future<({String topic, bool notify})?> _stageStartDialog() async {
    final controller = TextEditingController();
    var notify = false;
    final canNotify = channel.allows(Permission.mentionEveryone);
    final options = await showDialog<({String topic, bool notify})>(
      context: context,
      builder: (dialogContext) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          title: Text(L10n.of(context).ui_start_the_stage_3d1e5c4a),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                controller: controller,
                autofocus: true,
                maxLength: 120,
                decoration: InputDecoration(
                  labelText: L10n.of(context).ui_topic_68501b64,
                  hintText:
                      L10n.of(context).ui_what_is_this_stage_about_d623a541,
                ),
              ),
              if (canNotify)
                CheckboxListTile(
                  contentPadding: EdgeInsets.zero,
                  value: notify,
                  onChanged: (value) =>
                      setDialogState(() => notify = value ?? false),
                  title: Text(L10n.of(context).ui_notify_everyone_bb085da5),
                  subtitle: Text(L10n.of(context)
                      .ui_send_a_server_wide_stage_start_notification_47396c88),
                ),
            ],
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext),
              child: Text(L10n.of(context).ui_cancel_35afca3b),
            ),
            FilledButton(
              onPressed: () {
                final topic = controller.text.trim();
                if (topic.isNotEmpty) {
                  Navigator.pop(dialogContext, (topic: topic, notify: notify));
                }
              },
              child: Text(L10n.of(context).ui_start_stage_1d128645),
            ),
          ],
        ),
      ),
    );
    controller.dispose();
    return options;
  }

  Future<void> _startStage() async {
    final options = await _stageStartDialog();
    if (options == null || !mounted) return;
    if (!canStartStageNow(channel, notify: options.notify)) {
      _showVoiceError(
        UserInputException('Stage permissions changed. Try again.'),
        'Could not start this Stage',
      );
      return;
    }
    setState(() => _stageLoading = true);
    try {
      final instance = await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .createStageInstance(
            channel.ref,
            options.topic,
            sendStartNotification: options.notify,
          );
      if (mounted) setState(() => _stageInstance = instance);
    } on Object catch (error) {
      if (mounted) {
        _showVoiceError(error, 'Could not start this Stage');
      }
    } finally {
      if (mounted) setState(() => _stageLoading = false);
    }
  }

  void _onStageEvent(Map<String, Object?> event) {
    if (!mounted || channel.type != ChannelType.stage) return;
    final channelId = '${event['channel_id']}';
    final channelDomain = '${event['channel_domain']}';
    if (channelId != channel.ref.id.value ||
        channelDomain != channel.ref.domain.value) {
      return;
    }
    final eventType = '${event['event_type']}';
    if (eventType == 'STAGE_INSTANCE_DELETE') {
      setState(() {
        _stageInstance = null;
        _stageLoaded = true;
      });
      return;
    }
    try {
      final instance = StageInstance.fromJson(event);
      setState(() {
        _stageInstance = instance;
        _stageLoaded = true;
      });
      if (eventType == 'STAGE_INSTANCE_CREATE' &&
          event['notify_client'] == true) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
              content: Text(L10n.of(context).ui_stage_started_value0_9fecc0d3(
                  (instance.topic).toString()))),
        );
      }
    } on Object {
      // The next REST refresh repairs malformed or newer additive projections.
    }
  }

  Future<void> _editStage() async {
    final current = _stageInstance;
    if (current == null) return;
    final topic = await _stageTopicDialog(
      title: L10n.of(context).ui_edit_stage_topic_b450a62c,
      initial: current.topic,
    );
    if (topic == null || topic == current.topic || !mounted) return;
    if (!_canManageStage || _stageInstance?.ref != current.ref) {
      _showVoiceError(
        UserInputException('Stage permissions changed. Try again.'),
        'Could not update the Stage topic',
      );
      return;
    }
    setState(() => _stageLoading = true);
    try {
      final instance = await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .updateStageInstance(channel.ref, topic);
      if (mounted) setState(() => _stageInstance = instance);
    } on Object catch (error) {
      if (mounted) {
        _showVoiceError(error, 'Could not update the Stage topic');
      }
    } finally {
      if (mounted) setState(() => _stageLoading = false);
    }
  }

  Future<void> _endStage() async {
    final confirmed = await showDialog<bool>(
          context: context,
          builder: (dialogContext) => AlertDialog(
            title: Text(L10n.of(context).ui_end_stage_b7c63115),
            content: Text(L10n.of(context)
                .ui_the_live_stage_will_end_for_everyone_7d77ab3b),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(dialogContext, false),
                child: Text(L10n.of(context).ui_cancel_35afca3b),
              ),
              FilledButton(
                onPressed: () => Navigator.pop(dialogContext, true),
                child: Text(L10n.of(context).ui_end_stage_2bd54b88),
              ),
            ],
          ),
        ) ??
        false;
    if (!confirmed || !mounted) return;
    final current = _stageInstance;
    if (!_canManageStage || current == null) {
      _showVoiceError(
        UserInputException('Stage permissions changed. Try again.'),
        'Could not end this Stage',
      );
      return;
    }
    setState(() => _stageLoading = true);
    try {
      await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .deleteStageInstance(channel.ref);
      if (mounted) setState(() => _stageInstance = null);
    } on Object catch (error) {
      if (mounted) _showVoiceError(error, 'Could not end this Stage');
    } finally {
      if (mounted) setState(() => _stageLoading = false);
    }
  }

  void _showVoiceError(Object error, String summary) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(userFacingError(error, summary: summary))),
    );
  }

  String? _soundboardUnavailableReason(VoiceSession session) {
    final localParticipant =
        session.participants.whereType<LocalParticipant>().firstOrNull;
    final occupancy = localParticipant == null
        ? null
        : session.occupant(localParticipant.identity);
    return soundboardPlaybackUnavailableReason(
      connected: session.connected,
      canSpeak: session.canSpeak && occupancy?['can_speak'] != false,
      selfMuted: session.muted || occupancy?['self_mute'] == true,
      selfDeafened: session.deafened,
      serverMuted: occupancy?['server_mute'] == true,
      serverDeafened: occupancy?['server_deaf'] == true,
      suppressed: occupancy?['suppressed'] == true,
    );
  }

  Widget _controls(
    BuildContext context,
    WidgetRef ref,
    MobileState mobile,
    VoiceSession session,
  ) {
    final soundboardUnavailable = _soundboardUnavailableReason(session);
    return Material(
      color: context.kaede.panel,
      child: SafeArea(
        top: false,
        child: Padding(
          padding: EdgeInsets.fromLTRB(12, 10, 12, 12),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (session.pushToTalk)
                Padding(
                  padding: EdgeInsets.only(bottom: 8),
                  child: SizedBox(
                    width: double.infinity,
                    child: Listener(
                      onPointerDown: (_) => unawaited(_runVoiceAction(
                        context,
                        () => session.setPushHeld(true),
                      )),
                      onPointerUp: (_) => unawaited(_runVoiceAction(
                        context,
                        () => session.setPushHeld(false),
                      )),
                      onPointerCancel: (_) => unawaited(_runVoiceAction(
                        context,
                        () => session.setPushHeld(false),
                      )),
                      child: FilledButton.tonalIcon(
                        onPressed: () {},
                        icon: Icon(session.pushHeld
                            ? Icons.mic_rounded
                            : Icons.mic_off_rounded),
                        label: Text(session.pushHeld
                            ? L10n.of(context)
                                .ui_transmitting_release_to_stop_a7501aad
                            : L10n.of(context).ui_hold_to_talk_957f6511),
                      ),
                    ),
                  ),
                ),
              Row(
                children: [
                  if (widget.onApps case final onApps?)
                    Expanded(
                      child: _control(
                        context,
                        Icons.apps_rounded,
                        'Apps',
                        onApps,
                      ),
                    ),
                  Expanded(
                      child: _control(
                    context,
                    session.muted ? Icons.mic_off_rounded : Icons.mic_rounded,
                    session.muted ? 'Unmute' : 'Mute',
                    session.canSpeak ? session.toggleMute : null,
                    selected: session.muted,
                  )),
                  Expanded(
                      child: _control(
                    context,
                    session.deafened
                        ? Icons.headset_off_rounded
                        : Icons.headphones_rounded,
                    session.deafened ? 'Undeafen' : 'Deafen',
                    session.toggleDeafen,
                    selected: session.deafened,
                  )),
                  Expanded(
                      child: _control(
                    context,
                    session.camera
                        ? Icons.videocam_rounded
                        : Icons.videocam_off,
                    session.camera ? 'Camera off' : 'Camera',
                    session.canStream ? session.toggleCamera : null,
                    selected: session.camera,
                  )),
                  Expanded(
                      child: _control(
                    context,
                    Icons.screen_share_rounded,
                    session.screen ? 'Stop sharing' : 'Share',
                    session.canStream
                        ? session.screen
                            ? session.stopScreenShare
                            : () => _showScreenShare(context, session)
                        : null,
                    selected: session.screen,
                  )),
                  if (soundboardChannelSupported(
                        channelType: channel.type == ChannelType.voice ? 2 : 13,
                        directCall: callRef != null,
                      ) &&
                      channel.allows(Permission.speak) &&
                      channel.allows(Permission.useSoundboard))
                    Expanded(
                        child: _control(
                      context,
                      Icons.music_note_rounded,
                      'Sounds',
                      soundboardUnavailable == null
                          ? () => _showSoundboard(
                                context,
                                ref,
                                mobile,
                                session,
                              )
                          : null,
                      tooltip: soundboardUnavailable,
                    )),
                  Expanded(
                      child: _control(
                    context,
                    session.audioRoute == VoiceAudioRoute.bluetooth
                        ? Icons.bluetooth_audio_rounded
                        : session.audioRoute == VoiceAudioRoute.speaker
                            ? Icons.volume_up_rounded
                            : Icons.phone_android_rounded,
                    'Audio',
                    () => _showAudioRoutes(context, session),
                  )),
                  Expanded(
                    child: _control(
                      context,
                      Icons.call_end_rounded,
                      channel.type == ChannelType.stage
                          ? 'Exit quietly'
                          : 'Leave',
                      () => session.leave(),
                      destructive: true,
                    ),
                  ),
                ],
              ),
              if (session.canSpeak && (session.pushToTalk || session.canUseVad))
                TextButton.icon(
                  onPressed: () => _runVoiceAction(
                    context,
                    session.toggleInputMode,
                  ),
                  icon: Icon(session.pushToTalk
                      ? Icons.touch_app_rounded
                      : Icons.graphic_eq_rounded),
                  label: Text(session.pushToTalk
                      ? L10n.of(context).ui_push_to_talk_40dec4c0
                      : L10n.of(context).ui_voice_activity_88bfed22),
                ),
            ],
          ),
        ),
      ),
    );
  }

  Future<void> _showSoundboard(
    BuildContext context,
    WidgetRef ref,
    MobileState mobile,
    VoiceSession session,
  ) async {
    if (_soundboardUnavailableReason(session) != null) return;
    final repository = ref.read(mobileControllerProvider.notifier).repository;
    final canUseExternalSounds = channel.allows(Permission.useExternalSounds);
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (sheetContext) => _SoundboardPicker(
        groups: _loadSoundboardGroups(
          repository,
          mobile.guilds,
          channel.guildRef,
          canUseExternalSounds: canUseExternalSounds,
        ),
        onPlay: (sound) async {
          if (!channel.allows(Permission.speak) ||
              !channel.allows(Permission.useSoundboard) ||
              _soundboardUnavailableReason(session) != null ||
              !soundboardSourceAllowed(
                targetGuildRef: channel.guildRef,
                sourceGuildRef: sound.guildRef,
                canUseExternalSounds:
                    channel.allows(Permission.useExternalSounds),
              )) {
            return;
          }
          Navigator.pop(sheetContext);
          try {
            await repository.playSoundboardSound(
              channel.ref,
              sound.ref,
              sound.guildRef,
              soundVersion: sound.version,
            );
          } on Object catch (error) {
            if (!context.mounted) return;
            ScaffoldMessenger.of(context).showSnackBar(
              SnackBar(
                content: Text(userFacingError(
                  error,
                  summary:
                      L10n.of(context).ui_could_not_play_that_sound_cb8dc1ff,
                )),
              ),
            );
          }
        },
      ),
    );
  }

  Future<void> _showScreenShare(
    BuildContext context,
    VoiceSession session,
  ) async {
    var screen = session.mediaQuality.screen;
    var audio = session.mediaQuality.audio;
    final dtx = session.mediaQuality.dtx;
    var starting = false;
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      builder: (sheetContext) => StatefulBuilder(
        builder: (context, setSheetState) => SingleChildScrollView(
          padding: EdgeInsets.fromLTRB(
            20,
            0,
            20,
            20 + MediaQuery.viewInsetsOf(context).bottom,
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            mainAxisSize: MainAxisSize.min,
            children: [
              Row(
                children: [
                  Container(
                    width: 44,
                    height: 44,
                    decoration: BoxDecoration(
                      color: context.kaede.coralSoft,
                      borderRadius: BorderRadius.circular(14),
                    ),
                    child: Icon(
                      Icons.screen_share_rounded,
                      color: context.kaede.coralText,
                    ),
                  ),
                  SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(L10n.of(context).ui_share_your_screen_2fbd40bd,
                            style: Theme.of(context).textTheme.titleLarge),
                        Text(
                          L10n.of(context)
                              .ui_choose_quality_before_the_system_asks_what_to_29e0d348,
                          style: TextStyle(color: context.kaede.muted),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
              SizedBox(height: 22),
              Text(L10n.of(context).ui_video_quality_7fef5a25,
                  style: Theme.of(context).textTheme.labelSmall?.copyWith(
                        color: context.kaede.muted,
                        fontWeight: FontWeight.w800,
                        letterSpacing: 0.8,
                      )),
              SizedBox(height: 8),
              GridView.count(
                crossAxisCount: 2,
                shrinkWrap: true,
                physics: NeverScrollableScrollPhysics(),
                mainAxisSpacing: 8,
                crossAxisSpacing: 8,
                childAspectRatio: 2.35,
                children: [
                  for (final quality in ScreenShareQuality.values)
                    _qualityChoice(
                      context,
                      selected: quality == screen,
                      title: quality.profile.label,
                      subtitle: quality.profile.description,
                      onTap: () => setSheetState(() => screen = quality),
                    ),
                ],
              ),
              SizedBox(height: 20),
              Text(L10n.of(context).ui_outgoing_audio_4b8c2905,
                  style: Theme.of(context).textTheme.labelSmall?.copyWith(
                        color: context.kaede.muted,
                        fontWeight: FontWeight.w800,
                        letterSpacing: 0.8,
                      )),
              SizedBox(height: 8),
              Wrap(
                spacing: 7,
                runSpacing: 7,
                children: [
                  for (final quality in VoiceAudioQuality.values)
                    ChoiceChip(
                      selected: quality == audio,
                      onSelected: (_) => setSheetState(() => audio = quality),
                      label: Text(L10n.of(context)
                          .ui_value0_value1_kbps_278fa6b3(
                              (quality.label).toString(),
                              (quality.bitrate ~/ 1000).toString())),
                    ),
                ],
              ),
              SizedBox(height: 8),
              Text(
                L10n.of(context)
                    .ui_bitrate_is_an_upper_target_opus_automatically_3a7120a2,
                style: TextStyle(fontSize: 12, color: context.kaede.muted),
              ),
              SizedBox(height: 16),
              Container(
                padding: EdgeInsets.all(13),
                decoration: BoxDecoration(
                  color: context.kaede.panel,
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Icon(Icons.shield_outlined,
                        size: 20, color: context.kaede.muted),
                    SizedBox(width: 9),
                    Expanded(
                      child: Text(
                        L10n.of(context)
                            .ui_android_and_ios_always_use_a_protected_system_4a3f370f,
                        style: TextStyle(
                          fontSize: 12,
                          color: context.kaede.textSoft,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
              SizedBox(height: 18),
              FilledButton.icon(
                onPressed: starting
                    ? null
                    : () async {
                        setSheetState(() => starting = true);
                        try {
                          await session.startScreenShare(MobileMediaQuality(
                            screen: screen,
                            audio: audio,
                            dtx: dtx,
                          ));
                          final chooserWasPresented = !kIsWeb &&
                              defaultTargetPlatform == TargetPlatform.iOS;
                          if (sheetContext.mounted &&
                              (session.screen || chooserWasPresented)) {
                            Navigator.pop(sheetContext);
                          }
                        } on Object catch (error) {
                          if (sheetContext.mounted) {
                            ScaffoldMessenger.of(sheetContext).showSnackBar(
                              SnackBar(
                                content: Text(userFacingError(
                                  error,
                                  summary: L10n.of(context)
                                      .ui_could_not_start_screen_sharing_a459a6a5,
                                )),
                              ),
                            );
                          }
                        } finally {
                          if (sheetContext.mounted) {
                            setSheetState(() => starting = false);
                          }
                        }
                      },
                icon: Icon(Icons.screen_share_rounded),
                label: Text(starting
                    ? L10n.of(context).ui_starting_73727cef
                    : L10n.of(context).ui_choose_what_to_share_66a4c034),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _qualityChoice(
    BuildContext context, {
    required bool selected,
    required String title,
    required String subtitle,
    required VoidCallback onTap,
  }) =>
      Material(
        color: selected ? context.kaede.coralSoft : context.kaede.raised,
        shape: RoundedRectangleBorder(
          side: BorderSide(
            color: selected ? context.kaede.coral : context.kaede.border,
          ),
          borderRadius: BorderRadius.circular(12),
        ),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          onTap: onTap,
          child: Padding(
            padding: EdgeInsets.symmetric(horizontal: 12, vertical: 9),
            child: Row(
              children: [
                Expanded(
                  child: Column(
                    mainAxisAlignment: MainAxisAlignment.center,
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(title,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(fontWeight: FontWeight.w700)),
                      Text(subtitle,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                              fontSize: 11, color: context.kaede.muted)),
                    ],
                  ),
                ),
                if (selected)
                  Icon(Icons.check_circle_rounded,
                      size: 18, color: context.kaede.coralText),
              ],
            ),
          ),
        ),
      );

  Future<void> _showAudioRoutes(
    BuildContext context,
    VoiceSession session,
  ) async {
    final routes = await session.availableAudioRoutes();
    if (!context.mounted) return;
    final selected = await showModalBottomSheet<VoiceAudioRoute>(
      context: context,
      showDragHandle: true,
      builder: (context) => SafeArea(
        child: RadioGroup<VoiceAudioRoute>(
          groupValue: session.audioRoute,
          onChanged: (value) => Navigator.pop(context, value),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Padding(
                padding: EdgeInsets.fromLTRB(20, 2, 20, 10),
                child: Text(L10n.of(context).ui_select_audio_output_a5576730,
                    style: Theme.of(context).textTheme.titleLarge),
              ),
              for (final route in routes)
                RadioListTile<VoiceAudioRoute>(
                  value: route,
                  secondary: Icon(switch (route) {
                    VoiceAudioRoute.phone => Icons.phone_android_rounded,
                    VoiceAudioRoute.speaker => Icons.volume_up_rounded,
                    VoiceAudioRoute.bluetooth => Icons.bluetooth_audio_rounded,
                  }),
                  title: Text(switch (route) {
                    VoiceAudioRoute.phone => L10n.of(context).ui_phone_4930944f,
                    VoiceAudioRoute.speaker =>
                      L10n.of(context).ui_speaker_6c7955e2,
                    VoiceAudioRoute.bluetooth =>
                      L10n.of(context).ui_bluetooth_headset_a4c74b8b,
                  }),
                ),
            ],
          ),
        ),
      ),
    );
    if (selected != null) await session.selectAudioRoute(selected);
  }

  Widget _control(BuildContext context, IconData icon, String label,
          Future<void> Function()? tap,
          {bool selected = false, bool destructive = false, String? tooltip}) =>
      Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          IconButton.filledTonal(
            tooltip: tooltip ?? label,
            onPressed: tap == null ? null : () => _runVoiceAction(context, tap),
            style: IconButton.styleFrom(
              backgroundColor: destructive
                  ? context.kaede.dangerSoft
                  : selected
                      ? context.kaede.coralSoft
                      : context.kaede.raised,
              foregroundColor: destructive
                  ? context.kaede.danger
                  : selected
                      ? context.kaede.coralText
                      : context.kaede.text,
              disabledBackgroundColor: context.kaede.panel,
              disabledForegroundColor: context.kaede.muted,
            ),
            icon: Icon(icon, size: 21),
          ),
          SizedBox(height: 3),
          Text(label,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                fontSize: 10,
                fontWeight: FontWeight.w600,
                color: destructive
                    ? context.kaede.danger
                    : tap == null
                        ? context.kaede.muted
                        : context.kaede.textSoft,
              )),
        ],
      );

  Future<void> _runVoiceAction(
    BuildContext context,
    Future<void> Function() operation,
  ) async {
    try {
      await operation();
    } on Object catch (error) {
      if (!context.mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(userFacingError(
          error,
          summary:
              L10n.of(context).ui_could_not_complete_the_voice_action_eafa3403,
        )),
      ));
    }
  }
}

final class _StageParticipantRoster extends StatelessWidget {
  const _StageParticipantRoster({
    required this.participants,
    required this.session,
    required this.mobile,
    required this.guild,
    required this.channel,
  });

  final List<Participant> participants;
  final VoiceSession session;
  final MobileState mobile;
  final KaedeGuild? guild;
  final KaedeChannel channel;

  @override
  Widget build(BuildContext context) {
    final speakers = <Participant>[];
    final requesting = <Participant>[];
    final audience = <Participant>[];
    for (final participant in participants) {
      final state = session.occupant(participant.identity);
      final suppressed = state?['suppressed'] != false;
      if (!suppressed) {
        speakers.add(participant);
      } else if (state?['request_to_speak_timestamp'] != null) {
        requesting.add(participant);
      } else {
        audience.add(participant);
      }
    }
    return ListView(
      padding: EdgeInsets.all(12),
      children: [
        _group(context, 'Speakers', speakers),
        _group(context, 'Requested to speak', requesting),
        _group(context, 'Audience', audience),
        if (participants.isEmpty)
          Padding(
            padding: EdgeInsets.symmetric(vertical: 48),
            child: Center(
              child: Text(
                L10n.of(context)
                    .ui_stage_participants_will_appear_here_as_they_j_aad90d2e,
                style: TextStyle(color: context.kaede.muted),
              ),
            ),
          ),
      ],
    );
  }

  Widget _group(
    BuildContext context,
    String title,
    List<Participant> items,
  ) {
    if (items.isEmpty) return SizedBox.shrink();
    final videoTiles = _voiceVideoTiles(items);
    return Padding(
      padding: EdgeInsets.only(bottom: 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            L10n.of(context).ui_value0_value1_c34e3562(
                (title).toString(), (items.length).toString()),
            style: Theme.of(context).textTheme.labelLarge?.copyWith(
                  color: context.kaede.muted,
                ),
          ),
          SizedBox(height: 8),
          GridView.builder(
            shrinkWrap: true,
            physics: NeverScrollableScrollPhysics(),
            gridDelegate: SliverGridDelegateWithMaxCrossAxisExtent(
              maxCrossAxisExtent: 320,
              childAspectRatio: 16 / 10,
              crossAxisSpacing: 10,
              mainAxisSpacing: 10,
            ),
            itemCount: videoTiles.length,
            itemBuilder: (context, index) => _ParticipantTile(
              participant: videoTiles[index].participant,
              source: videoTiles[index].source,
              knownName: _knownVoiceParticipantName(
                mobile,
                videoTiles[index].participant.identity,
              ),
              session: session,
              guild: guild,
              actorRef: mobile.user?.ref,
              targetMember: guild == null
                  ? null
                  : guildMemberByIdentity(
                      mobile.guildMembers[guild!.ref] ?? const <GuildMember>[],
                      videoTiles[index].participant.identity,
                    ),
              channel: channel,
            ),
          ),
        ],
      ),
    );
  }
}

final class _ParticipantTile extends StatefulWidget {
  const _ParticipantTile({
    required this.participant,
    required this.source,
    required this.knownName,
    required this.session,
    required this.channel,
    required this.actorRef,
    required this.targetMember,
    this.guild,
  });

  final Participant participant;
  final TrackSource source;
  final String? knownName;
  final VoiceSession session;
  final KaedeGuild? guild;
  final EntityRef? actorRef;
  final GuildMember? targetMember;
  final KaedeChannel channel;

  @override
  State<_ParticipantTile> createState() => _ParticipantTileState();
}

final class _ParticipantTileState extends State<_ParticipantTile> {
  @override
  void initState() {
    super.initState();
    widget.participant.addListener(_changed);
  }

  @override
  void didUpdateWidget(covariant _ParticipantTile oldWidget) {
    oldWidget.participant.removeListener(_changed);
    widget.participant.addListener(_changed);
    super.didUpdateWidget(oldWidget);
  }

  @override
  void dispose() {
    widget.participant.removeListener(_changed);
    super.dispose();
  }

  void _changed() {
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final publication = widget.participant.videoTrackPublications
        .where((publication) =>
            publication.source == widget.source &&
            !publication.muted &&
            publication.track != null)
        .firstOrNull;
    final track = publication?.track;
    final identity = widget.participant.identity;
    final name = voiceParticipantLabel(
      liveName: widget.participant.name,
      identity: identity,
      knownName: widget.knownName,
    );
    return GestureDetector(
      onTap: widget.participant is RemoteParticipant
          ? () => _showParticipantControls(context, identity, name)
          : null,
      child: AnimatedContainer(
        duration: Duration(milliseconds: 120),
        clipBehavior: Clip.antiAlias,
        decoration: BoxDecoration(
          color: context.kaede.raised,
          borderRadius: BorderRadius.circular(18),
          border: Border.all(
            color: widget.participant.isSpeaking
                ? context.kaede.mint
                : context.kaede.border,
            width: widget.participant.isSpeaking ? 3 : 1,
          ),
        ),
        child: Stack(
          fit: StackFit.expand,
          children: [
            if (track is VideoTrack)
              VideoTrackRenderer(track)
            else
              Center(
                child: CircleAvatar(
                  radius: 36,
                  backgroundColor: context.kaede.coralDark,
                  child: Text(name.isEmpty ? '?' : name[0].toUpperCase(),
                      style:
                          TextStyle(fontSize: 28, fontWeight: FontWeight.w800)),
                ),
              ),
            Align(
              alignment: Alignment.bottomLeft,
              child: Container(
                margin: EdgeInsets.all(10),
                padding: EdgeInsets.symmetric(horizontal: 10, vertical: 6),
                decoration: BoxDecoration(
                  color: Colors.black.withValues(alpha: .68),
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    if (widget.source == TrackSource.screenShareVideo) ...[
                      Icon(Icons.screen_share_outlined,
                          color: Colors.white, size: 16),
                      SizedBox(width: 6),
                    ],
                    Text(name, style: TextStyle(color: Colors.white)),
                    if (widget.participant is RemoteParticipant) ...[
                      SizedBox(width: 6),
                      Icon(Icons.more_horiz_rounded,
                          color: Colors.white70, size: 18),
                    ],
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _runVolumeChange(String identity, double value,
      {bool stream = false}) async {
    try {
      await widget.session
          .setParticipantVolume(identity, value, stream: stream);
    } on Object {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
            content: Text('Could not change volume. Try again.')));
      }
    }
  }

  Future<void> _showParticipantControls(
    BuildContext context,
    String identity,
    String name,
  ) async {
    final guild = widget.guild;
    EntityRef? user;
    try {
      user = EntityRef.parse(identity);
    } on FormatException {
      // LiveKit identities should always be canonical entity references. Keep
      // volume usable if an older peer supplied a legacy identity, but do not
      // expose moderation actions for an ambiguous target.
    }
    final occupancy = widget.session.occupant(identity);
    final canModerate = user != null && _canModerateTarget(user);
    var volume = widget.session.participantVolume(identity);
    var streamVolume = widget.session.streamVolume(identity);
    final hasStream = widget.participant.audioTrackPublications
            .any((p) => p.source == TrackSource.screenShareAudio) ||
        widget.source == TrackSource.screenShareVideo;
    if (!context.mounted) return;
    await showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      isScrollControlled: true,
      constraints:
          BoxConstraints(maxHeight: MediaQuery.sizeOf(context).height * .85),
      builder: (sheetContext) => SingleChildScrollView(
          child: StatefulBuilder(
        builder: (context, setModalState) => SafeArea(
          child: Padding(
            padding: EdgeInsets.fromLTRB(20, 4, 20, 20),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(children: [
                  CircleAvatar(
                      radius: 24,
                      child: Text(name.isEmpty
                          ? '?'
                          : name.characters.first.toUpperCase())),
                  const SizedBox(width: 12),
                  Expanded(
                      child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                        Text(name,
                            style: Theme.of(context)
                                .textTheme
                                .titleMedium
                                ?.copyWith(fontWeight: FontWeight.bold)),
                        const SizedBox(height: 4),
                        Text('Only changes what you hear',
                            style: Theme.of(context).textTheme.bodySmall)
                      ]))
                ]),
                const SizedBox(height: 24),
                Row(children: [
                  const Icon(Icons.mic_none_outlined, size: 20),
                  const SizedBox(width: 8),
                  const Expanded(child: Text('User volume')),
                  Text('${(volume * 100).round()}%',
                      style: Theme.of(context).textTheme.labelLarge)
                ]),
                Slider(
                  value: volume,
                  onChanged: (value) {
                    setModalState(() => volume = value);
                    _runVolumeChange(identity, value);
                  },
                ),
                if (hasStream) ...[
                  const SizedBox(height: 12),
                  Row(children: [
                    const Icon(Icons.screen_share_outlined, size: 20),
                    const SizedBox(width: 8),
                    const Expanded(child: Text('Stream volume')),
                    Text('${(streamVolume * 100).round()}%',
                        style: Theme.of(context).textTheme.labelLarge)
                  ]),
                  Slider(
                    value: streamVolume,
                    semanticFormatterCallback: (value) =>
                        'Stream volume ${(value * 100).round()}%',
                    onChanged: (value) {
                      setModalState(() => streamVolume = value);
                      _runVolumeChange(identity, value, stream: true);
                    },
                  ),
                ],
                if (guild != null && user != null && canModerate) ...[
                  Divider(),
                  if (canManageStageChannel(widget.channel))
                    ListTile(
                      leading: Icon(occupancy?['suppressed'] != false
                          ? Icons.record_voice_over_outlined
                          : Icons.hearing_outlined),
                      title: Text(
                          occupancy?[L10n.of(context).ui_suppressed_087253d7] !=
                                  false
                              ? L10n.of(context).ui_invite_to_speak_1cee4bdb
                              : L10n.of(context).ui_move_to_audience_de051085),
                      onTap: () => _moderate(
                        sheetContext,
                        () => widget.session.setStageParticipantSuppressed(
                          guild.ref,
                          user!,
                          occupancy?['suppressed'] == false,
                        ),
                        authorized: () =>
                            _canModerateTarget(user!) &&
                            canManageStageChannel(widget.channel),
                      ),
                    ),
                  if (voiceChannelAllows(
                      widget.channel, Permission.muteMembers))
                    ListTile(
                      leading: Icon(Icons.mic_off_rounded),
                      title: Text(occupancy?[
                                  L10n.of(context).ui_server_mute_8c8094fe] ==
                              true
                          ? L10n.of(context).ui_remove_server_mute_a9a2d0ad
                          : L10n.of(context).ui_server_mute_5de99007),
                      onTap: () => _moderate(
                        sheetContext,
                        () => widget.session.setServerMute(
                          guild.ref,
                          user!,
                          occupancy?['server_mute'] != true,
                        ),
                        authorized: () =>
                            _canModerateTarget(user!) &&
                            voiceChannelAllows(
                                widget.channel, Permission.muteMembers),
                      ),
                    ),
                  if (canServerDeafenInChannel(widget.channel))
                    ListTile(
                      leading: Icon(Icons.headset_off_rounded),
                      title: Text(occupancy?[
                                  L10n.of(context).ui_server_deaf_513c8fbb] ==
                              true
                          ? L10n.of(context).ui_remove_server_deafen_37df1a67
                          : L10n.of(context).ui_server_deafen_5b9a5cf9),
                      onTap: () => _moderate(
                        sheetContext,
                        () => widget.session.setServerDeaf(
                          guild.ref,
                          user!,
                          occupancy?['server_deaf'] != true,
                        ),
                        authorized: () =>
                            _canModerateTarget(user!) &&
                            canServerDeafenInChannel(widget.channel),
                      ),
                    ),
                  if (voiceChannelAllows(
                      widget.channel, Permission.moveMembers)) ...[
                    for (final target
                        in voiceMoveDestinationChannels(guild, widget.channel))
                      ListTile(
                        leading: Icon(Icons.drive_file_move_rounded),
                        title: Text(L10n.of(context).ui_move_to_value0_5addb086(
                            (target.name ?? 'voice').toString())),
                        onTap: () => _moderate(
                          sheetContext,
                          () => widget.session.moveParticipant(
                            guild.ref,
                            user!,
                            target.ref,
                          ),
                          authorized: () =>
                              _canModerateTarget(user!) &&
                              voiceChannelAllows(
                                  widget.channel, Permission.moveMembers) &&
                              voiceChannelAllows(
                                  target, Permission.moveMembers),
                        ),
                      ),
                    ListTile(
                      leading: Icon(Icons.call_end_rounded,
                          color: Theme.of(context).colorScheme.error),
                      title: Text(
                          L10n.of(context).ui_disconnect_from_voice_a6e0e11f,
                          style: TextStyle(
                              color: Theme.of(context).colorScheme.error)),
                      onTap: () => _moderate(
                        sheetContext,
                        () => widget.session
                            .disconnectParticipant(guild.ref, user!),
                        authorized: () =>
                            _canModerateTarget(user!) &&
                            voiceChannelAllows(
                                widget.channel, Permission.moveMembers),
                      ),
                    ),
                  ],
                ],
              ],
            ),
          ),
        ),
      )),
    );
  }

  Future<void> _moderate(
    BuildContext context,
    Future<void> Function() operation, {
    required bool Function() authorized,
  }) async {
    if (!authorized()) return;
    Navigator.of(context).pop();
    try {
      await operation();
    } on Object catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(this.context).showSnackBar(
        SnackBar(
          content: Text(userFacingError(
            error,
            summary: L10n.current.ui_could_not_update_the_voice_state_3f129c64,
          )),
        ),
      );
    }
  }

  bool _canModerateTarget(EntityRef user) {
    final guild = widget.guild;
    final target = widget.targetMember;
    return guild != null &&
        target?.user.ref == user &&
        guildActorCanManageMember(
          guild: guild,
          actorRef: widget.actorRef,
          actorHighestRole: guildActorHighestRole(guild),
          target: target!,
        );
  }
}

final class _SoundboardPicker extends StatelessWidget {
  const _SoundboardPicker({required this.groups, required this.onPlay});

  final Future<List<_SoundboardGroup>> groups;
  final Future<void> Function(SoundboardSound sound) onPlay;

  @override
  Widget build(BuildContext context) => SafeArea(
        child: SizedBox(
          height: MediaQuery.sizeOf(context).height * .68,
          child: FutureBuilder<List<_SoundboardGroup>>(
            future: groups,
            builder: (context, snapshot) {
              if (!snapshot.hasData && !snapshot.hasError) {
                return Center(child: CircularProgressIndicator());
              }
              if (snapshot.hasError) {
                return const _VoiceNotice(
                  icon: Icons.error_outline_rounded,
                  text: 'Available sounds could not be loaded.',
                );
              }
              final available = snapshot.data ?? const <_SoundboardGroup>[];
              if (available.isEmpty) {
                return Center(
                  child: Text(L10n.of(context)
                      .ui_no_soundboard_sounds_are_available_45e14a9e),
                );
              }
              return ListView(
                padding: EdgeInsets.fromLTRB(12, 0, 12, 20),
                children: [
                  Padding(
                    padding: EdgeInsets.fromLTRB(8, 0, 8, 10),
                    child: Text(
                      L10n.of(context).ui_soundboard_5a91f77c,
                      style: Theme.of(context).textTheme.headlineSmall,
                    ),
                  ),
                  for (final group in available) ...[
                    Padding(
                      padding: EdgeInsets.fromLTRB(8, 14, 8, 4),
                      child: Text(
                        group.label,
                        style: TextStyle(
                          color: context.kaede.muted,
                          fontSize: 12,
                          fontWeight: FontWeight.w800,
                        ),
                      ),
                    ),
                    for (final sound in group.sounds)
                      ListTile(
                        key: ValueKey(
                            'voice-sound-${group.key}-${sound.ref.wire}'),
                        leading: CircleAvatar(
                          child: Text(sound.emojiName?.trim().isNotEmpty == true
                              ? sound.emojiName!
                              : '♫'),
                        ),
                        title: Text(sound.name),
                        onTap: sound.available ? () => onPlay(sound) : null,
                      ),
                  ],
                ],
              );
            },
          ),
        ),
      );
}

final class _VoiceEmpty extends StatelessWidget {
  const _VoiceEmpty({required this.canConnect});

  final bool canConnect;

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: EdgeInsets.all(32),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.spatial_audio_off_rounded,
                  color: context.kaede.coral, size: 64),
              SizedBox(height: 14),
              Text(
                  canConnect
                      ? L10n.of(context).ui_ready_when_you_are_28e0532d
                      : L10n.of(context).ui_voice_unavailable_d810746f,
                  style: Theme.of(context).textTheme.headlineSmall),
              SizedBox(height: 8),
              Text(
                canConnect
                    ? L10n.of(context)
                        .ui_join_the_room_to_see_and_hear_everyone_alread_69de4235
                    : L10n.of(context)
                        .ui_a_guild_role_or_channel_permission_is_prevent_3514b22a,
                textAlign: TextAlign.center,
                style: TextStyle(color: context.kaede.muted),
              ),
            ],
          ),
        ),
      );
}

final class _VoiceTakeoverNotice extends StatelessWidget {
  const _VoiceTakeoverNotice({
    required this.activeClient,
    required this.moving,
    required this.onMove,
    required this.onCancel,
  });

  final String activeClient;
  final bool moving;
  final Future<void> Function() onMove;
  final Future<void> Function() onCancel;

  @override
  Widget build(BuildContext context) => Container(
        width: double.infinity,
        margin: EdgeInsets.fromLTRB(16, 4, 16, 8),
        padding: EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: context.kaede.raised,
          border: Border.all(color: context.kaede.coralText),
          borderRadius: BorderRadius.circular(16),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
                L10n.of(context).ui_voice_is_active_on_value0_0e7eda95(
                    (activeClient).toString()),
                style: TextStyle(fontWeight: FontWeight.w800)),
            SizedBox(height: 4),
            Text(
              L10n.of(context)
                  .ui_moving_voice_here_will_disconnect_that_device_f30aa710,
              style: TextStyle(color: context.kaede.textSoft),
            ),
            SizedBox(height: 10),
            Row(
              children: [
                FilledButton(
                  onPressed: moving ? null : onMove,
                  child: Text(moving
                      ? L10n.of(context).ui_moving_voice_c3ab6c0f
                      : L10n.of(context).ui_move_voice_here_0c2d2f54),
                ),
                SizedBox(width: 8),
                TextButton(
                  onPressed: moving ? null : onCancel,
                  child: Text(L10n.of(context).ui_keep_it_there_3cd7dd37),
                ),
              ],
            ),
          ],
        ),
      );
}

final class _VoiceNotice extends StatelessWidget {
  const _VoiceNotice({required this.icon, required this.text});

  final IconData icon;
  final String text;

  @override
  Widget build(BuildContext context) => Container(
        width: double.infinity,
        margin: EdgeInsets.symmetric(horizontal: 16, vertical: 6),
        padding: EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: Theme.of(context).colorScheme.errorContainer,
          borderRadius: BorderRadius.circular(14),
        ),
        child: Row(
          children: [
            Icon(icon),
            SizedBox(width: 10),
            Expanded(child: Text(text)),
          ],
        ),
      );
}

extension _FirstOrNull<T> on Iterable<T> {
  T? get firstOrNull => isEmpty ? null : first;
}
