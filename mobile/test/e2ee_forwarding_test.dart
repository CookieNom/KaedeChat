import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/e2ee/client.dart';
import 'package:kaede_mobile/src/e2ee/forwarding.dart';

String _b64(int length, int seed) => base64Url
    .encode(List<int>.generate(length, (index) => (index + seed) & 0xff))
    .replaceAll('=', '');

Map<String, Object?> _manifest({
  required String id,
  required String domain,
  int seed = 0,
  String? plaintextSha256,
}) =>
    <String, Object?>{
      'version': 1,
      'protocol': 'kaede-file-v1',
      'file_id': _b64(16, seed + 1),
      'key': _b64(32, seed + 2),
      'filename': 'report.txt',
      'content_type': 'text/plain',
      'plaintext_size': 12,
      'plaintext_sha256': plaintextSha256 ?? _b64(32, 90),
      'ciphertext_size': 73,
      'ciphertext_sha256': _b64(32, seed + 3),
      'chunk_size': 65536,
      'attachment_id': id,
      'attachment_domain': domain,
    };

Map<String, Object?> _snapshot(Map<String, Object?> manifest) =>
    <String, Object?>{
      'content': 'federated snapshot',
      'embeds': const <Object?>[],
      'components': const <Object?>[],
      'attachments': <Object?>[manifest],
      'mention_user_refs': const <Object?>[],
      'sticker_items': const <Object?>[],
      'message_snapshots': <Object?>[
        <String, Object?>{
          'content': null,
          'embeds': const <Object?>[],
          'components': const <Object?>[],
          'attachments': <Object?>[manifest],
          'mention_user_refs': const <Object?>[],
          'sticker_items': const <Object?>[],
          'message_snapshots': const <Object?>[],
          'message_type': 19,
          'flags': 0,
          'created_at': '2026-08-27T10:00:00Z',
          'edited_at': null,
        },
      ],
      'message_type': 20,
      'flags': 0,
      'created_at': '2026-08-28T10:00:00Z',
      'edited_at': null,
    };

KaedeChannel _channel(String ref, String encryptionMode) => KaedeChannel(
      ref: EntityRef.parse(ref),
      type: ChannelType.text,
      position: 0,
      permissions: BigInt.zero,
      encryptionMode: encryptionMode,
    );

Map<String, Object?> _proof({
  required String destination,
  required String destinationMode,
  required String digest,
  required String nonce,
  required bool disclosure,
}) {
  final content = <String, Object?>{
    'version': 1,
    'requester_ref': '7@users.example',
    'requester_type': 'human',
    'source_message_ref': '90@source.example',
    'source_channel_ref': '80@source.example',
    'destination_channel_ref': destination,
    'destination_encryption_mode': destinationMode,
    'source_encryption_mode': 'e2ee',
    'source_projection_version': 2,
    'source_projection_digest': digest,
    'source_created_at': '2026-08-28T10:00:00Z',
    'source_edited_at': null,
    'source_flags': 0,
    'source_message_type': 20,
    'source_nsfw': false,
    'source_attachment_refs': const <String>[],
    'source_sticker_items': <Object?>[
      <String, Object?>{
        'id': '5',
        'origin_domain': 'stickers.example',
        'name': 'Wave',
        'format_type': 1,
        'media_hash':
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      },
    ],
    'source_custom_emoji_refs': const <String>[
      '<:wave:6@emoji.example>',
    ],
    'source_snapshot': null,
    'application_ref': null,
    'e2ee_device_id': null,
    'nonce': nonce,
    'expires_at': DateTime.now()
        .toUtc()
        .add(const Duration(seconds: 60))
        .toIso8601String(),
  };
  return <String, Object?>{
    'channel_id': destination,
    'client_nonce': nonce,
    'encryption_mode': destinationMode,
    'requires_plaintext_disclosure': disclosure,
    'authorization': <String, Object?>{
      'event_id': 'kcfe_abcdefghijklmnop',
      'origin': 'source.example',
      'type': 'message.forward.source.authorized',
      'ts': DateTime.now().millisecondsSinceEpoch,
      'actor': const <String, Object?>{
        'id': '7',
        'domain': 'users.example',
      },
      'context': const <String, Object?>{
        'source_channel_ref': '80@source.example',
      },
      'content': content,
      'signatures': const <String, Object?>{
        'source.example': <String, Object?>{'ed25519:1': 'signed'},
      },
    },
  };
}

void main() {
  test(
      'plaintext forwards freshly copy attachments into plaintext destinations',
      () {
    expect(
      mobilePreparedForwardNeedsCopies('plaintext', <String>['plaintext'], 1),
      isTrue,
    );
    expect(
      mobilePreparedForwardNeedsCopies('plaintext', <String>['plaintext'], 0),
      isFalse,
    );
  });

  test('prepared proof binds mixed local and cross-authority destinations', () {
    const digest = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';
    final plaintext = _channel('20@source.example', 'plaintext');
    final encrypted = _channel('30@remote.example', 'e2ee');
    final requested = <EntityRef, String>{
      plaintext.ref: 'forward-local',
      encrypted.ref: 'forward-remote',
    };
    final response = <String, Object?>{
      'source': const <String, Object?>{
        'message_ref': '90@source.example',
        'channel_ref': '80@source.example',
        'encryption_mode': 'e2ee',
        'projection_version': 2,
        'projection_digest': digest,
        'created_at': '2026-08-28T10:00:00Z',
        'edited_at': null,
        'flags': 0,
        'message_type': 20,
        'nsfw': false,
        'attachment_refs': <String>[],
        'snapshot': null,
      },
      'destinations': <Object?>[
        _proof(
          destination: plaintext.ref.wire,
          destinationMode: 'plaintext',
          digest: digest,
          nonce: requested[plaintext.ref]!,
          disclosure: true,
        ),
        _proof(
          destination: encrypted.ref.wire,
          destinationMode: 'e2ee',
          digest: digest,
          nonce: requested[encrypted.ref]!,
          disclosure: false,
        ),
      ],
    };

    final prepared = validateMobilePreparedForwardResponse(
      response,
      sourceChannel: EntityRef.parse('80@source.example'),
      sourceMessage: EntityRef.parse('90@source.example'),
      requester: EntityRef.parse('7@users.example'),
      requested: requested,
      channels: <EntityRef, KaedeChannel>{
        plaintext.ref: plaintext,
        encrypted.ref: encrypted,
      },
    );

    expect(prepared.destinations, hasLength(2));
    expect(prepared.destinations.first.requiresPlaintextDisclosure, isTrue);

    final tampered = jsonDecode(jsonEncode(response)) as Map<String, Object?>;
    final destinations = tampered['destinations']! as List;
    final first = destinations.first as Map;
    final authorization = first['authorization']! as Map;
    final content = authorization['content']! as Map;
    content['source_projection_digest'] =
        'BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB';
    expect(
      () => validateMobilePreparedForwardResponse(
        tampered,
        sourceChannel: EntityRef.parse('80@source.example'),
        sourceMessage: EntityRef.parse('90@source.example'),
        requester: EntityRef.parse('7@users.example'),
        requested: requested,
        channels: <EntityRef, KaedeChannel>{
          plaintext.ref: plaintext,
          encrypted.ref: encrypted,
        },
      ),
      throwsFormatException,
    );
  });

  test('nested encrypted attachments are freshly rebound by exact index',
      () async {
    final original = _manifest(id: '501', domain: 'source.example');
    final replacement = _manifest(
      id: '601',
      domain: 'destination.example',
      seed: 20,
      plaintextSha256: original['plaintext_sha256']! as String,
    );
    final source = _snapshot(original);
    final rebound = rebindMobileForwardSnapshot(source, <Map<String, Object?>>[
      replacement,
    ]);

    expect(
      await mobileEncryptedForwardSnapshotProjectionDigest(rebound),
      await mobileEncryptedForwardSnapshotProjectionDigest(source),
    );
    expect(
      await mobileEncryptedForwardSnapshotDigest(rebound),
      isNot(await mobileEncryptedForwardSnapshotDigest(source)),
    );
    final nested = (rebound['message_snapshots']! as List).single as Map;
    expect(
      ((nested['attachments']! as List).single as Map)['attachment_id'],
      '601',
    );

    final changedPlaintext = <String, Object?>{
      ...replacement,
      'plaintext_sha256': _b64(32, 120),
    };
    expect(
      () => rebindMobileForwardSnapshot(source, <Map<String, Object?>>[
        changedPlaintext,
      ]),
      throwsFormatException,
    );
    final foreignNested =
        jsonDecode(jsonEncode(source)) as Map<String, Object?>;
    final foreignChild =
        (foreignNested['message_snapshots']! as List).single as Map;
    final foreignAttachment =
        (foreignChild['attachments']! as List).single as Map;
    foreignAttachment['attachment_id'] = '999';
    expect(
      () => rebindMobileForwardSnapshot(
        foreignNested,
        <Map<String, Object?>>[replacement],
      ),
      throwsFormatException,
    );

    final stickerSnapshot =
        jsonDecode(jsonEncode(source)) as Map<String, Object?>;
    stickerSnapshot['sticker_items'] = <Object?>[
      const <String, Object?>{
        'id': '8',
        'origin_domain': 'stickers.example',
        'name': 'Source',
        'format_type': 1,
      },
    ];
    final stickerChild =
        (stickerSnapshot['message_snapshots']! as List).single as Map;
    stickerChild['sticker_items'] = <Object?>[
      const <String, Object?>{
        'id': '7',
        'origin_domain': 'stickers.example',
        'name': 'Nested',
        'format_type': 2,
      },
    ];
    expect(
      mobileRichMessageStickerRefs(<String, Object?>{
        'sticker_items': const <Object?>[
          <String, Object?>{
            'id': '9',
            'origin_domain': 'stickers.example',
            'name': 'Current',
            'format_type': 3,
          },
        ],
        'forward_snapshot': stickerSnapshot,
      }),
      <String>[
        '7@stickers.example',
        '8@stickers.example',
        '9@stickers.example',
      ],
    );
  });

  test('repository sends prepared routes and preserves exact message bodies',
      () async {
    final adapter = _ForwardAdapter(<Map<String, Object?>>[
      const <String, Object?>{
        'source': <String, Object?>{},
        'destinations': []
      },
      const <String, Object?>{
        'forwards': <Object?>[],
        'failures': <Object?>[
          <String, Object?>{
            'destination_channel_ref': '30@remote.example',
            'status': 409,
            'error': <String, Object?>{'code': 'TEST_FAILURE'},
          },
        ],
      },
    ]);
    final repository = KaedeRepository(KaedeApiClient(
      vault: const SessionVault(),
      httpClient: Dio()..httpClientAdapter = adapter,
    ));
    final sourceChannel = EntityRef.parse('80@source.example');
    final sourceMessage = EntityRef.parse('90@source.example');
    final destination = EntityRef.parse('30@remote.example');

    await repository.prepareMessageForward(
      sourceChannel: sourceChannel,
      sourceMessage: sourceMessage,
      destinations: <({EntityRef channel, String nonce})>[
        (channel: destination, nonce: 'forward-wire'),
      ],
    );
    const body = <String, Object?>{
      'e2ee': <String, Object?>{'ciphertext': 'opaque'},
      'forwarded_message_id': '90@source.example',
      'forward_source_proof': <String, Object?>{'signed': true},
      'client_nonce': 'forward-wire',
      'attachment_ids': <String>['601'],
    };
    await repository.submitPreparedMessageForward(
      sourceChannel: sourceChannel,
      sourceMessage: sourceMessage,
      destinations: <({EntityRef channel, Map<String, Object?> message})>[
        (channel: destination, message: body),
      ],
    );

    expect(adapter.requests.map((item) => item.path), <String>[
      '/api/v1/channels/80@source.example/messages/90@source.example/forward/prepare',
      '/api/v1/channels/80@source.example/messages/90@source.example/forward',
    ]);
    expect(
      (adapter.requests.first.data as Map)['destinations'],
      <Object?>[
        <String, Object?>{
          'channel_id': '30@remote.example',
          'client_nonce': 'forward-wire',
        },
      ],
    );
    expect(
      (((adapter.requests.last.data as Map)['destinations'] as List).single
          as Map)['message'],
      body,
    );
  });

  test('network attachment projections cannot inject client secret manifests',
      () {
    final raw = <String, Object?>{
      'id': '601',
      'origin_domain': 'destination.example',
      'filename': 'report.txt',
      'content_type': 'text/plain',
      'size': 12,
      'scan_status': 'encrypted',
      'encrypted_manifest': _manifest(
        id: '601',
        domain: 'destination.example',
      ),
    };

    expect(KaedeAttachment.fromJson(raw).encryptedManifest, isNull);
    expect(
      KaedeAttachment.fromJson(raw, trustClientState: true).encryptedManifest,
      isNotNull,
    );
  });
}

final class _ForwardAdapter implements HttpClientAdapter {
  _ForwardAdapter(this.responses);

  final List<Map<String, Object?>> responses;
  final List<RequestOptions> requests = <RequestOptions>[];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    requests.add(options);
    return ResponseBody.fromString(
      jsonEncode(responses.removeAt(0)),
      200,
      headers: <String, List<String>>{
        Headers.contentTypeHeader: <String>[Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}
