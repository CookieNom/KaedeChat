import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart' hide ConnectionState;
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/voice/media_quality.dart';
import 'package:kaede_mobile/src/features/voice/voice_session.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:livekit_client/livekit_client.dart';

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

/// A real LiveKit room rather than a UI-only connection placeholder.
///
/// LiveKit owns capture and playback on mobile so the operating system's AEC,
/// noise suppression, audio routing and call lifecycle remain coherent.
final class VoiceRoom extends ConsumerWidget {
  const VoiceRoom({required this.channel, this.callRef, super.key});

  final KaedeChannel channel;
  final EntityRef? callRef;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final session = ref.watch(voiceSessionProvider);
    final mobile = ref.watch(mobileControllerProvider);
    final guild = mobile.activeGuild;
    final canConnect = callRef != null || channel.allows(Permission.connect);
    final thisRoom =
        session.channel?.ref == channel.ref && session.callRef == callRef;
    final connected = thisRoom && session.connected;
    final joined = thisRoom && session.joined;
    final reconnecting = thisRoom && session.reconnecting;
    final participants = joined ? session.participants : const <Participant>[];
    if (thisRoom && callRef == null) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        session.reconcilePermissions(channel);
      });
    }

    return SafeArea(
      child: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 14, 16, 8),
            child: Row(
              children: [
                const CircleAvatar(
                  backgroundColor: KaedeColors.coralDark,
                  child: Icon(Icons.graphic_eq_rounded),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                          channel.name ??
                              (callRef == null ? 'Voice channel' : 'Call'),
                          style: Theme.of(context).textTheme.titleLarge),
                      Text(
                        connected
                            ? '${participants.length} connected'
                            : reconnecting
                                ? 'Reconnecting… your place in the call is being kept'
                                : 'Join to talk, listen, and share video',
                        style: const TextStyle(color: KaedeColors.muted),
                      ),
                    ],
                  ),
                ),
                if (!joined)
                  FilledButton.icon(
                    onPressed: canConnect && !session.connecting
                        ? () => session.connect(channel, callRef: callRef)
                        : null,
                    icon: session.connecting && thisRoom
                        ? const SizedBox.square(
                            dimension: 18,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.call_rounded),
                    label: Text(session.connecting && thisRoom
                        ? 'Connecting…'
                        : 'Join voice'),
                  ),
              ],
            ),
          ),
          if (!canConnect)
            const _VoiceNotice(
              icon: Icons.lock_outline_rounded,
              text: 'You do not have permission to join this voice channel.',
            ),
          if (thisRoom)
            if (session.error case final error?)
              _VoiceNotice(icon: Icons.error_outline_rounded, text: error),
          Expanded(
            child: !joined
                ? _VoiceEmpty(canConnect: canConnect)
                : reconnecting
                    ? const Center(
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            CircularProgressIndicator(),
                            SizedBox(height: 14),
                            Text('Restoring voice connection…'),
                          ],
                        ),
                      )
                    : GridView.builder(
                        padding: const EdgeInsets.all(12),
                        gridDelegate:
                            const SliverGridDelegateWithMaxCrossAxisExtent(
                          maxCrossAxisExtent: 420,
                          childAspectRatio: 16 / 10,
                          crossAxisSpacing: 10,
                          mainAxisSpacing: 10,
                        ),
                        itemCount: participants.length,
                        itemBuilder: (context, index) => _ParticipantTile(
                          participant: participants[index],
                          knownName: _knownVoiceParticipantName(
                            mobile,
                            participants[index].identity,
                          ),
                          session: session,
                          guild: guild,
                          channel: channel,
                        ),
                      ),
          ),
          if (joined) _controls(context, session),
        ],
      ),
    );
  }

  Widget _controls(BuildContext context, VoiceSession session) => Material(
        color: KaedeColors.panel,
        child: SafeArea(
          top: false,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(12, 10, 12, 12),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (session.pushToTalk)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 8),
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
                              ? 'Transmitting… release to stop'
                              : 'Hold to talk'),
                        ),
                      ),
                    ),
                  ),
                Row(
                  children: [
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
                        child: _control(context, Icons.call_end_rounded,
                            'Leave', () => session.leave(),
                            destructive: true)),
                  ],
                ),
                if (session.canSpeak &&
                    (session.pushToTalk || session.canUseVad))
                  TextButton.icon(
                    onPressed: () => _runVoiceAction(
                      context,
                      session.toggleInputMode,
                    ),
                    icon: Icon(session.pushToTalk
                        ? Icons.touch_app_rounded
                        : Icons.graphic_eq_rounded),
                    label: Text(
                        session.pushToTalk ? 'Push to talk' : 'Voice activity'),
                  ),
              ],
            ),
          ),
        ),
      );

  Future<void> _showScreenShare(
    BuildContext context,
    VoiceSession session,
  ) async {
    var screen = session.mediaQuality.screen;
    var audio = session.mediaQuality.audio;
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
                      color: KaedeColors.coralSoft,
                      borderRadius: BorderRadius.circular(14),
                    ),
                    child: const Icon(
                      Icons.screen_share_rounded,
                      color: KaedeColors.coralText,
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text('Share your screen',
                            style: Theme.of(context).textTheme.titleLarge),
                        const Text(
                          'Choose quality before the system asks what to share.',
                          style: TextStyle(color: KaedeColors.muted),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 22),
              Text('VIDEO QUALITY',
                  style: Theme.of(context).textTheme.labelSmall?.copyWith(
                        color: KaedeColors.muted,
                        fontWeight: FontWeight.w800,
                        letterSpacing: 0.8,
                      )),
              const SizedBox(height: 8),
              GridView.count(
                crossAxisCount: 2,
                shrinkWrap: true,
                physics: const NeverScrollableScrollPhysics(),
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
              const SizedBox(height: 20),
              Text('OUTGOING AUDIO',
                  style: Theme.of(context).textTheme.labelSmall?.copyWith(
                        color: KaedeColors.muted,
                        fontWeight: FontWeight.w800,
                        letterSpacing: 0.8,
                      )),
              const SizedBox(height: 8),
              Wrap(
                spacing: 7,
                runSpacing: 7,
                children: [
                  for (final quality in VoiceAudioQuality.values)
                    ChoiceChip(
                      selected: quality == audio,
                      onSelected: (_) => setSheetState(() => audio = quality),
                      label: Text(
                          '${quality.label} · ${quality.bitrate ~/ 1000} kbps'),
                    ),
                ],
              ),
              const SizedBox(height: 8),
              const Text(
                'Bitrate is an upper target. Opus automatically uses less bandwidth during silence and congestion.',
                style: TextStyle(fontSize: 12, color: KaedeColors.muted),
              ),
              const SizedBox(height: 16),
              Container(
                padding: const EdgeInsets.all(13),
                decoration: BoxDecoration(
                  color: KaedeColors.panel,
                  borderRadius: BorderRadius.circular(12),
                ),
                child: const Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Icon(Icons.shield_outlined,
                        size: 20, color: KaedeColors.muted),
                    SizedBox(width: 9),
                    Expanded(
                      child: Text(
                        'Android and iOS always use a protected system chooser. Kaede cannot see other apps or your screen until you approve sharing.',
                        style: TextStyle(
                          fontSize: 12,
                          color: KaedeColors.textSoft,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 18),
              FilledButton.icon(
                onPressed: starting
                    ? null
                    : () async {
                        setSheetState(() => starting = true);
                        try {
                          await session.startScreenShare(MobileMediaQuality(
                            screen: screen,
                            audio: audio,
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
                                  summary: 'Could not start screen sharing',
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
                icon: const Icon(Icons.screen_share_rounded),
                label: Text(starting ? 'Starting…' : 'Choose what to share'),
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
        color: selected ? KaedeColors.coralSoft : KaedeColors.raised,
        shape: RoundedRectangleBorder(
          side: BorderSide(
            color: selected ? KaedeColors.coral : KaedeColors.border,
          ),
          borderRadius: BorderRadius.circular(12),
        ),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
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
                          style: const TextStyle(fontWeight: FontWeight.w700)),
                      Text(subtitle,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                              fontSize: 11, color: KaedeColors.muted)),
                    ],
                  ),
                ),
                if (selected)
                  const Icon(Icons.check_circle_rounded,
                      size: 18, color: KaedeColors.coralText),
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
                padding: const EdgeInsets.fromLTRB(20, 2, 20, 10),
                child: Text('Select audio output',
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
                    VoiceAudioRoute.phone => 'Phone',
                    VoiceAudioRoute.speaker => 'Speaker',
                    VoiceAudioRoute.bluetooth => 'Bluetooth headset',
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
          {bool selected = false, bool destructive = false}) =>
      Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          IconButton.filledTonal(
            tooltip: label,
            onPressed: tap == null ? null : () => _runVoiceAction(context, tap),
            style: IconButton.styleFrom(
              backgroundColor: destructive
                  ? KaedeColors.dangerSoft
                  : selected
                      ? KaedeColors.coralSoft
                      : KaedeColors.raised,
              foregroundColor: destructive
                  ? KaedeColors.danger
                  : selected
                      ? KaedeColors.coralText
                      : KaedeColors.text,
              disabledBackgroundColor: KaedeColors.panel,
              disabledForegroundColor: KaedeColors.muted,
            ),
            icon: Icon(icon, size: 21),
          ),
          const SizedBox(height: 3),
          Text(label,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                fontSize: 10,
                fontWeight: FontWeight.w600,
                color: destructive
                    ? KaedeColors.danger
                    : tap == null
                        ? KaedeColors.muted
                        : KaedeColors.textSoft,
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
          summary: 'Could not complete the voice action',
        )),
      ));
    }
  }
}

final class _ParticipantTile extends StatefulWidget {
  const _ParticipantTile({
    required this.participant,
    required this.knownName,
    required this.session,
    required this.channel,
    this.guild,
  });

  final Participant participant;
  final String? knownName;
  final VoiceSession session;
  final KaedeGuild? guild;
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
        .where((publication) => !publication.muted && publication.track != null)
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
        duration: const Duration(milliseconds: 120),
        clipBehavior: Clip.antiAlias,
        decoration: BoxDecoration(
          color: KaedeColors.raised,
          borderRadius: BorderRadius.circular(18),
          border: Border.all(
            color: widget.participant.isSpeaking
                ? KaedeColors.mint
                : KaedeColors.border,
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
                  backgroundColor: KaedeColors.coralDark,
                  child: Text(name.isEmpty ? '?' : name[0].toUpperCase(),
                      style: const TextStyle(
                          fontSize: 28, fontWeight: FontWeight.w800)),
                ),
              ),
            Align(
              alignment: Alignment.bottomLeft,
              child: Container(
                margin: const EdgeInsets.all(10),
                padding:
                    const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
                decoration: BoxDecoration(
                  color: Colors.black.withValues(alpha: .68),
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(name, style: const TextStyle(color: Colors.white)),
                    if (widget.participant is RemoteParticipant) ...[
                      const SizedBox(width: 6),
                      const Icon(Icons.more_horiz_rounded,
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
    var volume = widget.session.participantVolume(identity);
    if (!context.mounted) return;
    await showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      builder: (sheetContext) => StatefulBuilder(
        builder: (context, setModalState) => SafeArea(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(20, 4, 20, 20),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(name, style: Theme.of(context).textTheme.titleLarge),
                const SizedBox(height: 16),
                Text('User volume · ${(volume * 100).round()}%'),
                Slider(
                  value: volume,
                  onChanged: (value) {
                    setModalState(() => volume = value);
                    widget.session.setParticipantVolume(identity, value);
                  },
                ),
                if (guild != null && user != null) ...[
                  const Divider(),
                  if (widget.channel.allows(Permission.muteMembers))
                    ListTile(
                      leading: const Icon(Icons.mic_off_rounded),
                      title: Text(occupancy?['server_mute'] == true
                          ? 'Remove server mute'
                          : 'Server mute'),
                      onTap: () => _moderate(
                        sheetContext,
                        () => widget.session.setServerMute(
                          guild.ref,
                          user!,
                          occupancy?['server_mute'] != true,
                        ),
                      ),
                    ),
                  if (widget.channel.allows(Permission.deafenMembers))
                    ListTile(
                      leading: const Icon(Icons.headset_off_rounded),
                      title: Text(occupancy?['server_deaf'] == true
                          ? 'Remove server deafen'
                          : 'Server deafen'),
                      onTap: () => _moderate(
                        sheetContext,
                        () => widget.session.setServerDeaf(
                          guild.ref,
                          user!,
                          occupancy?['server_deaf'] != true,
                        ),
                      ),
                    ),
                  if (widget.channel.allows(Permission.moveMembers)) ...[
                    for (final target in guild.channels.where((candidate) =>
                        candidate.type == ChannelType.voice &&
                        candidate.ref != widget.channel.ref))
                      ListTile(
                        leading: const Icon(Icons.drive_file_move_rounded),
                        title: Text('Move to ${target.name ?? 'voice'}'),
                        onTap: () => _moderate(
                          sheetContext,
                          () => widget.session.moveParticipant(
                            guild.ref,
                            user!,
                            target.ref,
                          ),
                        ),
                      ),
                    ListTile(
                      leading: Icon(Icons.call_end_rounded,
                          color: Theme.of(context).colorScheme.error),
                      title: Text('Disconnect from voice',
                          style: TextStyle(
                              color: Theme.of(context).colorScheme.error)),
                      onTap: () => _moderate(
                        sheetContext,
                        () => widget.session
                            .disconnectParticipant(guild.ref, user!),
                      ),
                    ),
                  ],
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }

  Future<void> _moderate(
    BuildContext context,
    Future<void> Function() operation,
  ) async {
    Navigator.of(context).pop();
    try {
      await operation();
    } on Object catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(this.context).showSnackBar(
        SnackBar(
          content: Text(userFacingError(
            error,
            summary: 'Could not update the voice state',
          )),
        ),
      );
    }
  }
}

final class _VoiceEmpty extends StatelessWidget {
  const _VoiceEmpty({required this.canConnect});

  final bool canConnect;

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.spatial_audio_off_rounded,
                  color: KaedeColors.coral, size: 64),
              const SizedBox(height: 14),
              Text(canConnect ? 'Ready when you are' : 'Voice unavailable',
                  style: Theme.of(context).textTheme.headlineSmall),
              const SizedBox(height: 8),
              Text(
                canConnect
                    ? 'Join the room to see and hear everyone already here.'
                    : 'A guild role or channel permission is preventing access.',
                textAlign: TextAlign.center,
                style: const TextStyle(color: KaedeColors.muted),
              ),
            ],
          ),
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
        margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: Theme.of(context).colorScheme.errorContainer,
          borderRadius: BorderRadius.circular(14),
        ),
        child: Row(
          children: [
            Icon(icon),
            const SizedBox(width: 10),
            Expanded(child: Text(text)),
          ],
        ),
      );
}

extension _FirstOrNull<T> on Iterable<T> {
  T? get firstOrNull => isEmpty ? null : first;
}
