import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/scanned_media.dart';

void main() {
  test('scanned resource retries the authority-scoped commit operation',
      () async {
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

  test('simple scanned media also retries only its qualified commit', () async {
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
}
