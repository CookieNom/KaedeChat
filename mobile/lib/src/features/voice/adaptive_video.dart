import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart' as rtc;
import 'package:livekit_client/livekit_client.dart';

import 'media_quality.dart';

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
    if (changed != null && now.difference(changed!).inSeconds < 20) {
      return false;
    }
    final next = bad >= 3
        ? min(3, level + 1)
        : good >= 15
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
    Map<String, dynamic> current, Map<String, dynamic> previous) {
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

/// Owns video-only subscriptions and the combined native sender budget.
/// Room encryption/key-provider state is deliberately never changed here.
class AdaptiveVideo {
  AdaptiveVideo(this.room, {required this.quality, required this.onNotice});

  static const topic = 'kaede.video.v1';
  static VideoPublishOptions publishOptions(
      Room room, TrackSource source, MobileMediaQuality quality) {
    final options = room.roomOptions.defaultVideoPublishOptions;
    final sourceName =
        source == TrackSource.screenShareVideo ? 'screen_share' : 'camera';
    return options.copyWith(
        name: '$topic:$sourceName:${options.videoCodec}',
        screenShareEncoding: quality.screen.profile.parameters.encoding,
        backupVideoCodec: const BackupVideoCodec(enabled: false));
  }

  static const _native = MethodChannel('FlutterWebRTC.Method');
  final _peers = <String, (DateTime, Map<String, dynamic>)>{};
  final _hardware = <String, bool>{};
  final _hardwareNames = <String>{};
  final _unprovedAv1 = <String, int>{};
  final _variants =
      <TrackSource, (LocalTrackPublication<LocalVideoTrack>, String?)>{};
  final _detached = <String>{};
  final _baselineChanged = <TrackSource, DateTime>{};
  final _failedUntil = <TrackSource, DateTime>{};
  EventsListener<RoomEvent>? _events;
  final Room room;
  final MobileMediaQuality Function() quality;
  final void Function(String?) onNotice;
  final _publisher = VideoEvidence();
  final _receivers = <String, VideoEvidence>{};
  final _previous = <String, Map<String, dynamic>>{};
  final _base = <String, List<(int?, int?, double?)>>{};
  final _appliedLevel = <String, int>{};
  String _publisherBottleneck = 'encode';
  final _demand = <String, String>{};
  final _health = <String, String>{};
  Set<String> _codecs = {'vp8'};
  Timer? _timer;
  bool _busy = false;
  bool _tickAgain = false;
  bool _closed = false;
  int _ticks = 0;

  Future<void> start() async {
    _events = room.createListener()
      ..on<TrackPublishedEvent>((event) {
        if (_closed) return;
        if (event.publication.kind == TrackType.AUDIO) {
          unawaited(event.publication.subscribe().catchError((_) {}));
        } else {
          unawaited(tick());
        }
      })
      ..on<ParticipantConnectedEvent>((_) => unawaited(tick()))
      ..on<ParticipantDisconnectedEvent>((_) => unawaited(tick()))
      ..on<DataReceivedEvent>((event) {
        if (_closed ||
            event.topic != topic ||
            event.participant == null ||
            event.data.length > 8192) {
          return;
        }
        try {
          final value = jsonDecode(utf8.decode(event.data));
          if (value is! Map<String, dynamic> ||
              value['v'] != 1 ||
              value['codecs'] is! List) {
            return;
          }
          final codecs = (value['codecs'] as List)
              .whereType<String>()
              .where((c) => {'vp8', 'h264', 'av1'}.contains(c))
              .toList();
          _peers[event.participant!.identity] =
              (DateTime.now(), {...value, 'codecs': codecs});
        } catch (_) {/* Ignore malformed peer hints. */}
      });
    try {
      final caps = await rtc.getRtpReceiverCapabilities('video');
      _codecs = caps.codecs
              ?.map((c) => c.mimeType.toLowerCase().split('/').last)
              .where((c) => {'vp8', 'h264', 'av1'}.contains(c))
              .toSet() ??
          {'vp8'};
      if (_codecs.isEmpty) _codecs = {'vp8'};
    } catch (_) {
      // Unknown decode support keeps the established single-track fallback.
    }
    if (_closed) return;
    await tick();
    if (!_closed) {
      _timer =
          Timer.periodic(const Duration(seconds: 2), (_) => unawaited(tick()));
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

  Future<void> _announce() async {
    if (_closed) return;
    await room.localParticipant?.publishData(
        utf8.encode(jsonEncode({
          'v': 1,
          'codecs': _codecs.toList(),
          'demand': _demand,
          'health': _health,
        })),
        topic: topic,
        reliable: true);
  }

  Future<void> tick() async {
    if (_closed || room.connectionState != ConnectionState.connected) return;
    if (_busy) {
      _tickAgain = true;
      return;
    }
    _busy = true;
    try {
      await _subscriptions();
      if (_closed) return;
      await _publishVariants();
      if (_closed) return;
      await _senders();
      onNotice(
          _publisher.level >= 2 || _receivers.values.any((e) => e.level >= 2)
              ? 'Video quality reduced to keep this call stable.'
              : null);
      if (_ticks++ % 5 == 0) await _announce();
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
    for (final participant in room.remoteParticipants.values.toList()) {
      for (final audio in participant.audioTrackPublications) {
        if (_closed) return;
        await audio.subscribe();
      }
      final groups =
          <TrackSource, List<RemoteTrackPublication<RemoteVideoTrack>>>{};
      for (final publication in participant.videoTrackPublications) {
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
        String codec(RemoteTrackPublication publication) => managed.isEmpty
            ? publication.mimeType.toLowerCase().split('/').last
            : publication.name.split(':').last;
        final requested = _demand[key];
        var selected = choices.where((p) => codec(p) == requested).firstOrNull;
        if ((requested != null && requested != 'av1') || evidence.level > 0) {
          selected ??= choices
              .where((p) => codec(p) != 'av1' && _codecs.contains(codec(p)))
              .firstOrNull;
        }
        selected ??= choices
            .where((p) =>
                codec(p) == 'av1' &&
                _codecs.contains('av1') &&
                evidence.level == 0)
            .firstOrNull;
        selected ??= choices
            .where((p) => codec(p) == 'vp8' && _codecs.contains('vp8'))
            .firstOrNull;
        selected ??=
            choices.where((p) => _codecs.contains(codec(p))).firstOrNull;
        // Legacy publications may omit codec metadata before subscription.
        selected ??= managed.isEmpty ? choices.firstOrNull : null;
        if (selected == null) continue;
        for (final publication in entry.value) {
          if (publication != selected) await publication.unsubscribe();
        }
        await selected.subscribe();
        await selected.setVideoQuality(evidence.level == 0
            ? VideoQuality.HIGH
            : evidence.level == 1
                ? VideoQuality.MEDIUM
                : VideoQuality.LOW);
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
        if (previous == null) continue;
        final reason = videoReceiverHealth(current, previous);
        final changed = evidence.observe(reason, DateTime.now());
        if (reason == null) continue;
        _health[key] = reason;
        if (changed) {
          await selected.setVideoQuality(evidence.level == 0
              ? VideoQuality.HIGH
              : evidence.level == 1
                  ? VideoQuality.MEDIUM
                  : VideoQuality.LOW);
          // AV1 hardware says nothing about spatial layers. A fallback simulcast
          // publication offers a real lower-resolution encode when available.
          final fallback = choices
              .where((p) => codec(p) != 'av1' && _codecs.contains(codec(p)))
              .firstOrNull;
          if (evidence.level > 0) {
            final fallbackCodec = fallback == null
                ? ['vp8', 'h264'].where(_codecs.contains).firstOrNull
                : codec(fallback);
            // Demand may create a publication which does not exist yet. Keep
            // receiving the current AV1 track until the publisher provides it.
            if (fallbackCodec != null) _demand[key] = fallbackCodec;
          }
          if (evidence.level == 0) _demand.remove(key);
          await _announce();
        }
      }
    }
    _previous.removeWhere(
        (key, _) => key.startsWith('r:') && !reportKeys.contains(key));
    _demand.removeWhere((key, _) => !active.contains(key));
    _health.removeWhere((key, _) => !active.contains(key));
    _receivers.removeWhere((key, _) => !active.contains(key));
  }

  bool _actualHardware(Map<String, dynamic> stats) {
    if (stats['powerEfficientEncoder'] == false) return false;
    final implementation =
        '${stats['encoderImplementation'] ?? ''}'.toLowerCase();
    return !['libaom', 'libvpx', 'software', 'svt-av1']
            .any(implementation.contains) &&
        _hardwareNames.any(implementation.contains) &&
        (!implementation.contains('videotoolbox') ||
            stats['powerEfficientEncoder'] == true);
  }

  Future<bool> _hardwareSupports(LocalVideoTrack track, String codec) async {
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
    final key = '$codec/$width/$height/$fps/$bitrate';
    if (_hardware.containsKey(key)) return _hardware[key]!;
    try {
      final result =
          await _native.invokeMethod<Object>('kaedeVideoHardwareSettings', {
        'codec': codec,
        'width': width,
        'height': height,
        'fps': fps,
        'bitrate': bitrate,
      }).timeout(const Duration(seconds: 3));
      if (result is Map &&
          result['hardware'] == true &&
          result['implementation'] is String) {
        _hardwareNames.add((result['implementation'] as String).toLowerCase());
        return _hardware[key] = true;
      }
      return _hardware[key] = false;
    } catch (_) {
      return _hardware[key] = false;
    }
  }

  Future<void> _publishVariants() async {
    if (kIsWeb ||
        !{TargetPlatform.android, TargetPlatform.iOS, TargetPlatform.macOS}
            .contains(defaultTargetPlatform)) {
      return;
    }
    final local = room.localParticipant;
    if (local == null) return;
    final now = DateTime.now();
    _peers.removeWhere((identity, hint) =>
        !room.remoteParticipants.containsKey(identity) ||
        now.difference(hint.$1).inSeconds > 25);
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
        _variants.remove(source);
        if (local.trackPublications.containsKey(existing.$1.sid)) {
          await local.removePublishedTrack(existing.$1.sid);
        }
        existing = null;
      }
      if (original == null || track == null || original.muted) continue;
      final sourceName =
          source == TrackSource.screenShareVideo ? 'screen_share' : 'camera';
      final demandKey = '${local.identity}/$sourceName';
      final viewers = room.remoteParticipants.keys.toList();
      bool supports(String identity, String codec) =>
          (_peers[identity]?.$2['codecs'] as List?)?.contains(codec) == true;
      String wanted(String identity) {
        final hints = _peers[identity]?.$2;
        final demand = hints?['demand'];
        final request = demand is Map ? demand[demandKey] : null;
        return request is String && supports(identity, request)
            ? request
            : supports(identity, 'av1')
                ? 'av1'
                : 'vp8';
      }

      final allH264 = viewers.isNotEmpty &&
          viewers.every((identity) => supports(identity, 'h264'));
      final preferredFallback = allH264 &&
              await _hardwareSupports(track, 'h264') &&
              !await _hardwareSupports(track, 'vp8')
          ? 'h264'
          : 'vp8';
      final fallbackCodec =
          track.codec ?? room.roomOptions.defaultVideoPublishOptions.videoCodec;
      final compatibilityChange = fallbackCodec == 'h264' && !allH264;
      if (fallbackCodec != preferredFallback &&
          (compatibilityChange ||
              now
                      .difference(_baselineChanged[source] ??
                          DateTime.fromMillisecondsSinceEpoch(0))
                      .inSeconds >=
                  20)) {
        if (_detached.remove(original.sid)) {
          await track.sender?.replaceTrack(track.mediaStreamTrack);
        }
        if (existing != null) {
          _variants.remove(source);
          await local.removePublishedTrack(existing.$1.sid);
          existing = null;
        }
        LocalVideoTrack? replacement;
        LocalTrackPublication<LocalVideoTrack>? replacementPublication;
        try {
          replacement = await track.cloneCapture();
          if (_closed ||
              original.muted ||
              !local.trackPublications.containsKey(original.sid)) {
            await replacement.stop();
            continue;
          }
          replacementPublication = await local.publishVideoTrack(replacement,
              publishOptions: publishOptions(room, source, quality()).copyWith(
                  name: '$topic:$sourceName:$preferredFallback',
                  videoCodec: preferredFallback));
          if (_closed ||
              replacement.codec != preferredFallback ||
              original.muted ||
              !local.trackPublications.containsKey(original.sid)) {
            await local.removePublishedTrack(replacementPublication.sid);
            continue;
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
        }
      }
      if (original == null || track == null) continue;
      final needsAv1 = viewers.any((identity) => wanted(identity) == 'av1');
      String? variantCodec;
      if (_publisher.level < 2 &&
          needsAv1 &&
          !(_failedUntil[source]?.isAfter(now) ?? false) &&
          await _hardwareSupports(track, 'av1')) {
        variantCodec = 'av1';
      }
      if (existing != null && existing.$1.track?.codec != variantCodec) {
        if (_detached.remove(original.sid)) {
          await track.sender?.replaceTrack(track.mediaStreamTrack);
        }
        _variants.remove(source);
        await local.removePublishedTrack(existing.$1.sid);
        existing = null;
      }
      if (variantCodec != null &&
          existing == null &&
          !(_failedUntil[source]?.isAfter(now) ?? false)) {
        LocalVideoTrack? clone;
        try {
          clone = await track.cloneCapture();
          if (_closed ||
              original.muted ||
              !local.trackPublications.containsKey(original.sid)) {
            await clone.stop();
            continue;
          }
          final options = publishOptions(room, source, quality()).copyWith(
              name: '$topic:$sourceName:$variantCodec',
              videoCodec: variantCodec,
              simulcast: variantCodec != 'av1',
              scalabilityMode: 'L1T1',
              backupVideoCodec: const BackupVideoCodec(enabled: false));
          final publication =
              await local.publishVideoTrack(clone, publishOptions: options);
          if (_closed ||
              clone.codec != variantCodec ||
              original.muted ||
              !local.trackPublications.containsKey(original.sid)) {
            _failedUntil[source] = now.add(const Duration(seconds: 60));
            await local.removePublishedTrack(publication.sid);
            continue;
          }
          _variants[source] =
              existing = (publication, track.mediaStreamTrack.id);
        } catch (_) {
          await clone?.stop();
          _failedUntil[source] = now.add(const Duration(seconds: 60));
        }
      }
      final allUseVariant = existing != null &&
          viewers.isNotEmpty &&
          viewers.every((identity) =>
              supports(identity, variantCodec!) &&
              (variantCodec == 'h264' || wanted(identity) == variantCodec));
      if (allUseVariant && !_detached.contains(original.sid)) {
        await track.sender?.replaceTrack(null);
        _detached.add(original.sid);
      } else if (!allUseVariant && _detached.remove(original.sid)) {
        await track.sender?.replaceTrack(track.mediaStreamTrack);
      }
    }
    _detached.removeWhere((sid) => !local.trackPublications.containsKey(sid));
  }

  Future<void> _senders() async {
    final publications =
        room.localParticipant?.videoTrackPublications.toList() ?? [];
    final senderIds =
        publications.map((p) => p.track?.sender?.senderId).toSet();
    _base.removeWhere((key, _) => !senderIds.contains(key));
    _appliedLevel.removeWhere((key, _) => !senderIds.contains(key));
    final activePerSource = <TrackSource, int>{};
    for (final publication in publications) {
      if (!publication.muted && !_detached.contains(publication.sid)) {
        activePerSource.update(publication.source, (count) => count + 1,
            ifAbsent: () => 1);
      }
    }
    String? reason;
    var encodeLoad = 0.0;
    var measuredEncoding = false;
    final reportKeys = <String>{};
    for (final publication in publications) {
      if (_closed) return;
      if (publication.muted || _detached.contains(publication.sid)) continue;
      final sender = publication.track?.sender;
      if (sender == null) continue;
      for (final report in await sender.getStats()) {
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
          if (seconds > 0 && frames > 0 && encoded >= 0) {
            measuredEncoding = true;
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
        if (publication.track?.codec == 'av1' &&
            (report.values['framesEncoded'] as num? ?? 0) >
                (previous?['framesEncoded'] as num? ?? 0)) {
          _unprovedAv1[publication.sid] =
              actualHardware ? 0 : (_unprovedAv1[publication.sid] ?? 0) + 1;
          if (_unprovedAv1[publication.sid]! >= 3) {
            _failedUntil[publication.source] =
                DateTime.now().add(const Duration(seconds: 60));
          }
        }
        final limitation = report.values['qualityLimitationReason'];
        if (limitation == 'cpu') reason = 'encode';
        if (limitation == 'bandwidth' && reason != 'encode') reason = 'upload';
        if (limitation == 'none') reason ??= 'healthy';
      }
    }
    _previous.removeWhere(
        (key, _) => key.startsWith('s:') && !reportKeys.contains(key));
    if (measuredEncoding) {
      if (encodeLoad > .85) {
        reason = 'encode';
      } else {
        reason ??= 'healthy';
      }
    }
    _publisher.observe(reason, DateTime.now());
    if (reason == 'encode' || reason == 'upload') {
      _publisherBottleneck = reason!;
    }
    for (final publication in publications) {
      if (_closed) return;
      if (publication.muted || _detached.contains(publication.sid)) continue;
      final sender = publication.track?.sender;
      final parameters = sender?.parameters;
      final encodings = parameters?.encodings;
      if (sender == null || parameters == null || encodings == null) continue;
      final variantCount = activePerSource[publication.source] ?? 1;
      final budgetVersion = _publisher.level * 10 + variantCount;
      if (_appliedLevel[sender.senderId] == budgetVersion) continue;
      if (_publisher.level == 0 &&
          variantCount == 1 &&
          !_base.containsKey(sender.senderId)) {
        continue;
      }
      final baseline = _base.putIfAbsent(
          sender.senderId,
          () => encodings
              .map((e) =>
                  (e.maxBitrate, e.maxFramerate, e.scaleResolutionDownBy))
              .toList());
      if (baseline.length != encodings.length) continue;
      final text = publication.source == TrackSource.screenShareVideo &&
          quality().screen != ScreenShareQuality.smooth;
      final level = _publisher.level;
      for (var i = 0; i < encodings.length; i++) {
        final original = baseline[i];
        final encoding = encodings[i];
        if (level == 0) {
          encoding.maxBitrate = original.$1 == null
              ? null
              : (original.$1! / variantCount).round();
          encoding.maxFramerate = original.$2;
          encoding.scaleResolutionDownBy = original.$3;
          continue;
        }
        encoding.maxBitrate =
            ((original.$1 ?? 1500000) * pow(.72, level) / variantCount).round();
        encoding.maxFramerate = max(text ? 5 : 12,
            ((original.$2 ?? 30) * pow(text ? .65 : .85, level)).round());
        // Text keeps pixels while reducing cadence. Camera/motion reduces the
        // combined processing working set of all published video senders.
        encoding.scaleResolutionDownBy = (original.$3 ?? 1) *
            (text || (_publisherBottleneck == 'upload' && level < 2)
                ? 1
                : pow(1.25, level));
      }
      if (await sender.setParameters(parameters)) {
        _appliedLevel[sender.senderId] = budgetVersion;
      }
    }
  }
}
