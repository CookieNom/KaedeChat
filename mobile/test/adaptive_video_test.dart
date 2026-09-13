import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/features/voice/adaptive_video.dart';

void main() {
  test('decoder stalls need actual complete-frame evidence; loss wins', () {
    final previous = <String, dynamic>{
      'framesDecoded': 0,
      'framesReceived': 0,
      'packetsReceived': 0,
      'packetsLost': 0
    };
    final current = <String, dynamic>{
      'framesDecoded': 0,
      'framesReceived': 30,
      'packetsReceived': 100,
      'packetsLost': 0
    };
    expect(videoReceiverHealth(current, previous), 'decode');
    expect(videoReceiverHealth({...current, 'packetsLost': 20}, previous),
        'download');
    expect(videoReceiverHealth({'framesReceived': 30}, {}), isNull);
    expect(videoReceiverHealth(previous, previous), isNull);
    expect(videoReceiverHealth({...current, 'framesDecoded': 30}, previous),
        'healthy');
  });

  test('sustained evidence, cooldown, gradual recovery and unknown stats', () {
    final policy = VideoEvidence();
    final start = DateTime.utc(2026);
    expect(policy.observe('encode', start), isFalse);
    expect(policy.observe('healthy', start.add(const Duration(seconds: 2))),
        isFalse);
    for (var second = 4; second <= 8; second += 2) {
      policy.observe('encode', start.add(Duration(seconds: second)));
    }
    expect(policy.level, 1);
    for (var second = 10; second < 28; second += 2) {
      expect(policy.observe('upload', start.add(Duration(seconds: second))),
          isFalse);
    }
    expect(policy.observe('upload', start.add(const Duration(seconds: 28))),
        isTrue);
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
}
