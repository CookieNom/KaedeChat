import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/refs.dart';

void main() {
  final channel = EntityRef.parse('5@remote.example');

  Map<String, Object?> pin(String id, String pinnedAt) => <String, Object?>{
        'pinned_at': pinnedAt,
        'message': <String, Object?>{
          'id': id,
          'origin_domain': 'remote.example',
          'channel_id': '5',
          'channel_domain': 'remote.example',
          'author_id': '7',
          'author_domain': 'remote.example',
          'content': 'saved',
          'created_at': '2026-08-28T00:00:00Z',
          'attachments': <Object?>[],
        },
      };

  test('current pin pages retain pin time and federation identity', () {
    final page = parsePinnedMessagePage(
      <String, Object?>{
        'items': <Object?>[
          pin('9', '2026-08-28T02:00:00Z'),
          pin('8', '2026-08-28T01:00:00+00:00'),
        ],
        'has_more': true,
      },
      channel: channel,
    );

    expect(page.hasMore, isTrue);
    expect(page.items.map((message) => message.ref.wire),
        <String>['9@remote.example', '8@remote.example']);
    expect(page.items.first.pinned, isTrue);
    expect(
      page.items.first.pinnedAt,
      DateTime.parse('2026-08-28T02:00:00Z'),
    );
  });

  test('pin pages reject wrong-channel and nonadvancing cursors', () {
    final wrongChannel = pin('9', '2026-08-28T01:00:00Z');
    (wrongChannel['message']! as Map<String, Object?>)['channel_id'] = '6';

    expect(
      () => parsePinnedMessagePage(
        <String, Object?>{
          'items': <Object?>[wrongChannel],
          'has_more': false,
        },
        channel: channel,
      ),
      throwsFormatException,
    );
    expect(
      () => parsePinnedMessagePage(
        <String, Object?>{
          'items': <Object?>[pin('9', '2026-08-28T02:00:00Z')],
          'has_more': true,
        },
        channel: channel,
        before: DateTime.parse('2026-08-28T02:00:00Z'),
      ),
      throwsFormatException,
    );
  });

  test('pin pages require aware newest-first timestamps but permit ties', () {
    expect(
      () => parsePinnedMessagePage(
        <String, Object?>{
          'items': <Object?>[pin('9', '2026-08-28T02:00:00')],
          'has_more': false,
        },
        channel: channel,
      ),
      throwsFormatException,
    );
    expect(
      () => parsePinnedMessagePage(
        <String, Object?>{
          'items': <Object?>[
            pin('9', '2026-08-28T01:00:00Z'),
            pin('8', '2026-08-28T02:00:00Z'),
          ],
          'has_more': false,
        },
        channel: channel,
      ),
      throwsFormatException,
    );

    final tied = parsePinnedMessagePage(
      <String, Object?>{
        'items': <Object?>[
          pin('9', '2026-08-28T02:00:00Z'),
          pin('8', '2026-08-28T02:00:00Z'),
        ],
        'has_more': false,
      },
      channel: channel,
    );
    expect(tied.items, hasLength(2));
  });
}
