import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart' as rtc;
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/app/providers.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:livekit_client/livekit_client.dart';
import 'package:permission_handler/permission_handler.dart' as permissions;

final voiceSessionProvider = ChangeNotifierProvider<VoiceSession>((ref) {
  final session = VoiceSession(ref.watch(repositoryProvider));
  ref.onDispose(session.dispose);
  return session;
});

enum VoiceAudioRoute { phone, speaker, bluetooth }

/// Owns mobile voice independently of the currently visible route.
///
/// The room must not belong to a channel widget: otherwise navigating to chat,
/// settings, or the app switcher tears down a healthy call. A monotonically
/// increasing generation fences late token and LiveKit completions when a user
/// switches rooms quickly.
final class VoiceSession extends ChangeNotifier {
  VoiceSession(this._repository);

  final KaedeRepository _repository;
  Room? _room;
  EventsListener<RoomEvent>? _events;
  KaedeChannel? _channel;
  var _generation = 0;
  var _connecting = false;
  var _muted = false;
  var _deafened = false;
  var _camera = false;
  var _screen = false;
  var _speaker = true;
  var _audioRoute = VoiceAudioRoute.speaker;
  var _pushToTalk = false;
  var _pushHeld = false;
  var _canSpeak = false;
  var _canStream = false;
  var _canUseVad = false;
  var _disposed = false;
  Timer? _occupancyTimer;
  final Map<String, Map<String, Object?>> _occupants =
      <String, Map<String, Object?>>{};
  final Map<String, double> _participantVolumes = <String, double>{};
  String? _error;

  KaedeChannel? get channel => _channel;
  Room? get room => _room;
  bool get connecting => _connecting;
  bool get connected => _room?.connectionState == ConnectionState.connected;
  bool get muted => _muted;
  bool get deafened => _deafened;
  bool get camera => _camera;
  bool get screen => _screen;
  bool get speaker => _speaker;
  VoiceAudioRoute get audioRoute => _audioRoute;
  bool get pushToTalk => _pushToTalk;
  bool get pushHeld => _pushHeld;
  bool get canSpeak => _canSpeak;
  bool get canStream => _canStream;
  bool get canUseVad => _canUseVad;
  String? get error => _error;
  Map<String, Object?>? occupant(String identity) => _occupants[identity];
  double participantVolume(String identity) =>
      _participantVolumes[identity] ?? 1;

  List<Participant> get participants {
    final current = _room;
    if (current == null) return const <Participant>[];
    return <Participant>[
      if (current.localParticipant case final participant?) participant,
      ...current.remoteParticipants.values,
    ];
  }

  Future<void> connect(KaedeChannel target, {bool force = false}) async {
    if (!force && (_connecting || connected) && _channel?.ref == target.ref) {
      return;
    }
    final generation = ++_generation;
    await _disposeRoom(notify: false);
    if (generation != _generation) return;
    _channel = target;
    _connecting = true;
    _error = null;
    _canUseVad = target.allows(Permission.useVad);
    if (!_canUseVad) _pushToTalk = true;
    notifyListeners();

    Room? candidate;
    EventsListener<RoomEvent>? candidateEvents;
    try {
      final grant = await _repository.voiceToken(target.ref);
      if (generation != _generation) return;
      final url = '${grant['url'] ?? ''}';
      final token = '${grant['token'] ?? ''}';
      if (url.isEmpty || token.isEmpty) {
        throw StateError('The voice server returned an invalid connection.');
      }

      final room = candidate = Room(
        roomOptions: const RoomOptions(
          defaultAudioCaptureOptions: AudioCaptureOptions(
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
            highPassFilter: true,
            voiceIsolation: true,
            typingNoiseDetection: true,
            stopAudioCaptureOnMute: false,
          ),
        ),
      );
      final events = candidateEvents = room.createListener();
      events
        ..on<RoomDisconnectedEvent>((event) {
          if (_room != room) return;
          _error = event.reason?.toString() ?? 'Voice disconnected.';
          notifyListeners();
        })
        ..on<ParticipantConnectedEvent>((_) {
          _roomChanged(room);
          unawaited(refreshOccupancy());
        })
        ..on<ParticipantDisconnectedEvent>((_) {
          _roomChanged(room);
          unawaited(refreshOccupancy());
        })
        ..on<ActiveSpeakersChangedEvent>((_) => _roomChanged(room))
        ..on<TrackSubscribedEvent>((event) {
          if (_room != room) return;
          if (_deafened) unawaited(event.publication.disable());
          if (event.track is RemoteAudioTrack) {
            unawaited(_applyVolume(
              event.participant.identity,
              event.track as RemoteAudioTrack,
            ));
          }
          notifyListeners();
        })
        ..on<TrackUnsubscribedEvent>((_) => _roomChanged(room));

      await room.connect(url, token);
      if (generation != _generation) return;
      _canSpeak = grant['can_speak'] == true && target.allows(Permission.speak);
      _canStream =
          grant['can_stream'] == true && target.allows(Permission.stream);
      await selectAudioRoute(_audioRoute);
      _room = room;
      _events = events;
      candidate = null;
      candidateEvents = null;
      room.addListener(_notifyRoomChanged);
      await refreshOccupancy();
      _occupancyTimer?.cancel();
      _occupancyTimer = Timer.periodic(
        const Duration(seconds: 10),
        (_) => unawaited(refreshOccupancy()),
      );

      if (_canSpeak) {
        final microphone = await permissions.Permission.microphone.request();
        if (generation != _generation) return;
        if (microphone.isGranted) {
          await room.localParticipant?.setMicrophoneEnabled(
            !_muted && (!_pushToTalk || _pushHeld),
          );
        } else {
          _muted = true;
          _error = 'Joined listen-only. Allow microphone access to speak.';
        }
      }
    } on Object catch (exception) {
      if (generation == _generation) _error = _friendly(exception);
    } finally {
      if (candidate != null) {
        await candidate.disconnect();
        await candidateEvents?.dispose();
        await candidate.dispose();
      }
      if (generation == _generation) {
        _connecting = false;
        notifyListeners();
      }
    }
  }

  /// Applies a fresh effective-permission payload to an existing connection.
  Future<void> reconcilePermissions(KaedeChannel fresh) async {
    if (_channel?.ref != fresh.ref) return;
    if (_channel?.permissions == fresh.permissions) return;
    _channel = fresh;
    if (!fresh.allows(Permission.connect)) {
      await leave(
        reason: 'You no longer have permission to join this voice channel.',
      );
      return;
    }
    final speak = fresh.allows(Permission.speak);
    final stream = fresh.allows(Permission.stream);
    final gainedServerGrant = (speak && !_canSpeak) || (stream && !_canStream);
    _canUseVad = fresh.allows(Permission.useVad);
    if (!speak && _canSpeak) {
      _canSpeak = false;
      _muted = true;
      await _room?.localParticipant?.setMicrophoneEnabled(false);
    }
    if (!stream && _canStream) {
      _canStream = false;
      _camera = false;
      _screen = false;
      await _room?.localParticipant?.setCameraEnabled(false);
      await _room?.localParticipant?.setScreenShareEnabled(false);
    }
    if (!_canUseVad && !_pushToTalk) {
      _pushToTalk = true;
      _pushHeld = false;
      await _room?.localParticipant?.setMicrophoneEnabled(false);
    }
    // LiveKit publication grants are embedded in the short-lived room token.
    // Merely changing the local booleans after a permission grant leaves the
    // participant connected with the old, more restrictive token. Reconnect
    // through the home instance so the authoritative grant is refreshed.
    if (gainedServerGrant && connected) {
      await connect(fresh, force: true);
      return;
    }
    notifyListeners();
  }

  Future<void> toggleMute() async {
    if (!_canSpeak) return;
    final next = !_muted;
    await _room?.localParticipant
        ?.setMicrophoneEnabled(!next && (!_pushToTalk || _pushHeld));
    _muted = next;
    notifyListeners();
  }

  Future<void> toggleDeafen() async {
    final next = !_deafened;
    for (final participant
        in _room?.remoteParticipants.values ?? const <RemoteParticipant>[]) {
      for (final publication in participant.audioTrackPublications) {
        if (next) {
          await publication.disable();
        } else {
          await publication.enable();
        }
      }
    }
    if (next && !_muted) await toggleMute();
    _deafened = next;
    notifyListeners();
  }

  Future<void> toggleCamera() async {
    if (!_canStream) return;
    final next = !_camera;
    if (next && !(await permissions.Permission.camera.request()).isGranted) {
      _error = 'Camera access was not granted.';
      notifyListeners();
      return;
    }
    await _room?.localParticipant?.setCameraEnabled(next);
    _camera = next;
    notifyListeners();
  }

  Future<void> toggleScreen() async {
    if (!_canStream) return;
    final next = !_screen;
    await _room?.localParticipant?.setScreenShareEnabled(next);
    _screen = next;
    notifyListeners();
  }

  Future<void> toggleSpeaker() async {
    final next = !_speaker;
    await Hardware.instance.setSpeakerphoneOn(next);
    _speaker = next;
    _audioRoute = next ? VoiceAudioRoute.speaker : VoiceAudioRoute.phone;
    notifyListeners();
  }

  Future<List<VoiceAudioRoute>> availableAudioRoutes() async {
    final routes = <VoiceAudioRoute>[
      VoiceAudioRoute.phone,
      VoiceAudioRoute.speaker,
    ];
    try {
      final devices = await rtc.navigator.mediaDevices.enumerateDevices();
      final hasBluetooth = devices.any((device) {
        final label = device.label.toLowerCase();
        return (device.kind == 'audiooutput' || device.kind == 'audioinput') &&
            (label.contains('bluetooth') ||
                label.contains('headset') ||
                label.contains('airpod') ||
                label.contains('buds') ||
                label.contains('wh-') ||
                label.contains('wf-'));
      });
      if (hasBluetooth) routes.add(VoiceAudioRoute.bluetooth);
    } on Object {
      // Phone and speaker routes remain usable when enumeration is restricted.
    }
    return routes;
  }

  Future<void> selectAudioRoute(VoiceAudioRoute route) async {
    switch (route) {
      case VoiceAudioRoute.phone:
        await Hardware.instance.setSpeakerphoneOn(false);
        _speaker = false;
        break;
      case VoiceAudioRoute.speaker:
        await Hardware.instance
            .setSpeakerphoneOn(true, forceSpeakerOutput: true);
        _speaker = true;
        break;
      case VoiceAudioRoute.bluetooth:
        await rtc.Helper.setSpeakerphoneOnButPreferBluetooth();
        _speaker = false;
        break;
    }
    _audioRoute = route;
    notifyListeners();
  }

  Future<void> refreshOccupancy() async {
    final target = _channel;
    if (target == null || !connected) return;
    try {
      final snapshot = await _repository.voiceOccupancy(target.ref);
      if (_channel?.ref != target.ref || !connected) return;
      final next = <String, Map<String, Object?>>{};
      for (final raw
          in snapshot['participants'] as List? ?? const <Object?>[]) {
        if (raw is! Map) continue;
        final item = raw.cast<String, Object?>();
        final identity = '${item['identity'] ?? ''}';
        if (identity.isNotEmpty) next[identity] = item;
      }
      _occupants
        ..clear()
        ..addAll(next);
      notifyListeners();
    } on Object {
      // Occupancy is best-effort and must never tear down healthy audio.
    }
  }

  Future<void> setParticipantVolume(String identity, double volume) async {
    final bounded = volume.clamp(0, 1).toDouble();
    _participantVolumes[identity] = bounded;
    final participant = _room?.remoteParticipants[identity];
    if (participant != null) {
      for (final publication in participant.audioTrackPublications) {
        if (publication.track case final RemoteAudioTrack track) {
          await _applyVolume(identity, track);
        }
      }
    }
    notifyListeners();
  }

  Future<void> _applyVolume(
    String identity,
    RemoteAudioTrack track,
  ) =>
      rtc.Helper.setVolume(participantVolume(identity), track.mediaStreamTrack);

  Future<void> setServerMute(
    EntityRef guild,
    EntityRef user,
    bool muted,
  ) async {
    await _repository.updateMemberVoice(guild, user, serverMute: muted);
    await refreshOccupancy();
  }

  Future<void> setServerDeaf(
    EntityRef guild,
    EntityRef user,
    bool deafened,
  ) async {
    await _repository.updateMemberVoice(guild, user, serverDeaf: deafened);
    await refreshOccupancy();
  }

  Future<void> disconnectParticipant(EntityRef guild, EntityRef user) async {
    await _repository.disconnectMemberVoice(guild, user);
    await refreshOccupancy();
  }

  Future<void> moveParticipant(
    EntityRef guild,
    EntityRef user,
    EntityRef target,
  ) async {
    await _repository.moveMemberVoice(guild, user, target);
    await refreshOccupancy();
  }

  Future<void> toggleInputMode() async {
    if (!_canSpeak || (!_pushToTalk && !_canUseVad)) return;
    final next = !_pushToTalk;
    if (next) {
      await _room?.localParticipant?.setMicrophoneEnabled(false);
    } else if (!_muted) {
      await _room?.localParticipant?.setMicrophoneEnabled(true);
    }
    _pushToTalk = next;
    _pushHeld = false;
    notifyListeners();
  }

  Future<void> setPushHeld(bool held) async {
    if (!_pushToTalk || _muted || held == _pushHeld) return;
    _pushHeld = held;
    await _room?.localParticipant?.setMicrophoneEnabled(held);
    notifyListeners();
  }

  Future<void> leave({String? reason}) async {
    _generation += 1;
    await _disposeRoom(notify: false);
    _channel = null;
    _connecting = false;
    _muted = false;
    _deafened = false;
    _camera = false;
    _screen = false;
    _pushHeld = false;
    _canSpeak = false;
    _canStream = false;
    _error = reason;
    notifyListeners();
  }

  void _roomChanged(Room room) {
    if (_room == room) _notify();
  }

  void _notifyRoomChanged() => _notify();

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  Future<void> _disposeRoom({required bool notify}) async {
    _occupancyTimer?.cancel();
    _occupancyTimer = null;
    _occupants.clear();
    final room = _room;
    final events = _events;
    _room = null;
    _events = null;
    room?.removeListener(_notifyRoomChanged);
    await room?.localParticipant?.setMicrophoneEnabled(false);
    await room?.disconnect();
    await events?.dispose();
    await room?.dispose();
    if (notify) notifyListeners();
  }

  String _friendly(Object exception) {
    final text = '$exception';
    if (text.contains('403') || text.toLowerCase().contains('forbidden')) {
      return 'You do not have permission to join this voice channel.';
    }
    return text.replaceFirst('Bad state: ', '');
  }

  @override
  void dispose() {
    _disposed = true;
    _occupancyTimer?.cancel();
    _generation += 1;
    unawaited(_disposeRoom(notify: false));
    super.dispose();
  }
}
