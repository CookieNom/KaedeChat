import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:audioplayers/audioplayers.dart';
import 'package:cryptography/cryptography.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart' as rtc;
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/stage_instances_repository.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/app/providers.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/e2ee/client.dart';
import 'package:kaede_mobile/src/features/voice/e2ee_policy.dart';
import 'package:kaede_mobile/src/features/voice/media_quality.dart';
import 'package:kaede_mobile/src/platform/voice_background_service.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:livekit_client/livekit_client.dart';
import 'package:permission_handler/permission_handler.dart' as permissions;

final voiceSessionProvider = ChangeNotifierProvider<VoiceSession>((ref) {
  final controller = ref.watch(mobileControllerProvider.notifier);
  final session = VoiceSession(
    ref.watch(repositoryProvider),
    controller.e2eeClient,
    voiceStatePublisher: ({required selfMute, required selfDeaf}) =>
        controller.gateway.setSelfVoiceState(
      selfMute: selfMute,
      selfDeaf: selfDeaf,
    ),
  );
  final soundboardSubscription = controller.soundboardEvents.listen(
    (event) => unawaited(session.playSoundboardEvent(event)),
  );
  ref.onDispose(() {
    unawaited(soundboardSubscription.cancel());
  });
  return session;
});

enum VoiceAudioRoute { phone, speaker, bluetooth }

enum VoiceConnectionPhase { idle, connecting, reconnecting, connected }

enum VoiceResumeAction {
  none,
  retryProtectedJoin,
  activateBackgroundService,
  restoreConnectedMedia,
  recoverDisconnectedRoom,
}

@visibleForTesting
bool validSoundboardMediaCapability({
  required Uri? download,
  required String authorityDomain,
  required String mediaOrigin,
}) {
  try {
    Domain(authorityDomain);
  } on FormatException {
    return false;
  }
  final origin = Uri.tryParse(mediaOrigin);
  if (download == null ||
      origin == null ||
      download.scheme != 'https' ||
      origin.scheme != 'https' ||
      origin.host.isEmpty ||
      (origin.path.isNotEmpty && origin.path != '/') ||
      origin.hasQuery ||
      origin.hasFragment ||
      origin.userInfo.isNotEmpty ||
      download.userInfo.isNotEmpty ||
      download.hasFragment) {
    return false;
  }
  return download.scheme == origin.scheme &&
      download.host == origin.host &&
      download.port == origin.port;
}

typedef MobileVoiceStatePublisher = Future<void> Function({
  required bool selfMute,
  required bool selfDeaf,
});

@visibleForTesting
bool voiceCanBeginProtectedJoin({
  required bool isAndroid,
  required bool appActive,
}) =>
    !isAndroid || appActive;

const _voiceBluetoothDeniedMessage =
    'Allow Nearby devices access to find and use Bluetooth headsets.';
const _voiceBluetoothSettingsMessage =
    'Bluetooth access is disabled. Enable Nearby devices for Kaede in system settings to use a headset.';

@visibleForTesting
VoiceResumeAction resolveVoiceResumeAction({
  required bool connecting,
  required bool hasRoom,
  required ConnectionState? connectionState,
  required bool recoverableDisconnect,
  bool pendingJoin = false,
}) {
  if (!connecting && !hasRoom && pendingJoin) {
    return VoiceResumeAction.retryProtectedJoin;
  }
  if (connecting || !hasRoom || connectionState == null) {
    return VoiceResumeAction.none;
  }
  return switch (connectionState) {
    ConnectionState.connected => VoiceResumeAction.restoreConnectedMedia,
    ConnectionState.reconnecting => VoiceResumeAction.activateBackgroundService,
    ConnectionState.disconnected when recoverableDisconnect =>
      VoiceResumeAction.recoverDisconnectedRoom,
    ConnectionState.connecting ||
    ConnectionState.disconnected =>
      VoiceResumeAction.none,
  };
}

@visibleForTesting
final class VoiceMediaRestoreResult {
  const VoiceMediaRestoreResult({
    required this.camera,
    required this.screen,
    required this.cameraFailed,
    required this.screenFailed,
  });

  final bool camera;
  final bool screen;
  final bool cameraFailed;
  final bool screenFailed;

  bool get failed => cameraFailed || screenFailed;
}

@visibleForTesting
Future<VoiceMediaRestoreResult> restoreVoiceMediaIntent({
  required bool canStream,
  required bool cameraRequested,
  required bool screenRequested,
  required Future<void> Function() publishCamera,
  required Future<void> Function() publishScreen,
}) async {
  if (!canStream) {
    return const VoiceMediaRestoreResult(
      camera: false,
      screen: false,
      cameraFailed: false,
      screenFailed: false,
    );
  }
  var camera = false;
  var screen = false;
  var cameraFailed = false;
  var screenFailed = false;
  if (cameraRequested) {
    try {
      await publishCamera();
      camera = true;
    } on Object {
      cameraFailed = true;
    }
  }
  if (screenRequested) {
    try {
      await publishScreen();
      screen = true;
    } on Object {
      screenFailed = true;
    }
  }
  return VoiceMediaRestoreResult(
    camera: camera,
    screen: screen,
    cameraFailed: cameraFailed,
    screenFailed: screenFailed,
  );
}

@visibleForTesting
Future<List<Object>> disposeTerminalVoiceResources({
  required Future<void> Function() stopBackgroundService,
  required Future<void> Function() disposeEvents,
  required Future<void> Function() disposeRoom,
}) async {
  final errors = <Object>[];
  for (final operation in <Future<void> Function()>[
    stopBackgroundService,
    disposeEvents,
    disposeRoom,
  ]) {
    try {
      await operation();
    } on Object catch (error) {
      errors.add(error);
    }
  }
  return List.unmodifiable(errors);
}

@visibleForTesting
bool voiceBluetoothPermissionsGranted(
  Iterable<permissions.PermissionStatus> statuses,
) =>
    statuses.every((status) => status.isGranted);

@visibleForTesting
String voiceBluetoothPermissionMessage(
  Iterable<permissions.PermissionStatus> statuses,
) {
  final blockedInSettings = statuses.any(
    (status) => status.isPermanentlyDenied || status.isRestricted,
  );
  return blockedInSettings
      ? _voiceBluetoothSettingsMessage
      : _voiceBluetoothDeniedMessage;
}

@visibleForTesting
bool voiceDisconnectIsRecoverable(DisconnectReason? reason) => switch (reason) {
      null ||
      DisconnectReason.unknown ||
      DisconnectReason.serverShutdown ||
      DisconnectReason.disconnected ||
      DisconnectReason.signalingConnectionFailure ||
      DisconnectReason.reconnectAttemptsExceeded =>
        true,
      DisconnectReason.clientInitiated ||
      DisconnectReason.duplicateIdentity ||
      DisconnectReason.participantRemoved ||
      DisconnectReason.roomDeleted ||
      DisconnectReason.stateMismatch ||
      DisconnectReason.joinFailure =>
        false,
    };

@visibleForTesting
VoiceConnectionPhase resolveVoiceConnectionPhase({
  required bool connecting,
  required bool hasRoom,
  required ConnectionState? connectionState,
  required bool recoverableDisconnect,
}) {
  if (connecting || connectionState == ConnectionState.connecting) {
    return VoiceConnectionPhase.connecting;
  }
  if (!hasRoom || connectionState == null) return VoiceConnectionPhase.idle;
  return switch (connectionState) {
    ConnectionState.connected => VoiceConnectionPhase.connected,
    ConnectionState.reconnecting => VoiceConnectionPhase.reconnecting,
    ConnectionState.disconnected => recoverableDisconnect
        ? VoiceConnectionPhase.reconnecting
        : VoiceConnectionPhase.idle,
    ConnectionState.connecting => VoiceConnectionPhase.connecting,
  };
}

KaedeChannel? findVoiceSessionChannel({
  required EntityRef target,
  required Iterable<KaedeChannel> directMessages,
  required Iterable<KaedeGuild> guilds,
}) {
  for (final channel in directMessages) {
    if (channel.ref == target) return channel;
  }
  for (final guild in guilds) {
    for (final channel in guild.channels) {
      if (channel.ref == target) return channel;
    }
  }
  return null;
}

String voiceDisconnectMessage(DisconnectReason? reason) => switch (reason) {
      DisconnectReason.duplicateIdentity =>
        'Voice moved to another device. This device will stay disconnected unless you explicitly move voice back here.',
      DisconnectReason.serverShutdown =>
        'The voice server restarted. Rejoin in a moment.',
      DisconnectReason.participantRemoved =>
        'This voice connection was ended from another device or by a moderator. It will not reconnect automatically.',
      DisconnectReason.roomDeleted => 'This voice room no longer exists.',
      DisconnectReason.stateMismatch =>
        'The voice session was out of date. Rejoin the channel.',
      DisconnectReason.joinFailure =>
        'Kaede could not join voice. Check your connection and permissions, then try again.',
      DisconnectReason.signalingConnectionFailure ||
      DisconnectReason.reconnectAttemptsExceeded =>
        'The connection to voice was lost. Check your connection and rejoin.',
      DisconnectReason.clientInitiated => 'You left voice.',
      _ => 'Voice disconnected. Check your connection and rejoin.',
    };

/// Owns mobile voice independently of the currently visible route.
///
/// The room must not belong to a channel widget: otherwise navigating to chat,
/// settings, or the app switcher tears down a healthy call. A monotonically
/// increasing generation fences late token and LiveKit completions when a user
/// switches rooms quickly.
final class VoiceSession extends ChangeNotifier {
  VoiceSession(
    this._repository,
    this._e2eeClient, {
    required MobileVoiceStatePublisher voiceStatePublisher,
    VoiceBackgroundService backgroundService = const VoiceBackgroundService(),
  })  : _voiceStatePublisher = voiceStatePublisher,
        _backgroundService = backgroundService {
    unawaited(_loadMediaQuality());
  }

  final KaedeRepository _repository;
  final Future<MobileE2EEClient> Function() _e2eeClient;
  final MobileVoiceStatePublisher _voiceStatePublisher;
  final VoiceBackgroundService _backgroundService;
  final AudioPlayer _soundboardPlayer = AudioPlayer();
  String? _connectionId;
  String? _activeElsewhereClient;
  static const _backgroundServiceError =
      'Voice is connected, but Android could not keep the call active in the background.';
  Room? _room;
  EventsListener<RoomEvent>? _events;
  KaedeChannel? _channel;
  EntityRef? _callRef;
  var _generation = 0;
  var _connecting = false;
  var _muted = false;
  var _deafened = false;
  var _camera = false;
  var _screen = false;
  var _mediaQuality = const MobileMediaQuality();
  var _voiceMediaPolicy = VoiceMediaPolicy.defaults;
  var _speaker = true;
  var _audioRoute = VoiceAudioRoute.speaker;
  var _pushToTalk = false;
  var _pushHeld = false;
  var _canSpeak = false;
  var _canStream = false;
  var _canUseVad = false;
  var _appActive = true;
  var _recoverableDisconnect = false;
  var _retryJoinOnResume = false;
  var _disposed = false;
  Timer? _occupancyTimer;
  final Map<String, Map<String, Object?>> _occupants =
      <String, Map<String, Object?>>{};
  final Map<String, double> _participantVolumes = <String, double>{};
  String? _error;

  KaedeChannel? get channel => _channel;
  EntityRef? get callRef => _callRef;
  Room? get room => _room;
  bool get connecting => _connecting;
  bool get connected => _room?.connectionState == ConnectionState.connected;
  VoiceConnectionPhase get phase => resolveVoiceConnectionPhase(
        connecting: _connecting,
        hasRoom: _room != null,
        connectionState: _room?.connectionState,
        recoverableDisconnect: _recoverableDisconnect,
      );
  bool get joined => phase == VoiceConnectionPhase.connected || reconnecting;
  bool get reconnecting => phase == VoiceConnectionPhase.reconnecting;
  bool get muted => _muted;
  bool get deafened => _deafened;
  bool get camera => _camera;
  bool get screen => _screen;
  MobileMediaQuality get mediaQuality => _mediaQuality;
  VoiceMediaPolicy get voiceMediaPolicy => _voiceMediaPolicy;
  bool get speaker => _speaker;
  VoiceAudioRoute get audioRoute => _audioRoute;
  bool get pushToTalk => _pushToTalk;
  bool get pushHeld => _pushHeld;
  bool get canSpeak => _canSpeak;
  bool get canStream => _canStream;
  bool get canUseVad => _canUseVad;
  bool get _microphoneShouldPublish =>
      _canSpeak && !_muted && (!_pushToTalk || _pushHeld);
  String? get error => _error;
  String? get activeElsewhereClient => _activeElsewhereClient;
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

  /// Plays an authorized server-dispatched sound only in the matching active
  /// voice room. Every connected client receives the same short, integrity-
  /// checked clip, so soundboard playback does not need to capture or mix the
  /// device microphone.
  Future<void> playSoundboardEvent(Map<String, Object?> data) async {
    final target = _channel;
    if (!joined || target == null || !target.type.isVoiceLike) return;
    final channelId = '${data['channel_id'] ?? ''}';
    final channelDomain = '${data['channel_domain'] ?? ''}';
    if (channelId != target.ref.id.value ||
        channelDomain != target.ref.domain.value) {
      return;
    }
    final rawSound = data['sound'];
    if (rawSound is! Map) return;
    final sound = Map<String, Object?>.from(rawSound);
    final expectedHash = '${sound['media_hash'] ?? ''}';
    if (!RegExp(r'^[0-9a-f]{64}$').hasMatch(expectedHash)) return;
    final uri = Uri.tryParse('${data['download_url'] ?? ''}');
    final mediaAuthority = '${data['media_authority'] ?? ''}'
        .trim()
        .toLowerCase()
        .replaceFirst(RegExp(r'\.$'), '');
    if (!validSoundboardMediaCapability(
      download: uri,
      authorityDomain: mediaAuthority,
      mediaOrigin: '${data['media_origin'] ?? ''}',
    )) {
      return;
    }
    final rawVolume = data['effective_volume'];
    final volume = rawVolume is num && rawVolume.isFinite
        ? rawVolume.toDouble().clamp(0, 1).toDouble()
        : 1.0;
    try {
      final bytes = await _downloadSoundboardBytes(uri!);
      final digest = await Sha256().hash(bytes);
      final actual = digest.bytes
          .map((value) => value.toRadixString(16).padLeft(2, '0'))
          .join();
      if (actual != expectedHash || !joined || _channel?.ref != target.ref) {
        return;
      }
      await _soundboardPlayer.stop();
      await _soundboardPlayer.play(
        BytesSource(Uint8List.fromList(bytes)),
        volume: volume,
      );
    } on Object {
      // A single expired or unavailable clip must never destabilize voice.
    }
  }

  Future<List<int>> _downloadSoundboardBytes(Uri uri) async {
    const maximum = 512 * 1024;
    final client = HttpClient()
      ..connectionTimeout = const Duration(seconds: 8)
      ..autoUncompress = false;
    try {
      final request =
          await client.getUrl(uri).timeout(const Duration(seconds: 8));
      request.followRedirects = false;
      final response =
          await request.close().timeout(const Duration(seconds: 8));
      if (response.isRedirect || response.statusCode != HttpStatus.ok) {
        throw const HttpException('Soundboard download was rejected.');
      }
      if (response.contentLength > maximum) {
        throw const HttpException('Soundboard response was too large.');
      }
      final bytes = <int>[];
      await for (final chunk in response.timeout(const Duration(seconds: 8))) {
        if (bytes.length + chunk.length > maximum) {
          throw const HttpException('Soundboard response was too large.');
        }
        bytes.addAll(chunk);
      }
      if (bytes.isEmpty) {
        throw const HttpException('Soundboard response was empty.');
      }
      return bytes;
    } finally {
      client.close(force: true);
    }
  }

  Future<void> connect(
    KaedeChannel target, {
    EntityRef? callRef,
    bool force = false,
    bool takeover = false,
  }) async {
    _mediaQuality = await MobileMediaQuality.load();
    if (!force &&
        (_connecting || connected) &&
        _channel?.ref == target.ref &&
        _callRef == callRef) {
      return;
    }
    final requestedCamera = _camera;
    final requestedScreen = _screen;
    final generation = ++_generation;
    if (_connectionId == null || takeover) {
      final random = Random.secure();
      _connectionId = base64UrlEncode(
        List<int>.generate(32, (_) => random.nextInt(256)),
      ).replaceAll('=', '');
    }
    await _disposeRoom(notify: false);
    if (generation != _generation) return;
    _channel = target;
    _callRef = callRef;
    _connecting = true;
    _error = null;
    _activeElsewhereClient = null;
    _recoverableDisconnect = false;
    _retryJoinOnResume = false;
    _canUseVad = callRef != null || target.allows(Permission.useVad);
    if (!_canUseVad) _pushToTalk = true;
    notifyListeners();

    Room? candidate;
    EventsListener<RoomEvent>? candidateEvents;
    var backgroundServiceStarted = false;
    var connectedSuccessfully = false;
    try {
      final isAndroid =
          !kIsWeb && defaultTargetPlatform == TargetPlatform.android;
      if (!voiceCanBeginProtectedJoin(
        isAndroid: isAndroid,
        appActive: _appActive,
      )) {
        _retryJoinOnResume = true;
        throw const KaedeException(
          code: 'VOICE_FOREGROUND_REQUIRED',
          message:
              'Return to Kaede to finish joining voice. Android will not start a call service from the background.',
          status: 409,
        );
      }
      if (isAndroid) {
        backgroundServiceStarted = await _backgroundService.setActive(true);
        if (generation != _generation) return;
        if (!backgroundServiceStarted) {
          _retryJoinOnResume = true;
          throw const KaedeException(
            code: 'VOICE_FOREGROUND_SERVICE_UNAVAILABLE',
            message: _backgroundServiceError,
            status: 503,
          );
        }
      }
      MobileE2EEClient? e2ee;
      if (target.encryptionMode == 'e2ee') {
        e2ee = await _e2eeClient();
      }
      final grant = callRef == null
          ? await _repository.voiceToken(
              target.ref,
              senderDeviceId: e2ee?.deviceId,
              connectionId: _connectionId!,
              takeover: takeover,
            )
          : await _repository.callVoiceToken(
              callRef,
              senderDeviceId: e2ee?.deviceId,
              connectionId: _connectionId!,
              takeover: takeover,
            );
      if (generation != _generation) return;
      final url = '${grant['url'] ?? ''}';
      final token = '${grant['token'] ?? ''}';
      if (url.isEmpty || token.isEmpty) {
        throw const KaedeException(
          code: 'VOICE_HOME_INVALID_RESPONSE',
          message:
              'The voice server returned an invalid connection. Try again or contact your instance operator.',
          status: 502,
        );
      }

      final grantEncryptionMode = grant['e2ee'];
      if (grantEncryptionMode is! bool) {
        throw const KaedeException(
          code: 'VOICE_E2EE_POLICY_MISMATCH',
          message:
              'The voice server omitted its encryption policy. Nothing was connected.',
          status: 409,
        );
      }
      final encryptedGrant = grantEncryptionMode;
      final encryptedChannel = target.encryptionMode == 'e2ee';
      final mediaPolicy = voiceMediaPolicyFromGrant(grant);
      if (encryptedGrant != encryptedChannel ||
          mediaPolicy == null ||
          !voiceGrantMatchesChannelPolicy(grant, target)) {
        throw const KaedeException(
          code: 'VOICE_E2EE_POLICY_MISMATCH',
          message:
              'The voice grant did not match this channel policy. Nothing was connected.',
          status: 409,
        );
      }
      E2EEOptions? e2eeOptions;
      if (encryptedGrant) {
        e2ee ??= await _e2eeClient();
        await e2ee.syncRoomState(target);
        final mediaKey = await e2ee.mediaKey(
          target,
          <String>[
            'kaede-livekit-key-v1',
            '${grant['media_protocol']}',
            '${grant['media_suite']}',
            '${grant['media_session_id']}',
            '${grant['media_epoch']}',
            '${grant['room']}',
          ].join('\u0000'),
        );
        try {
          final provider = await BaseKeyProvider.create(
            sharedKey: true,
            ratchetSalt: 'kaede-livekit-v1',
          );
          await provider.setRawKey(mediaKey);
          e2eeOptions = E2EEOptions(keyProvider: provider);
        } finally {
          mediaKey.fillRange(0, mediaKey.length, 0);
        }
      }
      if (generation != _generation) return;

      final room = candidate = Room(
        roomOptions: RoomOptions(
          e2eeOptions: e2eeOptions,
          defaultScreenShareCaptureOptions: _mediaQuality.screenCaptureOptions(
            useIosBroadcastExtension:
                !kIsWeb && defaultTargetPlatform == TargetPlatform.iOS,
          ),
          defaultCameraCaptureOptions:
              cameraCaptureOptionsForMode(mediaPolicy.videoQualityMode),
          defaultAudioPublishOptions:
              _mediaQuality.audioPublishOptionsForChannel(mediaPolicy.bitrate),
          defaultVideoPublishOptions: _mediaQuality
              .videoPublishOptionsForCameraMode(mediaPolicy.videoQualityMode),
          defaultAudioCaptureOptions: const AudioCaptureOptions(
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
            highPassFilter: true,
            voiceIsolation: true,
            typingNoiseDetection: true,
            stopAudioCaptureOnMute: false,
          ),
          adaptiveStream: true,
          dynacast: true,
        ),
      );
      final events = candidateEvents = room.createListener();
      events
        ..on<RoomReconnectingEvent>((_) {
          if (_room != room) return;
          _recoverableDisconnect = true;
          _error = 'Voice connection interrupted. Reconnecting…';
          notifyListeners();
        })
        ..on<RoomReconnectedEvent>((_) {
          if (_room != room) return;
          _recoverableDisconnect = false;
          _error = null;
          notifyListeners();
          if (_appActive) unawaited(_restoreForegroundMedia(room));
          unawaited(refreshOccupancy());
        })
        ..on<RoomDisconnectedEvent>((event) {
          if (_room != room) return;
          final recoverable = voiceDisconnectIsRecoverable(event.reason);
          if (!recoverable) {
            unawaited(_finishTerminalDisconnect(room, event.reason));
            return;
          }
          _recoverableDisconnect = true;
          _error = 'Voice connection interrupted. Reconnecting…';
          notifyListeners();
          if (_appActive) unawaited(_recoverDisconnectedRoom(room));
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
        ..on<TrackUnsubscribedEvent>((_) => _roomChanged(room))
        ..on<LocalTrackPublishedEvent>((event) {
          if (_room != room ||
              event.publication.source != TrackSource.screenShareVideo) {
            return;
          }
          _screen = true;
          notifyListeners();
        })
        ..on<LocalTrackUnpublishedEvent>((event) {
          if (_room != room ||
              event.publication.source != TrackSource.screenShareVideo) {
            return;
          }
          _screen = false;
          unawaited(_activateBackgroundService(room, screenShare: false));
          notifyListeners();
        });

      await room.connect(url, token);
      if (generation != _generation) return;
      _canSpeak = grant['can_speak'] == true &&
          (callRef != null || target.allows(Permission.speak));
      _canStream = grant['can_stream'] == true &&
          (callRef != null || target.allows(Permission.stream));
      await selectAudioRoute(_audioRoute);
      // Route selection may yield to a newer join/leave while this connected
      // room is still only a local candidate. Never publish that stale room;
      // the finally block below owns and disconnects it until this check has
      // passed.
      if (generation != _generation) return;
      _room = room;
      _events = events;
      _voiceMediaPolicy = mediaPolicy;
      candidate = null;
      candidateEvents = null;
      room.addListener(_notifyRoomChanged);
      if (callRef == null) {
        await refreshOccupancy();
        if (generation != _generation || _room != room) return;
        _occupancyTimer?.cancel();
        _occupancyTimer = Timer.periodic(
          const Duration(seconds: 10),
          (_) => unawaited(refreshOccupancy()),
        );
      }

      if (_canSpeak) {
        final microphone = await permissions.Permission.microphone.request();
        if (generation != _generation || _room != room) return;
        if (microphone.isGranted) {
          final protected = !isAndroid ||
              (_appActive && await _activateBackgroundService(room));
          if (generation != _generation || _room != room) return;
          if (protected) {
            await room.localParticipant?.setMicrophoneEnabled(
              _microphoneShouldPublish,
            );
            if (generation != _generation || _room != room) {
              await room.localParticipant?.setMicrophoneEnabled(false);
              return;
            }
          } else if (!_appActive) {
            _error =
                'Voice connected in listen-only mode while Kaede is in the background. Return to resume the microphone.';
          }
        } else {
          _muted = true;
          _error = 'Joined listen-only. Allow microphone access to speak.';
        }
      }
      if (!_canStream) {
        _camera = false;
        _screen = false;
      } else if (_appActive && (requestedCamera || requestedScreen)) {
        await _restorePublishedMedia(
          room,
          cameraRequested: requestedCamera,
          screenRequested: requestedScreen,
        );
      }
      connectedSuccessfully = generation == _generation && _room == room;
      if (connectedSuccessfully) _retryJoinOnResume = false;
    } on Object catch (exception) {
      if (generation == _generation) {
        if (exception is KaedeException &&
            exception.code == 'VOICE_ACTIVE_ELSEWHERE') {
          final kind = '${exception.details['active_client'] ?? ''}';
          _activeElsewhereClient = switch (kind) {
            'mobile' => 'your other phone or tablet',
            'desktop' => 'the desktop app',
            'web' => 'another browser',
            _ => 'another device',
          };
        }
        final error = _friendly(exception);
        if (_room != null) await _disposeRoom(notify: false);
        _canSpeak = false;
        _canStream = false;
        _recoverableDisconnect = false;
        _error = error;
      }
    } finally {
      if (candidate != null) {
        await candidate.disconnect();
        await candidateEvents?.dispose();
        await candidate.dispose();
      }
      if (generation == _generation) {
        if (!connectedSuccessfully && backgroundServiceStarted) {
          await _backgroundService.setActive(false);
        }
        _connecting = false;
        notifyListeners();
      }
    }
  }

  /// Applies a fresh effective-permission payload to an existing connection.
  Future<void> reconcilePermissions(KaedeChannel fresh) async {
    if (_callRef != null) return;
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
    final wasDeafened = _deafened;
    final room = _room;
    final publish = !next && (!_pushToTalk || _pushHeld);
    if (!next) {
      // Do not open capture until the authoritative session can publish the
      // unmute. Offline UI must fail closed for microphone privacy.
      await _voiceStatePublisher(selfMute: false, selfDeaf: false);
    }
    if (publish && room != null) {
      final protected = await _activateBackgroundService(
        room,
        microphone: true,
      );
      if (defaultTargetPlatform == TargetPlatform.android && !protected) {
        await _voiceStatePublisher(
          selfMute: true,
          selfDeaf: wasDeafened,
        ).catchError((_) {});
        return;
      }
    }
    try {
      await room?.localParticipant?.setMicrophoneEnabled(publish);
      if (!next && wasDeafened) await _setRemoteDeafened(false);
    } on Object {
      if (!next) {
        await _voiceStatePublisher(
          selfMute: true,
          selfDeaf: wasDeafened,
        ).catchError((_) {});
      }
      rethrow;
    }
    _muted = next;
    if (!next) _deafened = false;
    if (!publish && room != null && _appActive) {
      await _activateBackgroundService(room, microphone: false);
    }
    notifyListeners();
    if (next) {
      await _voiceStatePublisher(
        selfMute: true,
        selfDeaf: wasDeafened,
      );
    }
  }

  Future<void> toggleDeafen() async {
    final next = !_deafened;
    if (!next) {
      await _voiceStatePublisher(selfMute: true, selfDeaf: false);
    }
    try {
      await _setRemoteDeafened(next);
      if (next && !_muted) {
        await _room?.localParticipant?.setMicrophoneEnabled(false);
      }
    } on Object {
      if (!next) {
        await _voiceStatePublisher(selfMute: true, selfDeaf: true)
            .catchError((_) {});
      }
      rethrow;
    }
    if (next) _muted = true;
    _deafened = next;
    notifyListeners();
    if (next) {
      await _voiceStatePublisher(selfMute: true, selfDeaf: true);
    }
  }

  Future<void> _setRemoteDeafened(bool deafened) async {
    for (final participant
        in _room?.remoteParticipants.values ?? const <RemoteParticipant>[]) {
      for (final publication in participant.audioTrackPublications) {
        if (deafened) {
          await publication.disable();
        } else {
          await publication.enable();
        }
      }
    }
  }

  Future<void> toggleCamera() async {
    if (!_canStream) return;
    final next = !_camera;
    if (next && !(await permissions.Permission.camera.request()).isGranted) {
      _error = 'Camera access was not granted.';
      notifyListeners();
      return;
    }
    await _room?.localParticipant?.setCameraEnabled(
      next,
      cameraCaptureOptions:
          cameraCaptureOptionsForMode(_voiceMediaPolicy.videoQualityMode),
    );
    _camera = next;
    notifyListeners();
  }

  Future<void> startScreenShare(MobileMediaQuality quality) async {
    if (!_canStream) return;
    if (_screen) return;
    final room = _room;
    final participant = room?.localParticipant;
    if (room == null || participant == null) return;
    final previousQuality = _mediaQuality;
    final audioChanged = _mediaQuality.audio != quality.audio;
    _mediaQuality = quality;
    try {
      if (audioChanged) await _applyAudioQuality(participant);
      await quality.save();
      await quality.prepareIosBroadcastExtension();
    } on Object {
      _mediaQuality = previousQuality;
      if (audioChanged) {
        try {
          await _applyAudioQuality(participant);
        } on Object {
          // Preserve the original setup error. A transport reconnect will use
          // the restored last-publish options.
        }
      }
      try {
        await previousQuality.save();
      } on Object {
        // The active session rollback matters more than a best-effort local
        // preference write on a device with unavailable storage.
      }
      rethrow;
    }

    final isAndroid =
        !kIsWeb && defaultTargetPlatform == TargetPlatform.android;
    final isIos = !kIsWeb && defaultTargetPlatform == TargetPlatform.iOS;
    if (isAndroid) {
      final approved = await rtc.Helper.requestCapturePermission();
      if (!approved) {
        _error = 'Screen sharing was cancelled.';
        notifyListeners();
        return;
      }
      final protected = await _activateBackgroundService(
        room,
        screenShare: true,
      );
      if (!protected) return;
    }
    try {
      await participant.setScreenShareEnabled(
        true,
        captureScreenAudio: false,
        screenShareCaptureOptions: quality.screenCaptureOptions(
          useIosBroadcastExtension:
              !kIsWeb && defaultTargetPlatform == TargetPlatform.iOS,
        ),
      );
      // On iOS this call only presents ReplayKit's picker. The authoritative
      // state arrives asynchronously if the user actually starts broadcasting.
      _screen = isIos ? false : true;
    } on Object {
      if (isAndroid) {
        await _activateBackgroundService(room, screenShare: false);
      }
      rethrow;
    }
    notifyListeners();
  }

  Future<void> stopScreenShare() async {
    if (!_screen) return;
    final room = _room;
    if (!kIsWeb && defaultTargetPlatform == TargetPlatform.iOS) {
      await _mediaQuality.stopIosBroadcastExtension();
    }
    await room?.localParticipant?.setScreenShareEnabled(false);
    _screen = false;
    if (room != null) {
      await _activateBackgroundService(room, screenShare: false);
    }
    notifyListeners();
  }

  Future<void> toggleScreen() async {
    if (_screen) return stopScreenShare();
    return startScreenShare(_mediaQuality);
  }

  Future<void> _loadMediaQuality() async {
    final loaded = await MobileMediaQuality.load();
    if (_disposed) return;
    _mediaQuality = loaded;
    notifyListeners();
  }

  Future<void> _applyAudioQuality(LocalParticipant participant) async {
    final publication = participant.getTrackPublicationBySource(
      TrackSource.microphone,
    );
    final track = publication?.track;
    if (track is! LocalAudioTrack) return;
    final sender = track.transceiver?.sender;
    if (sender == null) return;
    final parameters = sender.parameters;
    final encodings = parameters.encodings;
    if (encodings == null || encodings.isEmpty) {
      throw StateError('The microphone encoder is not available.');
    }
    final previousBitrates =
        encodings.map((encoding) => encoding.maxBitrate).toList();
    for (final encoding in encodings) {
      encoding.maxBitrate = _mediaQuality
          .audioPublishOptionsForChannel(_voiceMediaPolicy.bitrate)
          .audioBitrate;
    }
    final applied = await sender.setParameters(parameters);
    if (!applied) {
      for (var index = 0; index < encodings.length; index += 1) {
        encodings[index].maxBitrate = previousBitrates[index];
      }
      await sender.setParameters(parameters);
      throw StateError('The microphone encoder rejected that bitrate.');
    }
    // LiveKit uses this on a future transport negotiation/reconnect, at which
    // point Studio's DTX preference is applied as well as the bitrate ceiling.
    track.lastPublishOptions =
        _mediaQuality.audioPublishOptionsForChannel(_voiceMediaPolicy.bitrate);
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
    if (!(await _ensureBluetoothPermission())) return routes;
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
      if (defaultTargetPlatform == TargetPlatform.android) {
        _error =
            'Bluetooth devices could not be checked. Reconnect the headset and try again.';
        notifyListeners();
      }
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
        if (!(await _ensureBluetoothPermission())) {
          await Hardware.instance
              .setSpeakerphoneOn(true, forceSpeakerOutput: true);
          _speaker = true;
          _audioRoute = VoiceAudioRoute.speaker;
          notifyListeners();
          return;
        }
        await rtc.Helper.setSpeakerphoneOnButPreferBluetooth();
        _speaker = false;
        break;
    }
    _audioRoute = route;
    notifyListeners();
  }

  Future<bool> _ensureBluetoothPermission() async {
    if (kIsWeb || defaultTargetPlatform != TargetPlatform.android) return true;
    final results = await <permissions.Permission>[
      permissions.Permission.bluetoothConnect,
    ].request();
    final statuses = results.values;
    if (voiceBluetoothPermissionsGranted(statuses)) {
      if (_error == _voiceBluetoothDeniedMessage ||
          _error == _voiceBluetoothSettingsMessage) {
        _error = null;
      }
      return true;
    }
    _error = voiceBluetoothPermissionMessage(statuses);
    notifyListeners();
    return false;
  }

  Future<void> refreshOccupancy() async {
    final target = _channel;
    if (target == null || !connected || _callRef != null) return;
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

  Future<void> requestToSpeak(EntityRef guild,
      {required bool requested}) async {
    await _repository.updateMyStageVoiceState(
      guild,
      <String, Object?>{
        'request_to_speak_timestamp':
            requested ? DateTime.now().toUtc().toIso8601String() : null,
      },
    );
    await refreshOccupancy();
  }

  Future<void> moveSelfToStageAudience(EntityRef guild) async {
    await _repository.updateMyStageVoiceState(
      guild,
      const <String, Object?>{'suppress': true},
    );
    await refreshOccupancy();
  }

  Future<void> setStageParticipantSuppressed(
    EntityRef guild,
    EntityRef user,
    bool suppressed,
  ) async {
    await _repository.updateStageVoiceState(
      guild,
      user,
      suppress: suppressed,
    );
    await refreshOccupancy();
  }

  Future<void> toggleInputMode() async {
    if (!_canSpeak || (!_pushToTalk && !_canUseVad)) return;
    final next = !_pushToTalk;
    if (next) {
      await _room?.localParticipant?.setMicrophoneEnabled(false);
    } else if (!_muted) {
      final room = _room;
      if (room != null) {
        final protected = await _activateBackgroundService(
          room,
          microphone: true,
        );
        if (defaultTargetPlatform == TargetPlatform.android && !protected) {
          return;
        }
      }
      await _room?.localParticipant?.setMicrophoneEnabled(true);
    }
    _pushToTalk = next;
    _pushHeld = false;
    final activeRoom = _room;
    if (next && _appActive && activeRoom != null) {
      await _activateBackgroundService(activeRoom, microphone: false);
    }
    notifyListeners();
  }

  Future<void> setPushHeld(bool held) async {
    if (!_pushToTalk || _muted || held == _pushHeld) return;
    final room = _room;
    if (held && room != null) {
      final protected = await _activateBackgroundService(
        room,
        microphone: true,
      );
      if (defaultTargetPlatform == TargetPlatform.android && !protected) return;
    }
    _pushHeld = held;
    await room?.localParticipant?.setMicrophoneEnabled(held);
    if (!held && room != null && _appActive) {
      await _activateBackgroundService(room, microphone: false);
    }
    notifyListeners();
  }

  /// Records that the UI moved into the background without changing the
  /// desired room membership. LiveKit and the Android foreground service keep
  /// audio alive; transient reconnecting states continue to render as joined.
  void didEnterBackground() {
    _appActive = false;
  }

  /// Reconciles native audio and, if the operating system exhausted LiveKit's
  /// reconnect attempts while suspended, obtains a fresh short-lived grant.
  Future<void> didResume() async {
    _appActive = true;
    final room = _room;
    final action = resolveVoiceResumeAction(
      connecting: _connecting,
      hasRoom: room != null && _channel != null,
      connectionState: room?.connectionState,
      recoverableDisconnect: _recoverableDisconnect,
      pendingJoin: _retryJoinOnResume,
    );
    switch (action) {
      case VoiceResumeAction.none:
        break;
      case VoiceResumeAction.retryProtectedJoin:
        final target = _channel;
        final call = _callRef;
        _retryJoinOnResume = false;
        if (target != null) {
          await connect(target, callRef: call, force: true);
          return;
        }
        break;
      case VoiceResumeAction.activateBackgroundService:
        await _activateBackgroundService(room!);
        break;
      case VoiceResumeAction.restoreConnectedMedia:
        await _restoreForegroundMedia(room!);
        unawaited(refreshOccupancy());
        break;
      case VoiceResumeAction.recoverDisconnectedRoom:
        await _recoverDisconnectedRoom(room!);
        return;
    }
    notifyListeners();
  }

  Future<void> _recoverDisconnectedRoom(Room room) async {
    final target = _channel;
    final call = _callRef;
    if (!_appActive ||
        target == null ||
        _room != room ||
        room.connectionState != ConnectionState.disconnected ||
        !_recoverableDisconnect ||
        _connecting) {
      return;
    }
    await connect(target, callRef: call, force: true);
  }

  Future<void> _restoreForegroundMedia(Room room) async {
    if (_room != room || room.connectionState != ConnectionState.connected) {
      return;
    }
    final generation = _generation;
    bool isCurrent() => generation == _generation && _room == room;
    final protected = await _activateBackgroundService(room);
    if (!isCurrent() ||
        (defaultTargetPlatform == TargetPlatform.android && !protected)) {
      return;
    }
    var deviceRestoreFailed = false;
    try {
      await selectAudioRoute(_audioRoute);
    } on Object {
      deviceRestoreFailed = true;
    }
    if (!isCurrent() || room.connectionState != ConnectionState.connected) {
      return;
    }
    if (_canSpeak) {
      try {
        await room.localParticipant?.setMicrophoneEnabled(
          _microphoneShouldPublish,
        );
      } on Object {
        deviceRestoreFailed = true;
      }
    }
    if (!isCurrent()) return;
    final media = await _restorePublishedMedia(
      room,
      cameraRequested: _camera,
      screenRequested: _screen,
    );
    if (!isCurrent()) return;
    if (deviceRestoreFailed && !media.failed) {
      _error =
          'Voice reconnected, but a media device could not be restored. Check the microphone and audio route.';
      notifyListeners();
    }
  }

  Future<VoiceMediaRestoreResult> _restorePublishedMedia(
    Room room, {
    required bool cameraRequested,
    required bool screenRequested,
  }) async {
    final generation = _generation;
    final participant = room.localParticipant;
    final result = await restoreVoiceMediaIntent(
      canStream: _canStream,
      cameraRequested: cameraRequested,
      screenRequested: screenRequested,
      publishCamera: () async {
        if (participant == null) {
          throw StateError('The local voice participant is unavailable.');
        }
        await participant.setCameraEnabled(
          true,
          cameraCaptureOptions:
              cameraCaptureOptionsForMode(_voiceMediaPolicy.videoQualityMode),
        );
      },
      publishScreen: () async {
        if (participant == null) {
          throw StateError('The local voice participant is unavailable.');
        }
        await participant.setScreenShareEnabled(
          true,
          captureScreenAudio: false,
          screenShareCaptureOptions: _mediaQuality.screenCaptureOptions(
            useIosBroadcastExtension:
                !kIsWeb && defaultTargetPlatform == TargetPlatform.iOS,
          ),
        );
      },
    );
    if (generation != _generation || _room != room) return result;
    _camera = result.camera;
    _screen = result.screen;
    if (result.failed) {
      final failed = <String>[
        if (result.cameraFailed) 'camera',
        if (result.screenFailed) 'screen sharing',
      ].join(' and ');
      _error =
          'Voice reconnected, but $failed could not be restored. Turn it on again to retry.';
      notifyListeners();
    }
    return result;
  }

  Future<bool> _activateBackgroundService(
    Room room, {
    bool? microphone,
    bool? screenShare,
  }) async {
    if (!_appActive || _room != room) return false;
    final generation = _generation;
    final ready = await _backgroundService.setActive(
      true,
      microphone: microphone ?? _microphoneShouldPublish,
      screenShare: screenShare ?? _screen,
    );
    if (generation != _generation || _room != room) return false;
    if (!ready && defaultTargetPlatform == TargetPlatform.android) {
      _error = _backgroundServiceError;
      notifyListeners();
      return false;
    }
    if (ready && _error == _backgroundServiceError) {
      _error = null;
      notifyListeners();
    }
    return ready;
  }

  Future<void> _finishTerminalDisconnect(
    Room room,
    DisconnectReason? reason,
  ) async {
    if (_room != room) return;
    _generation += 1;
    final events = _events;
    _room = null;
    _events = null;
    room.removeListener(_notifyRoomChanged);
    _occupancyTimer?.cancel();
    _occupancyTimer = null;
    _occupants.clear();
    _participantVolumes.clear();
    _channel = null;
    _callRef = null;
    _connecting = false;
    _muted = false;
    _deafened = false;
    _camera = false;
    _screen = false;
    _pushHeld = false;
    _canSpeak = false;
    _canStream = false;
    _canUseVad = false;
    _voiceMediaPolicy = VoiceMediaPolicy.defaults;
    _recoverableDisconnect = false;
    _retryJoinOnResume = false;
    _activeElsewhereClient = null;
    _connectionId = null;
    _error = voiceDisconnectMessage(reason);
    _notify();
    await disposeTerminalVoiceResources(
      stopBackgroundService: () async {
        await _backgroundService.setActive(false);
      },
      disposeEvents: () async {
        await events?.dispose();
      },
      // The room has already emitted its terminal disconnect. Calling
      // disconnect() here can recursively emit the same event; dispose the
      // detached owner directly instead.
      disposeRoom: room.dispose,
    );
  }

  Future<void> leave({String? reason}) async {
    _generation += 1;
    await _disposeRoom(notify: false);
    _channel = null;
    _callRef = null;
    _connecting = false;
    _muted = false;
    _deafened = false;
    _camera = false;
    _screen = false;
    _pushHeld = false;
    _canSpeak = false;
    _canStream = false;
    _voiceMediaPolicy = VoiceMediaPolicy.defaults;
    _recoverableDisconnect = false;
    _retryJoinOnResume = false;
    _activeElsewhereClient = null;
    _connectionId = null;
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
    await _soundboardPlayer.stop();
    _occupancyTimer?.cancel();
    _occupancyTimer = null;
    _occupants.clear();
    final room = _room;
    final events = _events;
    _room = null;
    _events = null;
    _voiceMediaPolicy = VoiceMediaPolicy.defaults;
    room?.removeListener(_notifyRoomChanged);
    if (!kIsWeb && defaultTargetPlatform == TargetPlatform.iOS && _screen) {
      await _mediaQuality.stopIosBroadcastExtension();
    }
    await _backgroundService.setActive(false);
    await room?.localParticipant?.setMicrophoneEnabled(false);
    await room?.disconnect();
    await events?.dispose();
    await room?.dispose();
    if (notify) notifyListeners();
  }

  String _friendly(Object exception) {
    if (exception is KaedeException) return userFacingError(exception);
    final text = '$exception';
    if (text.contains('403') || text.toLowerCase().contains('forbidden')) {
      return 'You do not have permission to join this voice channel.';
    }
    return 'Kaede could not connect to voice. Check your connection and microphone permissions, then try again.';
  }

  @override
  void dispose() {
    _disposed = true;
    _occupancyTimer?.cancel();
    _generation += 1;
    unawaited(_disposeRoom(notify: false));
    unawaited(_soundboardPlayer.dispose());
    super.dispose();
  }
}
