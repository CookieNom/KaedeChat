import 'dart:async';
import 'dart:collection';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart' as rtc;
import 'package:kaede_mobile/src/features/voice/adaptive_video.dart';
import 'package:kaede_mobile/src/features/voice/media_quality.dart';
import 'package:livekit_client/livekit_client.dart';
import 'package:mocktail/mocktail.dart';

class _Room extends Mock implements Room {}

class _Encryption extends Mock implements E2EEManager {}

class _Participant extends Mock implements LocalParticipant {}

class _Video extends Mock implements LocalVideoTrack {}

class _MediaTrack extends Mock implements rtc.MediaStreamTrack {}

class _Sender extends Mock implements rtc.RTCRtpSender {}

class _Publication extends Mock
    implements LocalTrackPublication<LocalVideoTrack> {}

void main() {
  test('initial SDK encodings reserve the compatible ladder before premium RTP',
      () async {
    final room = _Room();
    final participant = _Participant();
    final base = _Video();
    final premium = _Video();
    final media = _MediaTrack();
    final publication = _Publication();
    final sender = _Sender();
    when(() => room.roomOptions).thenReturn(const RoomOptions());
    when(() => room.localParticipant).thenReturn(participant);
    when(() => participant.videoTrackPublications).thenReturn([publication]);
    when(() => publication.source).thenReturn(TrackSource.screenShareVideo);
    when(() => publication.muted).thenReturn(false);
    when(() => publication.track).thenReturn(base);
    when(() => base.source).thenReturn(TrackSource.screenShareVideo);
    when(() => base.codec).thenReturn('vp8');
    when(() => base.sender).thenReturn(sender);
    when(() => base.mediaStreamTrack).thenReturn(media);
    when(() => media.getSettings()).thenReturn({'width': 3840, 'height': 2160});
    when(() => premium.source).thenReturn(TrackSource.screenShareVideo);
    final ladder = [
      rtc.RTCRtpEncoding(maxBitrate: 500000, scaleResolutionDownBy: 4),
      rtc.RTCRtpEncoding(maxBitrate: 1500000, scaleResolutionDownBy: 2),
      rtc.RTCRtpEncoding(maxBitrate: 6000000, scaleResolutionDownBy: 1),
    ];
    final parameters = rtc.RTCRtpParameters(encodings: ladder);
    when(() => sender.parameters).thenReturn(parameters);
    final reserved = Completer<bool>();
    when(() => sender.setParameters(parameters))
        .thenAnswer((_) => reserved.future);
    final options = AdaptiveVideo.publishOptions(
            room,
            TrackSource.screenShareVideo,
            const MobileMediaQuality(screen: ScreenShareQuality.source))
        .copyWith(videoCodec: 'h265');
    final incoming = [
      rtc.RTCRtpEncoding(maxBitrate: 8000000, scalabilityMode: 'L1T1')
    ];
    final ready = options.configureEncodings!(premium, incoming, 'h265');
    expect(ladder.map((encoding) => encoding.maxBitrate),
        [250000, 750000, 3000000]);
    expect(ladder.map((encoding) => encoding.scaleResolutionDownBy), [4, 3, 3]);
    expect(incoming.single.maxBitrate, 8000000);
    reserved.complete(true);
    await ready;
    expect(incoming.single.maxBitrate, 4000000);
    expect(incoming.single.scaleResolutionDownBy, 1);
    expect(incoming.single.scalabilityMode, 'L1T1');

    ladder.last.maxBitrate = 8000000;
    when(() => sender.setParameters(parameters)).thenAnswer((_) async => false);
    incoming.single.maxBitrate = 8000000;
    await expectLater(options.configureEncodings!(premium, incoming, 'h265'),
        throwsStateError);
    expect(incoming.single.maxBitrate, 8000000);
    expect(ladder.map((encoding) => encoding.maxBitrate),
        [250000, 750000, 3000000]);
    // A previous cap/SDK rewrite must not become the next ladder's weights.
    ladder[0].maxBitrate = 500000;
    ladder[1].maxBitrate = 250000;
    ladder[2].maxBitrate = 250000;
    when(() => sender.setParameters(parameters)).thenAnswer((_) async => true);
    await options.configureEncodings!(premium, incoming, 'h265');
    expect(
        ladder.map((encoding) => encoding.maxBitrate), [62500, 187500, 750000]);
  });

  test(
      'CPU headroom covers brief publication gaps but never renews unknown evidence',
      () {
    final headroom = VideoCpuHeadroom();
    final now = DateTime.utc(2026);
    expect(headroom.available(now), isFalse);
    headroom.observe(.2, now);
    headroom.observe(null, now.add(const Duration(seconds: 8)));
    expect(headroom.available(now.add(const Duration(seconds: 10))), isTrue);
    expect(headroom.available(now.add(const Duration(seconds: 11))), isFalse);
    headroom.observe(.2, now.add(const Duration(seconds: 12)));
    headroom.observe(.5, now.add(const Duration(seconds: 13)));
    expect(headroom.available(now.add(const Duration(seconds: 13))), isFalse);
    expect(videoEncodeEvidenceComplete(null, {'low'}), isFalse);
    expect(videoEncodeEvidenceComplete(['low', 'high'], {'low'}), isFalse);
    expect(
        videoEncodeEvidenceComplete(['low', 'high'], {'low', 'high'}), isTrue);
    expect(videoEncodeEvidenceComplete([null, null], {''}), isFalse);
    expect(videoEncodeEvidenceComplete([null], {''}), isTrue);
    expect(videoEncodeEvidenceComplete([null], {}), isFalse);
  });

  test('VP9 keeps bandwidth savings while audience and CPU remain safe', () {
    bool allowed(
            {bool active = false,
            bool audience = true,
            bool cpu = true,
            bool upload = false,
            int healthy = 15,
            int level = 0,
            bool lowPower = false}) =>
        useVp9Compatible(
            active: active,
            audienceSupports: audience,
            cpuSpare: cpu,
            uploadConstrained: upload,
            level: level,
            healthySamples: healthy,
            lowPower: lowPower);
    expect(allowed(), isFalse);
    expect(allowed(upload: true, healthy: 14), isFalse);
    expect(allowed(upload: true), isTrue);
    expect(allowed(active: true, healthy: 0), isTrue);
    expect(allowed(active: true, audience: false), isFalse);
    expect(allowed(active: true, cpu: false), isFalse);
    expect(allowed(active: true, level: 1), isFalse);
    expect(allowed(active: true, lowPower: true), isFalse);
  });

  test('dual-cast shares one source ceiling and preserves ladder proportions',
      () {
    final trackCeiling = videoTrackBitrate(8000000, 2, 2);
    expect(trackCeiling, 2000000);
    final limits =
        videoBitrateLimits([500000, 1500000, 6000000], trackCeiling, 0);
    expect(limits, [125000, 375000, 1500000]);
    expect(limits.reduce((a, b) => a + b) * 4, 8000000);
    expect(videoTrackBitrate(2500000, 1, 2), 1250000);
    expect(videoBitrateLimits([null, 500000], 1000000, 0), [750000, 250000]);
    // Per-layer minimums must never inflate a small allocated share.
    expect(videoBitrateLimits([500000, 1500000], 100000, 3), [25000, 75000]);
    expect(
        videoBitrateLimits([500000, 1500000], 2000000, 0), [500000, 1500000]);
    expect(compatibleVideoScale(3840, 2160), 3);
    expect(compatibleVideoScale(2160, 3840), 3);
    expect(compatibleVideoScale(640, 360), 1);
  });

  test(
      'hardware rejection or timeout delays compatible probes without becoming permanent',
      () {
    final cache = VideoHardwareProofs();
    final now = DateTime.utc(2026);
    const h264 = 'h264/1920/1080/30/4500000/L1T1';
    cache.record(h264, false, now);
    expect(cache.cached(h264, now.add(const Duration(seconds: 2))), isFalse);
    expect(cache.cached(h264, now.add(const Duration(seconds: 59))), isFalse);
    expect(cache.cached(h264, now.add(const Duration(minutes: 1))), isNull);
    cache.record(h264, true, now);
    expect(cache.cached(h264, now.add(const Duration(days: 1))), isTrue);
    cache.record('vp8/1280/720/30/2500000/L1T1', true, now);
    cache.clearCodec('h264');
    expect(cache.cached(h264, now), isNull);
    expect(cache.cached('vp8/1280/720/30/2500000/L1T1', now), isTrue);
    expect(VideoPolicy.announceEvery, const Duration(seconds: 4));
    expect(VideoPolicy.announceEvery.inMilliseconds * 2,
        lessThanOrEqualTo(VideoPolicy.hintGrace.inMilliseconds));
  });
  test(
    'codec extension keeps v1 hints compatible and bounds untrusted input',
    () {
      expect(
        parseVideoCodecs({
          'v': 1,
          'codecs': ['vp8', 'h264', 'av1'],
          'extendedCodecs': ['h265', 'vp9', 'future'],
        }),
        {'vp8', 'h264', 'av1', 'h265', 'vp9'},
      );
      expect(
        parseVideoCodecs({
          'v': 1,
          'codecs': ['vp8'],
        }),
        {'vp8'},
      );
      expect(
        parseVideoCodecs({
          'v': 1,
          'codecs': ['vp8', 'h265'],
        }),
        isNull,
      );
      expect(
        parseVideoCodecs({
          'v': 1,
          'codecs': ['vp8'],
          'extendedCodecs': [42],
        }),
        isNull,
      );
      expect(
        parseVideoCodecs({
          'v': 1,
          'codecs': ['vp8'],
          'extendedCodecs': List.filled(9, 'h265'),
        }),
        isNull,
      );
      expect(
        parseVideoCodecs({
          'v': 1,
          'codecs': ['vp8'],
          'extendedCodecs': ['h265'],
          'demand': {'bob/camera': 'h265'},
        }),
        isNull,
      );
      expect(
        parseVideoCodecs({
          'v': 1,
          'codecs': ['vp8'],
          'health': {'bob/camera': 'unknown'},
        }),
        isNull,
      );
      expect(isPremiumVideoCodec('h265'), isTrue);
      expect(isPremiumVideoCodec('vp9'), isFalse);
    },
  );

  test('encrypted H264 requires a matching hint from every viewer', () {
    expect(sharesEncryptedH264Format([]), isFalse);
    expect(sharesEncryptedH264Format([2, 2]), isTrue);
    for (final missingOrDifferent in <Object?>[null, '2', 2.0, 3]) {
      expect(sharesEncryptedH264Format([2, missingOrDifferent]), isFalse);
    }
    expect(
        remoteVideoCodec('video/H264;profile-level-id=42e01f', 'vp8',
            encrypted: true),
        'h264');
    expect(remoteVideoCodec('video/VP8', 'h264', encrypted: true), 'vp8');
    expect(remoteVideoCodec('', 'vp8', encrypted: true), 'vp8');
  });

  test('an encrypted publication cannot negotiate H264 before receiving hints',
      () async {
    final room = _Room();
    when(() => room.roomOptions).thenReturn(const RoomOptions(
      defaultVideoPublishOptions: VideoPublishOptions(videoCodec: 'h264'),
    ));
    when(() => room.e2eeManager).thenReturn(_Encryption());
    when(() => room.remoteParticipants)
        .thenReturn(UnmodifiableMapView(<String, RemoteParticipant>{}));
    const quality = MobileMediaQuality();
    final controller =
        AdaptiveVideo(room, quality: () => quality, onNotice: (_) {});
    final options = AdaptiveVideo.publishOptions(
        room, TrackSource.camera, quality,
        controller: controller);
    expect(options.videoCodec, 'vp8');
    expect(options.name, 'kaede.video.v1:camera:vp8');
    await expectLater(
        options.configureEncodings!(_Video(), [], 'h264'), throwsStateError);
    when(() => room.e2eeManager).thenReturn(null);
    expect(
        AdaptiveVideo.publishOptions(room, TrackSource.camera, quality,
                controller: controller)
            .videoCodec,
        'h264');
  });

  test(
      'large rooms can withdraw H264 without dropping their codec announcement',
      () {
    final feedback = {
      for (int i = 0; i < 65; i++) '${'猫' * 65}-$i/camera': 'healthy'
    };
    final encoded = encodeVideoHints(
        {'vp8', 'av1', 'vp9'}, {'user/camera': 'h264'}, feedback);
    expect(utf8.encode(encoded).length, lessThanOrEqualTo(8192));
    final hint = jsonDecode(encoded) as Map<String, dynamic>;
    expect(hint['encryptedH264'], 2);
    expect(hint['codecs'], ['vp8', 'av1']);
    expect(hint['extendedCodecs'], ['vp9']);
    expect(hint['health'], isEmpty);
    expect(hint['demand'], isEmpty);
    final demands = {
      'legacy/camera': 'h264',
      '猫' * 86: 'vp8',
      for (int i = 0; i < 66; i++) 'peer-$i/camera': 'vp8'
    };
    final compact = jsonDecode(encodeVideoHints({'vp8'}, demands, {}))
        as Map<String, dynamic>;
    final sent = compact['demand'] as Map<String, dynamic>;
    expect(sent.length, 64);
    expect(sent.keys, everyElement(startsWith('peer-')));
  });

  test('premium choice counts overlapping audiences and AV1 wins ties', () {
    expect(
      preferredPremiumVideoCodec({'av1': 3, 'h265': 5}, {'av1', 'h265'}),
      'h265',
    );
    expect(
      preferredPremiumVideoCodec({'av1': 5, 'h265': 5}, {'av1', 'h265'}),
      'av1',
    );
    expect(preferredPremiumVideoCodec({'av1': 5, 'h265': 5}, {'h265'}), 'h265');
    expect(
      preferredPremiumVideoCodec({'av1': 0, 'h265': 0}, {'av1', 'h265'}),
      isNull,
    );
    final backoff = VideoBackoff();
    final now = DateTime.utc(2026);
    backoff.fail('bob/camera/h265', now);
    expect(backoff.blocked('bob/camera/h265', now), isTrue);
    expect(backoff.blocked('bob/camera/av1', now), isFalse);
  });

  test('decoder stalls need actual complete-frame evidence; loss wins', () {
    final previous = <String, dynamic>{
      'framesDecoded': 0,
      'framesReceived': 0,
      'packetsReceived': 0,
      'packetsLost': 0,
    };
    final current = <String, dynamic>{
      'framesDecoded': 0,
      'framesReceived': 30,
      'packetsReceived': 100,
      'packetsLost': 0,
    };
    expect(videoReceiverHealth(current, previous), 'decode');
    expect(
      videoReceiverHealth({...current, 'packetsLost': 20}, previous),
      'download',
    );
    expect(videoReceiverHealth({'framesReceived': 30}, {}), isNull);
    expect(videoReceiverHealth(previous, previous), isNull);
    expect(
      videoReceiverHealth({...current, 'framesDecoded': 30}, previous),
      'healthy',
    );
  });

  test('sustained evidence, cooldown, gradual recovery and unknown stats', () {
    final policy = VideoEvidence();
    final start = DateTime.utc(2026);
    expect(policy.observe('encode', start), isFalse);
    expect(
      policy.observe('healthy', start.add(const Duration(seconds: 2))),
      isFalse,
    );
    for (var second = 4; second <= 8; second += 2) {
      policy.observe('encode', start.add(Duration(seconds: second)));
    }
    expect(policy.level, 1);
    for (var second = 10; second < 28; second += 2) {
      expect(
        policy.observe('upload', start.add(Duration(seconds: second))),
        isFalse,
      );
    }
    expect(
      policy.observe('upload', start.add(const Duration(seconds: 28))),
      isTrue,
    );
    expect(policy.level, 2);
    for (var second = 30; second < 60; second += 2) {
      policy.observe('healthy', start.add(Duration(seconds: second)));
    }
    expect(policy.level, 1);
    for (var second = 60; second < 90; second += 2) {
      policy.observe(null, start.add(Duration(seconds: second)));
    }
    expect(policy.level, 1);
    for (var second = 90; second < 120; second += 2) {
      policy.observe('healthy', start.add(Duration(seconds: second)));
    }
    expect(policy.level, 0);
  });

  test(
    'failed AV1 attempts back off progressively, then stop for the session',
    () {
      final backoff = VideoBackoff();
      final start = DateTime.utc(2026);
      expect(backoff.blocked('bob/camera/av1', start), isFalse);
      backoff.fail('bob/camera/av1', start);
      expect(
        backoff.blocked(
          'bob/camera/av1',
          start.add(const Duration(seconds: 59)),
        ),
        isTrue,
      );
      expect(
        backoff.blocked(
          'bob/camera/av1',
          start.add(const Duration(minutes: 1)),
        ),
        isFalse,
      );
      final second = start.add(const Duration(minutes: 2));
      backoff.fail('bob/camera/av1', second);
      expect(
        backoff.blocked(
          'bob/camera/av1',
          second.add(const Duration(minutes: 4)),
        ),
        isTrue,
      );
      backoff
        ..fail('bob/camera/av1', second)
        ..fail('bob/camera/av1', second);
      expect(
        backoff.blocked('bob/camera/av1', second.add(const Duration(days: 1))),
        isTrue,
      );
      backoff.clear('bob/');
      expect(backoff.blocked('bob/camera/av1', second), isFalse);
    },
  );

  test('thermal and power state raise the floor and withhold software AV1', () {
    final device = VideoDeviceState();
    expect(device.levelFloor, 0);
    expect(device.softwareAv1Allowed, isTrue);
    device.thermal = 2;
    expect(device.levelFloor, 1);
    expect(device.softwareAv1Allowed, isFalse);
    device
      ..thermal = 3
      ..lowPower = true;
    expect(device.levelFloor, 2);
  });
}
