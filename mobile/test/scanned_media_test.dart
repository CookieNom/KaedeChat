import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/scanned_media.dart';
import 'package:kaede_mobile/src/core/errors.dart';

void main() {
  test('retrying supplied resource commit', () async {
    var calls = 0;
    final result = await completeScannedMediaResource<String>(
      commit: () async {
        calls += 1;
        if (calls < 3) {
          return <String, Object?>{
            'status': 'processing',
            'attachment': <String, Object?>{'scan_status': 'pending'},
          };
        }
        return <String, Object?>{
          'application_ref': '9@apps.example',
          'name': 'ready',
        };
      },
      isComplete: (json) => json['application_ref'] != null,
      parse: (json) => json['name']! as String,
      pollInterval: Duration.zero,
    );

    expect(result, 'ready');
    expect(calls, 3);
  });

  test('simple scanned-media commit retry', () async {
    var calls = 0;
    final result = await commitScannedMedia(
      commit: () async {
        calls += 1;
        return calls < 3
            ? <String, Object?>{'scan_status': 'pending'}
            : <String, Object?>{
                'id': '7',
                'origin_domain': 'guild.example',
                'name': 'ready',
              };
      },
      pollInterval: Duration.zero,
    );

    expect(result['name'], 'ready');
    expect(calls, 3);
  });
  test('clean media commits once', () async {
    var calls = 0;
    final result = await commitScannedMedia(
      commit: () async {
        calls += 1;
        return <String, Object?>{'scan_status': 'clean'};
      },
      pollInterval: Duration.zero,
    );
    expect(result['scan_status'], 'clean');
    expect(calls, 1);
  });

  test('scan helpers reject unsafe media and bound pending retries', () async {
    for (final resource in [false, true]) {
      for (final status in ['rejected', 'infected', 'failed', 'pending']) {
        var calls = 0;
        Future<Map<String, Object?>> commit() async {
          calls += 1;
          return resource
              ? <String, Object?>{
                  'attachment': {'scan_status': status}
                }
              : <String, Object?>{'scan_status': status};
        }

        final operation = resource
            ? completeScannedMediaResource<Map<String, Object?>>(
                commit: commit,
                isComplete: (json) => json['id'] != null,
                parse: (json) => json,
                pollInterval: Duration.zero,
                maxPollAttempts: 2,
              )
            : commitScannedMedia(
                commit: commit,
                pollInterval: Duration.zero,
                maxPollAttempts: 2,
              );
        await expectLater(
          operation,
          throwsA(isA<KaedeException>().having(
            (error) => error.code,
            'code',
            status == 'pending'
                ? 'MEDIA_PROCESSING_TIMEOUT'
                : 'MEDIA_PROCESSING_REJECTED',
          )),
        );
        expect(calls, status == 'pending' ? 2 : 1);
      }
    }
  });
}
