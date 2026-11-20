import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:connectivity_plus/connectivity_plus.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart' as rtc;
import 'package:livekit_client/livekit_client.dart';

import 'media_quality.dart';

/// Wire names and roles are shared with the browser and desktop controllers.
const videoCodecPolicy = {
  'vp8': (premium: false, legacy: true, scalable: false),
  'h264': (premium: false, legacy: true, scalable: false),
  'av1': (premium: true, legacy: true, scalable: true),
  'h265': (premium: true, legacy: false, scalable: false),
  'vp9': (premium: false, legacy: false, scalable: true),
};

bool isPremiumVideoCodec(String? codec) =>
    videoCodecPolicy[codec]?.premium ?? false;

const encryptedH264Version = 2;

bool sharesEncryptedH264Format(Iterable<Object?> versions) =>
    versions.isNotEmpty &&
    versions
        .every((version) => version is int && version == encryptedH264Version);

String remoteVideoCodec(String mimeType, String? namedCodec,
    {required bool encrypted}) {
  final actual = mimeType.split(';').first.trim().toLowerCase().split('/').last;
  // The SFU can substitute H.264 after a publisher names its track.
  if (encrypted &&
      (actual == 'h264' ||
          namedCodec == 'h264' && videoCodecPolicy.containsKey(actual))) {
    return actual;
  }
  return namedCodec ?? actual;
}

String encodeVideoHints(Set<String> codecs, Map<String, String> demand,
    Map<String, String> health) {
  final hints = <String, Object>{
    'v': 1,
    'encryptedH264': encryptedH264Version,
    'codecs': codecs.where((codec) => videoCodecPolicy[codec]!.legacy).toList(),
    'extendedCodecs':
        codecs.where((codec) => !videoCodecPolicy[codec]!.legacy).toList(),
    'demand': Map.fromEntries(demand.entries
        .where((entry) =>
            codecs.contains(entry.value) &&
            videoCodecPolicy[entry.value]!.legacy &&
            utf8.encode(entry.key).length <= 256)
        .take(64)),
    'health': health,
  };
  final encoded = jsonEncode(hints);
  // Large rooms must still receive capability changes within the v1 wire limit.
  if (utf8.encode(encoded).length <= 8192) return encoded;
  return jsonEncode(
      {...hints, 'demand': <String, String>{}, 'health': <String, String>{}});
}

/// Preserve the v1 field for old parsers; newer codecs travel only in its extension.
Set<String>? parseVideoCodecs(Map<String, dynamic> hints) {
  final legacy = hints['codecs'];
  if (hints['v'] != 1 ||
      legacy is! List ||
      legacy.length > 3 ||
      legacy.any(
        (codec) => codec is! String || videoCodecPolicy[codec]?.legacy != true,
      )) {
    return null;
  }
  final extension = hints['extendedCodecs'];
  if (extension != null &&
      (extension is! List ||
          extension.length > 8 ||
          extension.any((codec) => codec is! String || codec.length > 16))) {
    return null;
  }
  final demand = hints['demand'];
  final health = hints['health'];
  if (demand != null &&
      (demand is! Map ||
          demand.entries.any(
            (entry) =>
                entry.key is! String ||
                (entry.key as String).length > 512 ||
                videoCodecPolicy[entry.value]?.legacy != true,
          ))) {
    return null;
  }
  if (health != null &&
      (health is! Map ||
          health.values.any(
            (value) => !['healthy', 'decode', 'download'].contains(value),
          ))) {
    return null;
  }
  return {
    ...legacy.cast<String>(),
    if (extension is List)
      ...extension.whereType<String>().where(
            (codec) => videoCodecPolicy[codec]?.legacy == false,
          ),
  };
}

/// Count every viewer who can use each candidate, including viewers who support both.
/// Iteration order breaks ties in favour of AV1.
String? preferredPremiumVideoCodec(
  Map<String, int> audience,
  Set<String> available,
) {
  String? selected;
  var largest = 0;
  for (final codec in ['av1', 'h265']) {
    final viewers = audience[codec] ?? 0;
    if (available.contains(codec) && viewers > largest) {
      selected = codec;
      largest = viewers;
    }
  }
  return selected;
}

/// Shared adaptation policy. Mirrors `VIDEO_POLICY` in
/// frontend/src/lib/voice/adaptive-video.ts and the native desktop
/// controller in kaede-voice; change all three together.
abstract final class VideoPolicy {
  static const sample = Duration(seconds: 2);

  /// Consecutive bad samples before degrading (6s).
  static const degradeSamples = 3;

  /// Consecutive healthy samples before recovering one step (30s).
  static const recoverSamples = 15;
  static const minChange = Duration(seconds: 20);

  /// Bandwidth limitation is expected while the congestion controller ramps up.
  static const uploadWarmup = Duration(seconds: 10);

  /// A viewer without codec hints is pending this long before it counts as VP8-only.
  static const hintGrace = Duration(seconds: 8);
  static const announceEvery = Duration(seconds: 4);

  /// Retry delays after a failed premium attempt; afterwards the session stops retrying.
  static const backoff = [
    Duration(minutes: 1),
    Duration(minutes: 5),
    Duration(minutes: 15),
  ];
  static const bitrateStep = .72;
  static const totalBitrate = 8000000;
  static const probeRetry = Duration(minutes: 1);
  static const motionFpsStep = .85;
  static const detailFpsStep = .65;
  static const scaleStep = 1.25;
  static const motionFpsFloor = 12;
  static const detailFpsFloor = 5;

  /// Dual-cast only when at least this share of viewers would watch the premium codec.
  static const dualCastShare = 1 / 3;

  /// Healthy premium samples (5 min) after which earlier playback failures are forgotten.
  static const premiumTrustSamples = 150;
}

/// Upload pressure triggers entry; relief from the more efficient codec is not a reason to exit.
bool useVp9Compatible({
  required bool active,
  required bool audienceSupports,
  required bool cpuSpare,
  required bool uploadConstrained,
  required int level,
  required int healthySamples,
  required bool lowPower,
}) =>
    audienceSupports &&
    cpuSpare &&
    level == 0 &&
    !lowPower &&
    (active ||
        uploadConstrained && healthySamples >= VideoPolicy.recoverSamples);

int videoTrackBitrate(int requested, int sources, int variants) =>
    min(requested, VideoPolicy.totalBitrate ~/ max(1, sources)) ~/
    max(1, variants);

/// Preserve the SDK ladder's proportions without letting per-layer floors exceed its share.
List<int> videoBitrateLimits(List<int?> original, int ceiling, int level) {
  final weights = original
      .map((bitrate) => bitrate != null && bitrate > 0 ? bitrate : 1500000)
      .toList();
  final sum = weights.fold(0, (total, bitrate) => total + bitrate);
  if (sum == 0) return [];
  return weights.map((bitrate) {
    final allocated = min(bitrate, (max(0, ceiling) * bitrate / sum).floor());
    return max(min(allocated, 150000),
        (allocated * pow(VideoPolicy.bitrateStep, level)).floor());
  }).toList();
}

/// A newly published sender briefly lacks deltas; missing data never extends the last measured verdict.
class VideoCpuHeadroom {
  DateTime? _observed;
  bool _spare = false;

  void observe(double? load, DateTime now) {
    if (load == null) return;
    _observed = now;
    _spare = load.isFinite && load >= 0 && load < .5;
  }

  bool available(DateTime now) =>
      _spare &&
      _observed != null &&
      now.difference(_observed!) <= VideoPolicy.uploadWarmup;
}

/// Every active simulcast layer needs timing evidence; one measured sender cannot cover another.
bool videoEncodeEvidenceComplete(
    List<String?>? activeRids, Set<String> measuredRids) {
  if (activeRids == null) return false;
  if (activeRids.isEmpty) return true;
  if (activeRids.length == 1) return measuredRids.isNotEmpty;
  return activeRids.every((rid) => rid != null && rid.isNotEmpty) &&
      activeRids.toSet().length == activeRids.length &&
      activeRids.every(measuredRids.contains);
}

double compatibleVideoScale(int width, int height) =>
    max(1.0, max(max(width, height) / 1280, min(width, height) / 720));

/// Positive hardware proof is stable; failures and timeouts must not stall every controller tick.
class VideoHardwareProofs {
  final _values = <String, (bool, DateTime)>{};

  bool? cached(String key, DateTime now) {
    final entry = _values[key];
    if (entry == null) return null;
    return entry.$1 || now.difference(entry.$2) < VideoPolicy.probeRetry
        ? entry.$1
        : null;
  }

  void record(String key, bool hardware, DateTime now) =>
      _values[key] = (hardware, now);
  void clearCodec(String codec) =>
      _values.removeWhere((key, _) => key.startsWith('$codec/'));
}

/// Escalating retry delays for an attempt that failed (premium encode or playback).
class VideoBackoff {
  final _entries = <String, (int, DateTime?)>{};

  bool blocked(String key, DateTime now) {
    final entry = _entries[key];
    if (entry == null) return false;
    final until = entry.$2;
    return until == null || until.isAfter(now);
  }

  void fail(String key, DateTime now) {
    final failures = _entries[key]?.$1 ?? 0;
    _entries[key] = (
      failures + 1,
      failures < VideoPolicy.backoff.length
          ? now.add(VideoPolicy.backoff[failures])
          : null,
    );
  }

  void clear(String prefix) =>
      _entries.removeWhere((key, _) => key.startsWith(prefix));
}

/// Evidence is sampled every two seconds. Missing statistics never mean healthy.
class VideoEvidence {
  int bad = 0;
  int good = 0;
  int level = 0;
  DateTime? changed;

  bool observe(String? reason, DateTime now) {
    if (reason == null) {
      bad = good = 0;
      return false;
    }
    if (reason == 'healthy') {
      good++;
      bad = 0;
    } else {
      bad++;
      good = 0;
    }
    if (changed != null && now.difference(changed!) < VideoPolicy.minChange) {
      return false;
    }
    final next = bad >= VideoPolicy.degradeSamples
        ? min(3, level + 1)
        : good >= VideoPolicy.recoverSamples
            ? max(0, level - 1)
            : level;
    if (next == level) return false;
    level = next;
    bad = good = 0;
    changed = now;
    return true;
  }
}

String? videoReceiverHealth(
  Map<String, dynamic> current,
  Map<String, dynamic> previous,
) {
  num delta(String field) => current[field] is num && previous[field] is num
      ? (current[field] as num) - (previous[field] as num)
      : 0;
  final decoded = delta('framesDecoded');
  final received = delta('packetsReceived');
  final lost = max(0, delta('packetsLost'));
  final dropped = max(0, delta('framesDropped'));
  final decodeSeconds = delta('totalDecodeTime');
  final download = received > 0 && lost / (received + lost) > .05;
  final stalled = current['framesDecoded'] is num &&
      previous['framesDecoded'] is num &&
      decoded == 0 &&
      (delta('framesReceived') > 0 || dropped > 0);
  final decode = stalled ||
      decoded > 0 &&
          (dropped / (decoded + dropped) > .12 ||
              (decodeSeconds > 0 &&
                  decodeSeconds /
                          decoded *
                          ((current['framesPerSecond'] as num?) ?? 30) >
                      .8));
  return download
      ? 'download'
      : decode
          ? 'decode'
          : decoded > 0
              ? 'healthy'
              : null;
}

/// Device conditions that stutter or drain before WebRTC stats show it.
class VideoDeviceState {
  /// 0 nominal .. 3 critical (iOS thermalState; Android status normalized).
  int thermal = 0;
  bool lowPower = false;
  bool av1HardwareDecode = false;
  bool hevcHardwareDecode = false;
  bool cellular = false;

  /// Thermal throttling raises the encode budget level before frames drop.
  int get levelFloor => thermal >= 3
      ? 2
      : thermal >= 2
          ? 1
          : 0;

  /// Software AV1 decoding is avoided while hot or in low-power mode.
  bool get softwareAv1Allowed => thermal < 2 && !lowPower;
}

/// Owns video-only subscriptions and the combined native sender budget.
/// Room encryption/key-provider state is deliberately never changed here.
class AdaptiveVideo {
  AdaptiveVideo(this.room, {required this.quality, required this.onNotice});

  static const topic = 'kaede.video.v1';
  static VideoPublishOptions publishOptions(
      Room room, TrackSource source, MobileMediaQuality quality,
      {AdaptiveVideo? controller}) {
    final options = room.roomOptions.defaultVideoPublishOptions;
    bool h264Allowed() =>
        room.e2eeManager == null ||
        (controller != null &&
            identical(controller.room, room) &&
            controller._h264Allowed);
    final initialCodec = options.videoCodec == 'h264' && !h264Allowed()
        ? 'vp8'
        : options.videoCodec;
    final sourceName =
        source == TrackSource.screenShareVideo ? 'screen_share' : 'camera';
    return options.copyWith(
      name: '$topic:$sourceName:$initialCodec',
      videoCodec: initialCodec,
      screenShareEncoding: quality.screen.profile.parameters.encoding,
      backupVideoCodec: const BackupVideoCodec(enabled: false),
      configureEncodings: (track, encodings, codec) async {
        // LiveKit can substitute its negotiated codec after we choose our preference.
        if (codec == 'h264' && !h264Allowed()) {
          throw StateError('This call requires another video codec');
        }
        await _prepareInitialEncodings(room, quality, track, encodings, codec);
      },
    );
  }

  static Future<void> _prepareInitialEncodings(
      Room room,
      MobileMediaQuality quality,
      LocalVideoTrack track,
      List<rtc.RTCRtpEncoding> encodings,
      String codec) async {
    _initialBase[track] = encodings
        .map((encoding) => (
              encoding.maxBitrate,
              encoding.maxFramerate,
              encoding.scaleResolutionDownBy,
              encoding.priority
            ))
        .toList();
    final publications = room.localParticipant?.videoTrackPublications
            .where((publication) =>
                !publication.muted && publication.track != track)
            .toList() ??
        <LocalTrackPublication<LocalVideoTrack>>[];
    final sources = {
      ...publications.map((publication) => publication.source),
      track.source
    };
    int variants(TrackSource source) =>
        publications
            .where((publication) => publication.source == source)
            .length +
        (source == track.source ? 1 : 0);
    int requested(LocalVideoTrack video) =>
        video.source == TrackSource.screenShareVideo
            ? quality.screen.profile.maxBitrate
            : room.roomOptions.defaultVideoPublishOptions.videoEncoding
                    ?.maxBitrate ??
                video.currentOptions.params.encoding?.maxBitrate ??
                1500000;
    void limit(LocalVideoTrack video, List<rtc.RTCRtpEncoding> values,
        String? actualCodec) {
      final count = variants(video.source);
      final ceiling =
          videoTrackBitrate(requested(video), sources.length, count);
      final pristine = _initialBase[video]!;
      if (pristine.length != values.length) {
        throw StateError('Video ladder changed during publication');
      }
      // Reservations may reduce an already adapted sender, but never raise its aggregate budget.
      final currentLimit = values.any((encoding) => encoding.maxBitrate == null)
          ? ceiling
          : values.fold<int>(
              0, (total, encoding) => total + encoding.maxBitrate!);
      final limits = videoBitrateLimits(
          pristine.map((encoding) => encoding.$1).toList(),
          min(ceiling, currentLimit),
          0);
      var scale = 1.0;
      if (count > 1 && !isPremiumVideoCodec(actualCodec)) {
        final settings = video.mediaStreamTrack.getSettings();
        final width = settings['width'] is int
            ? settings['width'] as int
            : video.currentOptions.params.dimensions.width;
        final height = settings['height'] is int
            ? settings['height'] as int
            : video.currentOptions.params.dimensions.height;
        if (width <= 0 || height <= 0) {
          throw StateError('Compatible video dimensions are unavailable');
        }
        scale = compatibleVideoScale(width, height);
      }
      for (var i = 0; i < values.length; i++) {
        final encoding = values[i];
        final nextScale = max(
            max(encoding.scaleResolutionDownBy ?? 1, pristine[i].$3 ?? 1),
            scale);
        encoding.maxBitrate = limits[i];
        encoding.scaleResolutionDownBy = nextScale;
      }
    }

    // Reserve every existing source's share before the new sender can emit RTP.
    for (final publication in publications) {
      final video = publication.track;
      final sender = video?.sender;
      final parameters = sender?.parameters;
      final values = parameters?.encodings;
      if (video == null || sender == null) continue;
      if (parameters == null || values == null || values.isEmpty) {
        throw StateError('Existing video bandwidth is unavailable');
      }
      _initialBase[video] ??= values
          .map((encoding) => (
                encoding.maxBitrate,
                encoding.maxFramerate,
                encoding.scaleResolutionDownBy,
                encoding.priority
              ))
          .toList();
      limit(video, values, video.codec);
      if (!await sender.setParameters(parameters)) {
        throw StateError('Could not reserve initial video bandwidth');
      }
    }
    limit(track, encodings, codec);
  }

  static const _native = MethodChannel('FlutterWebRTC.Method');
  // Encoder capability depends on the device and settings, not on the call.
  static final _hardware = VideoHardwareProofs();
  static final _hardwareNames = <String>{};
  static final _initialBase =
      Expando<List<(int?, int?, double?, rtc.RTCPriorityType)>>();
  final _peers = <String, (DateTime, Map<String, dynamic>)>{};
  final _seen = <String, DateTime>{};
  final _unprovedPremium = <String, int>{};
  final _variants =
      <TrackSource, (LocalTrackPublication<LocalVideoTrack>, String?)>{};
  final _premiumKeys = <TrackSource, String>{};
  final _audience = <TrackSource, (int, int)>{};
  final _detached = <String>{};
  final _baselineChanged = <TrackSource, DateTime>{};
  final _publishedAt = <String, DateTime>{};
  EventsListener<RoomEvent>? _events;
  final Room room;
  final MobileMediaQuality Function() quality;
  final void Function(String?) onNotice;
  final _publisher = VideoEvidence();
  final _upload = VideoEvidence();
  final _backoff = VideoBackoff();
  final _device = VideoDeviceState();
  final _receivers = <String, VideoEvidence>{};
  final _capabilityDemand = <String>{};
  final _previous = <String, Map<String, dynamic>>{};
  final _base = <String, List<(int?, int?, double?, rtc.RTCPriorityType)>>{};
  final _demand = <String, String>{};
  final _health = <String, String>{};
  Set<String> _codecs = {'vp8'};
  String _lastAnnounced = '';
  DateTime _lastAnnounceAt = DateTime.fromMillisecondsSinceEpoch(0);
  Timer? _timer;
  bool _busy = false;
  bool _tickAgain = false;
  bool _closed = false;
  bool _videoVisible = true;
  final _cpuHeadroom = VideoCpuHeadroom();
  bool get _spareCpu => _cpuHeadroom.available(DateTime.now());

  int get _level => max(_publisher.level, _device.levelFloor);
  bool get _uploadConstrained => _upload.level > 0;

  bool get _h264Allowed {
    if (room.e2eeManager == null) return true;
    if (_closed) return false;
    final now = DateTime.now();
    return sharesEncryptedH264Format(
        room.remoteParticipants.keys.map((identity) {
      final hint = _peers[identity];
      return hint == null || now.difference(hint.$1).inSeconds > 25
          ? null
          : hint.$2['encryptedH264'];
    }));
  }

  /// Decode codecs offered to publishers; AV1 is withheld when it would be
  /// decoded in software on a hot or power-saving device.
  Set<String> get _advertised {
    return _codecs.where((codec) {
      if (codec == 'h264') return _h264Allowed;
      if (codec == 'h265') return _device.hevcHardwareDecode;
      return codec != 'av1' ||
          _device.av1HardwareDecode ||
          _device.softwareAv1Allowed;
    }).toSet();
  }

  void setVideoVisible(bool visible) {
    if (_videoVisible == visible || _closed) return;
    _videoVisible = visible;
    unawaited(tick());
  }

  int _ticks = 0;

  Future<void> start() async {
    _events = room.createListener()
      ..on<RoomReconnectingEvent>((_) {
        if (room.e2eeManager != null) {
          _peers.clear();
          _seen.clear();
        }
      })
      ..on<RoomReconnectedEvent>((_) {
        unawaited(_announce(force: true).catchError((_) {}));
        unawaited(tick());
      })
      ..on<TrackPublishedEvent>((event) {
        if (_closed) return;
        if (event.publication.kind == TrackType.AUDIO) {
          unawaited(event.publication.subscribe().catchError((_) {}));
        } else {
          unawaited(tick());
        }
      })
      ..on<ParticipantConnectedEvent>((event) {
        _seen[event.participant.identity] = DateTime.now();
        _peers.remove(event.participant.identity);
        // New viewers need our hints before publishers can choose codecs.
        unawaited(_announce(force: true).catchError((_) {}));
        unawaited(tick());
      })
      ..on<ParticipantDisconnectedEvent>((event) {
        final identity = event.participant.identity;
        _seen.remove(identity);
        _peers.remove(identity);
        _backoff.clear('$identity/');
        unawaited(_announce().catchError((_) {}));
        unawaited(tick());
      })
      ..on<DataReceivedEvent>((event) {
        final peer = event.participant;
        if (_closed ||
            event.topic != topic ||
            peer == null ||
            !identical(room.remoteParticipants[peer.identity], peer)) {
          return;
        }
        final allowedBefore = _h264Allowed;
        if (room.e2eeManager != null) _peers.remove(peer.identity);
        try {
          if (event.data.length > 8192) return;
          final value = jsonDecode(utf8.decode(event.data));
          if (value is! Map<String, dynamic> ||
              value['demand'] is! Map ||
              value['health'] is! Map) {
            return;
          }
          final codecs = parseVideoCodecs(value);
          if (codecs == null) return;
          _peers[peer.identity] = (
            DateTime.now(),
            {...value, 'codecs': codecs.toList()},
          );
        } catch (_) {
          /* Ignore malformed peer hints. */
        } finally {
          if (allowedBefore != _h264Allowed) {
            unawaited(_announce().catchError((_) {}));
            unawaited(tick());
          }
        }
      });
    try {
      final caps = await rtc.getRtpReceiverCapabilities('video');
      _codecs = caps.codecs
              ?.map((c) => c.mimeType.toLowerCase().split('/').last)
              .where(videoCodecPolicy.containsKey)
              .toSet() ??
          {'vp8'};
      if (_codecs.isEmpty) _codecs = {'vp8'};
    } catch (_) {
      // Unknown decode support keeps the established single-track fallback.
    }
    if (_closed) return;
    await _refreshDeviceState();
    if (_closed) return;
    await tick();
    if (!_closed) {
      _timer = Timer.periodic(VideoPolicy.sample, (_) => unawaited(tick()));
    }
  }

  void close() {
    _closed = true;
    _timer?.cancel();
    unawaited(_events?.dispose());
    _events = null;
    _previous.clear();
    _base.clear();
    _receivers.clear();
  }

  Future<void> _refreshDeviceState() async {
    try {
      final state = await _native
          .invokeMethod<Object>('kaedeVideoDeviceState')
          .timeout(const Duration(seconds: 2));
      if (state is Map) {
        final thermal = state['thermal'];
        _device
          ..thermal = thermal is int ? thermal.clamp(0, 3) : 0
          ..lowPower = state['lowPower'] == true
          ..av1HardwareDecode = state['av1HardwareDecode'] == true
          ..hevcHardwareDecode = state['hevcHardwareDecode'] == true;
      }
    } catch (_) {
      // Older native builds: no device signals, the stats-driven policy applies.
    }
    try {
      final links = await Connectivity().checkConnectivity().timeout(
            const Duration(seconds: 2),
          );
      _device.cellular = links.contains(ConnectivityResult.mobile) &&
          !links.contains(ConnectivityResult.wifi) &&
          !links.contains(ConnectivityResult.ethernet);
    } catch (_) {
      _device.cellular = false;
    }
  }

  /// Sends hints when they change, on membership events, and every
  /// [VideoPolicy.announceEvery] otherwise.
  Future<void> _announce({bool force = false}) async {
    if (_closed) return;
    final payload = encodeVideoHints(_advertised, _demand, _health);
    final now = DateTime.now();
    if (!force &&
        payload == _lastAnnounced &&
        now.difference(_lastAnnounceAt) < VideoPolicy.announceEvery) {
      return;
    }
    _lastAnnounced = payload;
    _lastAnnounceAt = now;
    try {
      await room.localParticipant?.publishData(
        utf8.encode(payload),
        topic: topic,
        reliable: true,
      );
    } catch (_) {
      _lastAnnounced = '';
      rethrow;
    }
  }

  Future<void> tick() async {
    if (_closed || room.connectionState != ConnectionState.connected) return;
    if (_busy) {
      _tickAgain = true;
      return;
    }
    _busy = true;
    try {
      if (_ticks++ % 5 == 0) await _refreshDeviceState();
      if (_closed) return;
      await _subscriptions();
      if (_closed) return;
      await _publishVariants();
      if (_closed) return;
      await _senders();
      onNotice(
        _level >= 2 ||
                _uploadConstrained ||
                _receivers.values.any((e) => e.level >= 2)
            ? 'Video quality reduced to keep this call stable.'
            : null,
      );
      await _announce();
    } catch (_) {
      // Reconnection and platform-specific missing stats are retryable; never
      // renegotiate the room or downgrade encryption to recover video.
    } finally {
      _busy = false;
      if (_tickAgain && !_closed) {
        _tickAgain = false;
        unawaited(tick());
      }
    }
  }

  Future<void> _subscriptions() async {
    final active = <String>{};
    final reportKeys = <String>{};
    final now = DateTime.now();
    final codecs = _advertised;
    final encrypted = room.e2eeManager != null;
    for (final participant in room.remoteParticipants.values.toList()) {
      for (final audio in participant.audioTrackPublications) {
        if (_closed) return;
        await audio.subscribe();
      }
      final groups =
          <TrackSource, List<RemoteTrackPublication<RemoteVideoTrack>>>{};
      for (final publication in participant.videoTrackPublications) {
        if (!_videoVisible) {
          await publication.unsubscribe();
          continue;
        }
        (groups[publication.source] ??= []).add(publication);
      }
      for (final entry in groups.entries) {
        if (_closed) return;
        final source = entry.key == TrackSource.screenShareVideo
            ? 'screen_share'
            : 'camera';
        final key = '${participant.identity}/$source';
        active.add(key);
        final evidence = _receivers.putIfAbsent(key, VideoEvidence.new);
        final managed = entry.value
            .where((p) => p.name.startsWith('$topic:$source:'))
            .toList();
        final choices = managed.isEmpty ? entry.value : managed;
        String codec(RemoteTrackPublication publication) => remoteVideoCodec(
              publication.mimeType,
              managed.isEmpty ? null : publication.name.split(':').last,
              encrypted: encrypted,
            );

        String? fallbackFor() {
          final fallback = choices
              .where(
                (p) =>
                    !isPremiumVideoCodec(codec(p)) && codecs.contains(codec(p)),
              )
              .firstOrNull;
          return fallback == null
              ? ['vp8', 'h264'].where(codecs.contains).firstOrNull
              : videoCodecPolicy[codec(fallback)]?.legacy == true
                  ? codec(fallback)
                  : 'vp8';
        }

        // Software AV1 above 1080p is not attempted on a phone; the publisher
        // offers a scaled fallback for it.
        final av1 = choices.where((p) => codec(p) == 'av1').firstOrNull;
        final dimensions = av1?.dimensions;
        if (dimensions != null &&
            !_device.av1HardwareDecode &&
            dimensions.width * dimensions.height > 1920 * 1088 &&
            av1?.simulcasted != true &&
            !_demand.containsKey(key)) {
          final fallback = fallbackFor();
          if (fallback != null) {
            _demand[key] = fallback;
            _capabilityDemand.add(key);
          }
        }
        final premium = choices
            .where(
              (p) => isPremiumVideoCodec(codec(p)) && codecs.contains(codec(p)),
            )
            .firstOrNull;
        final premiumCodec = premium == null ? null : codec(premium);
        final tryLowerLayer =
            premium?.simulcasted == true && evidence.level == 1;
        final premiumBlocked =
            premiumCodec != null && _backoff.blocked('$key/$premiumCodec', now);
        if (!_demand.containsKey(key) && premiumBlocked) {
          final fallback = fallbackFor();
          if (fallback != null) _demand[key] = fallback;
        }
        if (evidence.level == 0 &&
            _demand.containsKey(key) &&
            !_capabilityDemand.contains(key) &&
            !premiumBlocked) {
          _demand.remove(key);
        }
        final requested = _demand[key];
        var selected = choices
            .where((p) => codec(p) == requested && codecs.contains(codec(p)))
            .firstOrNull;
        if ((requested != null && !isPremiumVideoCodec(requested)) ||
            (evidence.level > 0 && !tryLowerLayer)) {
          selected ??= choices
              .where(
                (p) =>
                    !isPremiumVideoCodec(codec(p)) && codecs.contains(codec(p)),
              )
              .firstOrNull;
        }
        if ((evidence.level == 0 || tryLowerLayer) && !premiumBlocked) {
          selected ??= premium;
        }
        selected ??= choices
            .where((p) => codec(p) == 'vp8' && codecs.contains('vp8'))
            .firstOrNull;
        selected ??= choices
            .where(
              (p) =>
                  codecs.contains(codec(p)) &&
                  (codec(p) != 'h265' || !_backoff.blocked('$key/h265', now)),
            )
            .firstOrNull;
        // Legacy publications may omit codec metadata before subscription.
        selected ??= managed.isEmpty && _h264Allowed
            ? choices
                .where((publication) => codec(publication).isEmpty)
                .firstOrNull
            : null;
        if (selected == null) {
          for (final publication in entry.value) {
            await publication.unsubscribe();
          }
          continue;
        }
        for (final publication in entry.value) {
          if (publication != selected) await publication.unsubscribe();
        }
        final selectedCodec = codec(selected);
        if (!_videoVisible ||
            ((selectedCodec == 'h264' || selectedCodec.isEmpty) &&
                !_h264Allowed)) {
          await selected.unsubscribe();
          continue;
        }
        await selected.subscribe();
        final selectedDimensions = selected.dimensions;
        final softwareAv1LowerLayer = codec(selected) == 'av1' &&
            !_device.av1HardwareDecode &&
            selected.simulcasted &&
            selectedDimensions != null &&
            selectedDimensions.width * selectedDimensions.height > 1920 * 1088;
        VideoQuality receiveQuality() => softwareAv1LowerLayer ||
                (isPremiumVideoCodec(codec(selected!)) &&
                    selected.simulcasted &&
                    evidence.level > 0)
            ? VideoQuality.LOW
            : evidence.level == 0
                ? VideoQuality.HIGH
                : evidence.level == 1
                    ? VideoQuality.MEDIUM
                    : VideoQuality.LOW;
        await selected.setVideoQuality(receiveQuality());
        final track = selected.track;
        if (track?.receiver == null) continue;
        final reports = await track!.receiver!.getStats();
        final report =
            reports.where((r) => r.type == 'inbound-rtp').firstOrNull;
        if (report == null) continue;
        final current = Map<String, dynamic>.from(report.values);
        reportKeys.add('r:${selected.sid}');
        final previous = _previous['r:${selected.sid}'];
        _previous['r:${selected.sid}'] = current;
        if (codec(selected) == 'h265' &&
            current['powerEfficientDecoder'] == false) {
          _device.hevcHardwareDecode = false;
          _backoff.fail('$key/h265', now);
          final fallback = fallbackFor();
          if (fallback != null) _demand[key] = fallback;
          await selected.unsubscribe();
          await _announce(force: true);
          continue;
        }
        if (previous == null) continue;
        final reason = videoReceiverHealth(current, previous);
        final changed = evidence.observe(reason, now);
        // Long healthy premium playback forgets earlier failures, so one bad spell
        // early in a long call does not ratchet to permanent fallback.
        if (isPremiumVideoCodec(codec(selected)) &&
            evidence.level == 0 &&
            evidence.good >= VideoPolicy.premiumTrustSamples) {
          _backoff.clear('$key/${codec(selected)}');
        }
        if (reason == null) continue;
        _health[key] = reason;
        if (changed) {
          await selected.setVideoQuality(receiveQuality());
          // A real spatial layer gets one adaptation interval to settle before
          // switching codec; single-layer encoders need the compatible track.
          if (evidence.level > 0 &&
              (!selected.simulcasted || evidence.level > 1)) {
            final fallbackCodec = fallbackFor();
            // Demand may create a publication which does not exist yet. Keep
            // receiving the current premium track until the publisher provides it.
            if (fallbackCodec != null) {
              if (isPremiumVideoCodec(codec(selected)) &&
                  _demand[key] != fallbackCodec) {
                _backoff.fail('$key/${codec(selected)}', now);
              }
              _demand[key] = fallbackCodec;
            }
          }
          await _announce(force: true);
        }
      }
    }
    _previous.removeWhere(
      (key, _) => key.startsWith('r:') && !reportKeys.contains(key),
    );
    _demand.removeWhere((key, _) => !active.contains(key));
    _capabilityDemand.removeWhere((key) => !active.contains(key));
    _health.removeWhere((key, _) => !active.contains(key));
    _receivers.removeWhere((key, _) => !active.contains(key));
  }

  bool _actualHardware(Map<String, dynamic> stats) {
    if (stats['powerEfficientEncoder'] == false) return false;
    final implementation =
        '${stats['encoderImplementation'] ?? ''}'.toLowerCase();
    return ![
          'libaom',
          'libvpx',
          'software',
          'svt-av1',
        ].any(implementation.contains) &&
        _hardwareNames.any(implementation.contains) &&
        (!implementation.contains('videotoolbox') ||
            stats['powerEfficientEncoder'] == true);
  }

  Future<bool> _hardwareSupports(
    LocalVideoTrack track,
    String codec, {
    String scalabilityMode = 'L1T1',
  }) async {
    final settings = track.mediaStreamTrack.getSettings();
    final width = settings['width'], height = settings['height'];
    // Native capture may differ from its requested preset (notably displays).
    // Only query the actual dimensions that SDK publishing will encode.
    if (width is! int || height is! int || width <= 0 || height <= 0) {
      return false;
    }
    final encoding = track.source == TrackSource.screenShareVideo
        ? quality().screen.profile.parameters.encoding
        : track.lastPublishOptions?.videoEncoding ??
            track.currentOptions.params.encoding;
    final fps = encoding?.maxFramerate ?? 30;
    final bitrate = encoding?.maxBitrate ?? 1500000;
    final key = '$codec/$width/$height/$fps/$bitrate/$scalabilityMode';
    final cached = _hardware.cached(key, DateTime.now());
    if (cached != null) return cached;
    try {
      final result =
          await _native.invokeMethod<Object>('kaedeVideoHardwareSettings', {
        'codec': codec,
        'width': width,
        'height': height,
        'fps': fps,
        'bitrate': bitrate,
        'scalabilityMode': scalabilityMode,
      }).timeout(const Duration(seconds: 3));
      if (result is Map &&
          result['hardware'] == true &&
          result['scalabilityMode'] == scalabilityMode &&
          result['implementation'] is String &&
          (result['implementation'] as String).trim().isNotEmpty) {
        _hardwareNames.add((result['implementation'] as String).toLowerCase());
        _hardware.record(key, true, DateTime.now());
        return true;
      }
    } catch (_) {
      // An unavailable native call gets the same bounded retry delay as a rejection.
    }
    if (!isPremiumVideoCodec(codec)) {
      _hardware.record(key, false, DateTime.now());
    }
    return false;
  }

  Future<void> _restoreBaseline(
    LocalTrackPublication<LocalVideoTrack> publication,
    LocalVideoTrack track,
  ) async {
    if (track.codec == 'h264' && !_h264Allowed) {
      // A legacy join must not resume H.264 while its VP8 replacement is being prepared.
      if (!_detached.contains(publication.sid) && track.sender != null) {
        await track.sender!.replaceTrack(null);
        _detached.add(publication.sid);
      }
      return;
    }
    if (!_detached.contains(publication.sid)) return;
    final sender = track.sender;
    if (sender == null) {
      throw StateError('Compatible video sender is unavailable');
    }
    final active = room.localParticipant!.videoTrackPublications
        .where((other) =>
            other.source == publication.source &&
            other.sid != publication.sid &&
            !other.muted &&
            !_detached.contains(other.sid))
        .toList();
    final count = active.length + 1;
    for (final other in [...active, publication]) {
      if (!await _applyBudget(other, variantCount: count)) {
        throw StateError('Could not reserve compatible video bandwidth');
      }
    }
    if (track.codec == 'h264' && !_h264Allowed) return;
    await sender.replaceTrack(track.mediaStreamTrack);
    // A failed resume must remain retryable before retiring the premium sender.
    _detached.remove(publication.sid);
  }

  Future<void> _publishVariants() async {
    if (kIsWeb ||
        !{
          TargetPlatform.android,
          TargetPlatform.iOS,
          TargetPlatform.macOS,
        }.contains(defaultTargetPlatform)) {
      return;
    }
    final local = room.localParticipant;
    if (local == null) return;
    final now = DateTime.now();
    _peers.removeWhere(
      (identity, hint) =>
          !room.remoteParticipants.containsKey(identity) ||
          now.difference(hint.$1).inSeconds > 25,
    );
    _seen.removeWhere(
      (identity, _) => !room.remoteParticipants.containsKey(identity),
    );
    for (final source in [TrackSource.camera, TrackSource.screenShareVideo]) {
      if (_closed) return;
      var original = local.videoTrackPublications
          .where((p) => p.source == source && p != _variants[source]?.$1)
          .firstOrNull;
      var track = original?.track;
      var existing = _variants[source];
      if (existing != null &&
          (!local.trackPublications.containsKey(existing.$1.sid) ||
              track == null ||
              original!.muted ||
              existing.$2 != track.mediaStreamTrack.id)) {
        if (original != null && track != null && !original.muted) {
          await _restoreBaseline(original, track);
        }
        if (local.trackPublications.containsKey(existing.$1.sid)) {
          await local.removePublishedTrack(existing.$1.sid);
        }
        _variants.remove(source);
        existing = null;
      }
      if (original == null || track == null || original.muted) continue;
      final sourceName =
          source == TrackSource.screenShareVideo ? 'screen_share' : 'camera';
      final demandKey = '${local.identity}/$sourceName';
      final viewers = room.remoteParticipants.keys.toList();
      bool pending(String identity) =>
          !_peers.containsKey(identity) &&
          now.difference(_seen.putIfAbsent(identity, () => now)) <
              VideoPolicy.hintGrace;
      bool supports(String identity, String codec) =>
          (_peers[identity]?.$2['codecs'] as List?)?.contains(codec) == true;
      String? requested(String identity) {
        final demand = _peers[identity]?.$2['demand'];
        final request = demand is Map ? demand[demandKey] : null;
        return request is String && supports(identity, request)
            ? request
            : null;
      }

      bool accepts(String identity, String codec) =>
          supports(identity, codec) &&
          (requested(identity) == null || requested(identity) == codec);

      final fallbackCodec =
          track.codec ?? room.roomOptions.defaultVideoPublishOptions.videoCodec;
      // A viewer whose hints have not arrived yet keeps the current fallback.
      final allH264 = _h264Allowed &&
          viewers.isNotEmpty &&
          viewers.every(
            (identity) => pending(identity)
                ? fallbackCodec == 'h264'
                : supports(identity, 'h264') &&
                    (requested(identity) == null ||
                        isPremiumVideoCodec(requested(identity)) ||
                        requested(identity) == 'h264'),
          );
      final allVp9 = viewers.isNotEmpty &&
          viewers.every(
            (identity) =>
                !pending(identity) &&
                supports(identity, 'vp9') &&
                (requested(identity) == null ||
                    isPremiumVideoCodec(requested(identity))),
          );
      final vp9Allowed = useVp9Compatible(
        active: fallbackCodec == 'vp9',
        audienceSupports: allVp9,
        cpuSpare: _spareCpu,
        uploadConstrained: _uploadConstrained,
        level: _level,
        healthySamples: _publisher.good,
        lowPower: _device.lowPower,
      );
      final preferredFallback = vp9Allowed
          ? 'vp9'
          : allH264 &&
                  await _hardwareSupports(track, 'h264') &&
                  !await _hardwareSupports(track, 'vp8')
              ? 'h264'
              : 'vp8';
      final compatibilityChange = fallbackCodec == 'h264' && !allH264 ||
          fallbackCodec == 'vp9' && (!allVp9 || !vp9Allowed);
      if (fallbackCodec != preferredFallback &&
          (compatibilityChange ||
              now
                      .difference(
                        _baselineChanged[source] ??
                            DateTime.fromMillisecondsSinceEpoch(0),
                      )
                      .inSeconds >=
                  20)) {
        await _restoreBaseline(original, track);
        if (existing != null) {
          await local.removePublishedTrack(existing.$1.sid);
          _variants.remove(source);
          existing = null;
        }
        LocalVideoTrack? replacement;
        LocalTrackPublication<LocalVideoTrack>? replacementPublication;
        try {
          if (!await _applyBudget(original, variantCount: 2)) {
            throw StateError('Could not reserve replacement video bandwidth');
          }
          replacement = await track.cloneCapture();
          if (_closed ||
              original.muted ||
              !local.trackPublications.containsKey(original.sid)) {
            await replacement.stop();
            continue;
          }
          replacementPublication = await local.publishVideoTrack(
            replacement,
            publishOptions:
                publishOptions(room, source, quality(), controller: this)
                    .copyWith(
              name: '$topic:$sourceName:$preferredFallback',
              videoCodec: preferredFallback,
              simulcast: !videoCodecPolicy[preferredFallback]!.scalable,
              scalabilityMode: 'L1T1',
            ),
          );
          if (_closed ||
              replacement.codec != preferredFallback ||
              (preferredFallback == 'h264' && !_h264Allowed) ||
              original.muted ||
              !local.trackPublications.containsKey(original.sid)) {
            await local.removePublishedTrack(replacementPublication.sid);
            continue;
          }
          if (!await _applyBudget(replacementPublication, variantCount: 2)) {
            throw StateError('Could not limit replacement video bandwidth');
          }
          await local.removePublishedTrack(original.sid);
          original = replacementPublication;
          track = replacement;
          _baselineChanged[source] = now;
        } catch (_) {
          if (replacementPublication != null &&
              local.trackPublications.containsKey(replacementPublication.sid)) {
            if (!local.trackPublications.containsKey(original!.sid)) {
              original = replacementPublication;
              track = replacement;
            } else {
              await local.removePublishedTrack(replacementPublication.sid);
            }
          } else {
            await replacement?.stop();
          }
          _baselineChanged[source] = now;
        } finally {
          await _applySourceBudgets(source);
        }
      }
      if (original == null || track == null) continue;
      final settings = track.mediaStreamTrack.getSettings();
      String premiumKey(String codec) =>
          '$sourceName/$codec/${settings['width']}x${settings['height']}/L1T1';
      final known = viewers.where((identity) => !pending(identity)).toList();
      final audience = {
        for (final codec in ['av1', 'h265'])
          codec: known.where((identity) => accepts(identity, codec)).length,
      };
      final available = <String>{};
      for (final codec in audience.keys) {
        if (audience[codec]! > 0 && !_backoff.blocked(premiumKey(codec), now)) {
          if (await _hardwareSupports(track, codec)) {
            available.add(codec);
          } else {
            // Device session exhaustion is retryable; never cache a failed
            // premium initialization for the remainder of the process.
            _backoff.fail(premiumKey(codec), now);
          }
        }
      }
      final preferred = preferredPremiumVideoCodec(audience, available);
      final premiumViewers = audience[preferred] ?? 0;
      final others = known.length - premiumViewers;
      final level = _level;
      final dualAllowed = level == 0 &&
          !_uploadConstrained &&
          !_device.cellular &&
          !_device.lowPower &&
          premiumViewers >= (known.length * VideoPolicy.dualCastShare).ceil();
      final variantCodec =
          preferred != null && (others == 0 ? level < 3 : dualAllowed)
              ? preferred
              : null;
      final variantKey = variantCodec == null ? null : premiumKey(variantCodec);
      if (variantKey != null) _premiumKeys[source] = variantKey;
      _audience[source] = (
        variantCodec == null ? 0 : premiumViewers,
        variantCodec == null ? known.length : others,
      );
      if (existing != null && existing.$1.track?.codec != variantCodec) {
        final retiredCodec = existing.$1.track?.codec;
        if (retiredCodec != null &&
            isPremiumVideoCodec(retiredCodec) &&
            preferred == retiredCodec &&
            (others == 0 ||
                (!_device.cellular &&
                    !_device.lowPower &&
                    premiumViewers >=
                        (known.length * VideoPolicy.dualCastShare).ceil())) &&
            (level > 0 || _uploadConstrained) &&
            !_backoff.blocked(premiumKey(retiredCodec), now)) {
          _backoff.fail(premiumKey(retiredCodec), now);
        }
        await _restoreBaseline(original, track);
        await local.removePublishedTrack(existing.$1.sid);
        _variants.remove(source);
        existing = null;
      }
      if (variantCodec != null && existing == null) {
        LocalVideoTrack? clone;
        LocalTrackPublication<LocalVideoTrack>? publication;
        try {
          if (!await _applyBudget(original, variantCount: 2)) {
            throw StateError('Could not reserve premium video bandwidth');
          }
          clone = await track.cloneCapture();
          if (_closed ||
              original.muted ||
              !local.trackPublications.containsKey(original.sid)) {
            await clone.stop();
            continue;
          }
          final options =
              publishOptions(room, source, quality(), controller: this)
                  .copyWith(
            name: '$topic:$sourceName:$variantCodec',
            videoCodec: variantCodec,
            simulcast: false,
            scalabilityMode: 'L1T1',
            backupVideoCodec: const BackupVideoCodec(enabled: false),
          );
          publication =
              await local.publishVideoTrack(clone, publishOptions: options);
          if (_closed ||
              clone.codec != variantCodec ||
              original.muted ||
              !local.trackPublications.containsKey(original.sid)) {
            _backoff.fail(variantKey!, now);
            await local.removePublishedTrack(publication.sid);
            continue;
          }
          _variants[source] = existing = (
            publication,
            track.mediaStreamTrack.id,
          );
          if (!await _applyBudget(publication, variantCount: 2)) {
            throw StateError('Could not limit premium video bandwidth');
          }
        } catch (_) {
          _backoff.fail(variantKey!, now);
          if (publication != null &&
              local.trackPublications.containsKey(publication.sid)) {
            await local.removePublishedTrack(publication.sid);
          }
          _variants.remove(source);
          existing = null;
          await clone?.stop();
        } finally {
          await _applySourceBudgets(source);
        }
      }
      final allUseVariant = existing != null &&
          viewers.isNotEmpty &&
          viewers.every((identity) => accepts(identity, variantCodec!));
      if (allUseVariant && !_detached.contains(original.sid)) {
        await track.sender?.replaceTrack(null);
        _detached.add(original.sid);
      } else if (!allUseVariant) {
        await _restoreBaseline(original, track);
      }
      await _applySourceBudgets(source);
    }
    _detached.removeWhere((sid) => !local.trackPublications.containsKey(sid));
  }

  Future<void> _senders() async {
    final publications =
        room.localParticipant?.videoTrackPublications.toList() ?? [];
    final senderIds =
        publications.map((p) => p.track?.sender?.senderId).toSet();
    _base.removeWhere((key, _) => !senderIds.contains(key));
    final sids = publications.map((p) => p.sid).toSet();
    _publishedAt.removeWhere((sid, _) => !sids.contains(sid));
    final now = DateTime.now();
    String? reason;
    var upload = false;
    var measuredUpload = false;
    var encodeLoad = 0.0;
    var measuredEncoding = false;
    var incompleteEncoding = false;
    final reportKeys = <String>{};
    for (final publication in publications) {
      if (_closed) return;
      if (publication.muted || _detached.contains(publication.sid)) continue;
      final sender = publication.track?.sender;
      if (sender == null) {
        incompleteEncoding = true;
        continue;
      }
      final expectedLayers = sender.parameters.encodings
          ?.where((encoding) => encoding.active)
          .map((encoding) => encoding.rid)
          .toList();
      final measuredLayers = <String>{};
      final publishedAt = _publishedAt.putIfAbsent(publication.sid, () => now);
      List<rtc.StatsReport> reports;
      try {
        reports = await sender.getStats().timeout(VideoPolicy.sample);
      } catch (_) {
        incompleteEncoding = true;
        continue;
      }
      for (final report in reports) {
        if (report.type != 'outbound-rtp') continue;
        final reportKey = 's:${sender.senderId}/${report.id}';
        reportKeys.add(reportKey);
        final current = Map<String, dynamic>.from(report.values)
          ..['_timestamp'] = report.timestamp;
        final previous = _previous[reportKey];
        _previous[reportKey] = current;
        final actualHardware = _actualHardware(current);
        if (previous != null &&
            current['totalEncodeTime'] is num &&
            previous['totalEncodeTime'] is num &&
            current['framesEncoded'] is num &&
            previous['framesEncoded'] is num) {
          final seconds =
              (report.timestamp - (previous['_timestamp'] as num)) / 1000;
          final frames = (current['framesEncoded'] as num) -
              (previous['framesEncoded'] as num);
          final encoded = (current['totalEncodeTime'] as num) -
              (previous['totalEncodeTime'] as num);
          if (seconds.isFinite &&
              seconds > 0 &&
              frames.isFinite &&
              frames > 0 &&
              encoded.isFinite &&
              encoded > 0) {
            measuredEncoding = true;
            measuredLayers
                .add(current['rid'] is String ? current['rid'] as String : '');
            if (!actualHardware) {
              encodeLoad += encoded / seconds;
            } else {
              final requested = publication.track?.lastPublishOptions;
              final fps = (publication.source == TrackSource.screenShareVideo
                      ? requested?.screenShareEncoding?.maxFramerate
                      : requested?.videoEncoding?.maxFramerate) ??
                  30;
              if (encoded / frames * fps > 1.2 && frames / seconds < fps * .8) {
                reason = 'encode';
              }
            }
          }
        }
        if (isPremiumVideoCodec(publication.track?.codec) &&
            (report.values['framesEncoded'] as num? ?? 0) >
                (previous?['framesEncoded'] as num? ?? 0)) {
          _unprovedPremium[publication.sid] =
              actualHardware ? 0 : (_unprovedPremium[publication.sid] ?? 0) + 1;
          final premiumKey = _premiumKeys[publication.source];
          if (_unprovedPremium[publication.sid]! >=
                  (publication.track?.codec == 'h265' ? 1 : 3) &&
              premiumKey != null &&
              !_backoff.blocked(premiumKey, now)) {
            // Retire an encoder that falls back to software at runtime.
            _hardware.clearCodec(publication.track!.codec!);
            _backoff.fail(premiumKey, now);
            _tickAgain = true;
          }
        }
        final limitation = report.values['qualityLimitationReason'];
        if (limitation == 'cpu') reason = 'encode';
        // Ramp-up reports "bandwidth" while the estimate grows; it is not pressure.
        if (limitation is String &&
            now.difference(publishedAt) >= VideoPolicy.uploadWarmup) {
          measuredUpload = true;
          if (limitation == 'bandwidth') upload = true;
        }
        if (limitation == 'none' || limitation == 'bandwidth') {
          reason ??= 'healthy';
        }
      }
      incompleteEncoding |=
          !videoEncodeEvidenceComplete(expectedLayers, measuredLayers);
    }
    _previous.removeWhere(
      (key, _) => key.startsWith('s:') && !reportKeys.contains(key),
    );
    if (reason == 'encode' || measuredEncoding && encodeLoad >= .5) {
      _cpuHeadroom.observe(1, now);
    } else {
      _cpuHeadroom.observe(
          measuredEncoding && !incompleteEncoding ? encodeLoad : null, now);
    }
    if (measuredEncoding) {
      if (encodeLoad > .85) {
        reason = 'encode';
      } else {
        reason ??= 'healthy';
      }
    }
    // Encode pressure drives the level; upload pressure only sheds dual-cast
    // and is otherwise left to the congestion controller.
    _publisher.observe(
        reason == 'healthy' && incompleteEncoding ? null : reason, now);
    _upload.observe(
      measuredUpload
          ? upload
              ? 'upload'
              : 'healthy'
          : null,
      now,
    );
    for (final publication in publications) {
      if (_closed) return;
      if (!publication.muted && !_detached.contains(publication.sid)) {
        await _applyBudget(publication);
      }
    }
  }

  Future<void> _applySourceBudgets(TrackSource source) async {
    for (final publication in room.localParticipant?.videoTrackPublications ??
        <LocalTrackPublication<LocalVideoTrack>>[]) {
      if (publication.source == source &&
          !publication.muted &&
          !_detached.contains(publication.sid)) {
        await _applyBudget(publication);
      }
    }
  }

  int _requestedBitrate(LocalVideoTrack track) => track.source ==
          TrackSource.screenShareVideo
      ? quality().screen.profile.maxBitrate
      : room.roomOptions.defaultVideoPublishOptions.videoEncoding?.maxBitrate ??
          track.currentOptions.params.encoding?.maxBitrate ??
          1500000;

  Map<TrackSource, int> _activeSources() {
    final counts = <TrackSource, int>{};
    for (final publication in room.localParticipant?.videoTrackPublications ??
        <LocalTrackPublication<LocalVideoTrack>>[]) {
      if (!publication.muted && !_detached.contains(publication.sid)) {
        counts.update(publication.source, (count) => count + 1,
            ifAbsent: () => 1);
      }
    }
    return counts;
  }

  int _trackBudget(LocalVideoTrack track, int variantCount) =>
      videoTrackBitrate(
          _requestedBitrate(track), _activeSources().length, variantCount);

  Future<bool> _applyBudget(LocalTrackPublication<LocalVideoTrack> publication,
      {int? variantCount}) async {
    final track = publication.track;
    final sender = track?.sender;
    final parameters = sender?.parameters;
    final encodings = parameters?.encodings;
    if (track == null ||
        sender == null ||
        parameters == null ||
        encodings == null ||
        encodings.isEmpty) {
      return false;
    }
    variantCount ??= _activeSources()[publication.source] ?? 1;
    final ceiling = _trackBudget(track, variantCount);
    final level = _level;
    final audience = _audience[publication.source];
    final isVariant = isPremiumVideoCodec(track.codec);
    rtc.RTCPriorityType? priority;
    if (variantCount > 1 && audience != null) {
      final mine = isVariant ? audience.$1 : audience.$2;
      final theirs = isVariant ? audience.$2 : audience.$1;
      priority =
          mine >= theirs ? rtc.RTCPriorityType.high : rtc.RTCPriorityType.low;
    }
    final settings = track.mediaStreamTrack.getSettings();
    final width = settings['width'] is int
        ? settings['width'] as int
        : track.currentOptions.params.dimensions.width;
    final height = settings['height'] is int
        ? settings['height'] as int
        : track.currentOptions.params.dimensions.height;
    final dualCompatible = variantCount > 1 && !isVariant;
    if (dualCompatible && (width <= 0 || height <= 0)) return false;
    final capScale = dualCompatible ? compatibleVideoScale(width, height) : 1.0;
    final baseline = _base.putIfAbsent(
        sender.senderId,
        () =>
            _initialBase[track] ??
            encodings
                .map((encoding) => (
                      encoding.maxBitrate,
                      encoding.maxFramerate,
                      encoding.scaleResolutionDownBy,
                      encoding.priority
                    ))
                .toList());
    if (baseline.length != encodings.length) return false;
    final limits = videoBitrateLimits(
        baseline.map((encoding) => encoding.$1).toList(), ceiling, level);
    final text = publication.source == TrackSource.screenShareVideo &&
        quality().screen != ScreenShareQuality.smooth;
    for (var i = 0; i < encodings.length; i++) {
      final original = baseline[i];
      final encoding = encodings[i];
      encoding.priority = priority ?? original.$4;
      encoding.maxBitrate = limits[i];
      final fps = original.$2 ?? 30;
      final floor =
          text ? VideoPolicy.detailFpsFloor : VideoPolicy.motionFpsFloor;
      encoding.maxFramerate = max(
          min(fps, floor),
          (fps *
                  pow(
                      text
                          ? VideoPolicy.detailFpsStep
                          : VideoPolicy.motionFpsStep,
                      level))
              .round());
      // Keep each lower SDK layer intact; only layers exceeding the compatible box shrink.
      encoding.scaleResolutionDownBy = max(
          capScale,
          (original.$3 ?? 1) *
              (text
                  ? (level >= 3 ? 2 : 1)
                  : pow(VideoPolicy.scaleStep, level).toDouble()));
    }
    if (!await sender.setParameters(parameters)) return false;
    return true;
  }
}
