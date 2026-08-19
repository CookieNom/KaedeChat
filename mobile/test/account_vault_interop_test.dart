import 'dart:convert';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/e2ee/client.dart';
import 'package:kaede_mobile/src/e2ee/store.dart';

void main() {
  const accountRef = '1@example.com';
  const state = MobileE2EEState(
    accountRef: accountRef,
    deviceId: 'ked_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
    credential: 'credential',
    mlsState: 'mls-state',
    vaultSequence: '1',
    vaultParentChain: mobileZeroVaultChain,
    messageCache: <String, MobileMessageCacheEntry>{
      'Y2lwaGVy': MobileMessageCacheEntry(
        plaintext: 'plaintext',
        authorRef: accountRef,
        messageRef: '2@example.com',
      ),
    },
  );
  const expectedCiphertext =
      'PCCleK2Ar3qve6Wnk4gbDuyj6UCiHjleAkXUxXgRYd9xfMvSzK5_uliGG4j-7ktdpz1Ct3i9xr5g1mtYWaK0r7F9hzuSkGcgXRXrL67OT6l5y4gjNyISKby83405sfjMirkYp4Zt6nnseVrXJgFIHtZFWImAt0D-KJEjpOOKTCKZP2y156OqJ23DcoNQtj_vRYCCQVAzv2j6Pp0bjoGMn-by8HaM3fZclkHx2Knh89fpnqtQ7sUH_w2-qalT_5WyUg3Lu0ycl4h2F5Kn-wCYxaA_uTd8QfuwMNDbhgAJzrsCYpXup2FCZBMGmiSXfBki2_LeEN5R6s1ySV0IqcVw7WOwtcbk-HJFVa8eZt5DTm-iwOj3ovvtzhhBGTRygJhnI2KrPNKqvEX3WhhgKKTgtuCrNi4l8eF091w_OrVQIJq69BhP2QUey5nCSWA4q3wP43ayqv976Co03lRE7t4miytAlZrwJgGkUtyGGezITgr0mHEdh59nk_CKWZOsMitT0eqvt_Ql6afcDfOyjkqjEP_j38A-NSURUvU';
  const expectedVaultDigest = 'AqLF_ssQCwyJ5hsba6wmVQPoqzkzlY0ev9Vh4Cr2e5Y';
  const expectedVaultChainRoot = 'CAEkikOBbzZQ0cRXCHB9tNKIKtLoERyk6okiTTReHcU';

  String encoded(List<int> value) =>
      base64Url.encode(value).replaceAll('=', '');

  Map<String, Object?> envelope([String sequence = '1']) => <String, Object?>{
        'version': 2,
        'cipher': 'AES-256-GCM',
        'sequence': sequence,
        'nonce': 'AAECAwQFBgcICQoL',
        'ciphertext': expectedCiphertext,
      };

  test('derives the exact backend-compatible deterministic device ID',
      () async {
    expect(
      await mobileE2eeDeviceId(
        accountRef,
        Uint8List.fromList(List<int>.generate(32, (index) => index)),
      ),
      'ked_Ua3EHIzejsRFr2B5x-jRRKFBHjX0mi8QvvLsIHXcspE',
    );
    await expectLater(
      mobileE2eeDeviceId('1@EXAMPLE.com', List<int>.filled(32, 0)),
      throwsFormatException,
    );
  });

  test('seals the exact web-compatible AES-GCM account-vault envelope',
      () async {
    final key = SecretKeyData(
      List<int>.generate(32, (index) => index),
      overwriteWhenDestroyed: true,
    );
    try {
      final envelope = await const MobileE2EEStore().sealAccountVault(
        state,
        key,
        nonce: Uint8List.fromList(List<int>.generate(12, (index) => index)),
      );
      expect(envelope, <String, Object?>{
        'version': 2,
        'cipher': 'AES-256-GCM',
        'sequence': '1',
        'nonce': 'AAECAwQFBgcICQoL',
        'ciphertext': expectedCiphertext,
      });
    } finally {
      key.destroy();
    }
  });

  test('opens a web-produced envelope and rejects another account', () async {
    final key = SecretKeyData(
      List<int>.generate(32, (index) => index),
      overwriteWhenDestroyed: true,
    );
    final envelope = <String, Object?>{
      'version': 2,
      'cipher': 'AES-256-GCM',
      'sequence': '1',
      'nonce': 'AAECAwQFBgcICQoL',
      'ciphertext': expectedCiphertext,
    };
    try {
      final opened = await const MobileE2EEStore().openAccountVault(
        accountRef,
        key,
        envelope,
      );
      expect(opened.accountRef, accountRef);
      expect(opened.deviceId, state.deviceId);
      expect(opened.messageCache, state.messageCache);
      await expectLater(
        const MobileE2EEStore().openAccountVault(
          '2@example.com',
          key,
          envelope,
        ),
        throwsA(anything),
      );
    } finally {
      key.destroy();
    }
  });

  test('vault digest and sequence binding match the backend byte protocol',
      () async {
    final digest = await mobileAccountVaultDigest(
      revision: '1',
      envelope: envelope(),
    );
    final digestText = encoded(digest);
    digest.fillRange(0, digest.length, 0);
    expect(digestText, expectedVaultDigest);

    final record = <String, Object?>{
      'revision': '1',
      'envelope': envelope(),
      'digest': digestText,
    };
    await validateMobileVaultRestoreHighWater(
      currentRevision: '1',
      currentDigest: digestText,
      candidate: record,
    );
    await expectLater(
      validateMobileVaultRestoreHighWater(
        currentRevision: '2',
        currentDigest: digestText,
        candidate: record,
      ),
      throwsStateError,
    );
    await expectLater(
      validateMobileVaultRestoreHighWater(
        currentRevision: '1',
        currentDigest: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
        candidate: record,
      ),
      throwsStateError,
    );

    final key = SecretKeyData(
      List<int>.generate(32, (index) => index),
      overwriteWhenDestroyed: true,
    );
    try {
      await expectLater(
        const MobileE2EEStore().openAccountVault(
          accountRef,
          key,
          <String, Object?>{
            'version': 2,
            'cipher': 'AES-256-GCM',
            'sequence': '2',
            'nonce': 'AAECAwQFBgcICQoL',
            'ciphertext': expectedCiphertext,
          },
        ),
        throwsA(anything),
      );
    } finally {
      key.destroy();
    }
  });

  test('vault ancestry chain matches the exact cross-client byte protocol',
      () async {
    final digest = Uint8List.fromList(
      base64Url.decode(base64Url.normalize(expectedVaultDigest)),
    );
    final parent = Uint8List(32);
    try {
      final root = await mobileAccountVaultChainRoot(
        parentChain: parent,
        revision: '1',
        digest: digest,
      );
      try {
        expect(encoded(root), expectedVaultChainRoot);
      } finally {
        root.fillRange(0, root.length, 0);
      }
    } finally {
      parent.fillRange(0, parent.length, 0);
      digest.fillRange(0, digest.length, 0);
    }
  });

  test('retained rollback checkpoint stores only a hashed account label', () {
    const accountHash = 'SjAnenykIpjw4UZ8HRBE3taCN2TRW2M8kmvLjTsEJTo';
    const checkpoint = MobileVaultCheckpoint(
      accountRef: accountRef,
      revision: '1',
      digest: expectedVaultDigest,
      chainRoot: expectedVaultChainRoot,
    );
    final json = checkpoint.toJson(accountHash);
    expect(json, isNot(contains('account_ref')));
    expect(json['account_hash'], accountHash);
    expect(
      MobileVaultCheckpoint.fromJson(
        json,
        accountRef: accountRef,
        accountHash: accountHash,
      ).chainRoot,
      expectedVaultChainRoot,
    );
    expect(
      () => MobileVaultCheckpoint.fromJson(
        json,
        accountRef: accountRef,
        accountHash: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
      ),
      throwsFormatException,
    );
  });

  test('portable schema has exact ordered keys and structured cache entries',
      () {
    expect(state.toPortableJson().keys.toList(), <String>[
      'schema',
      'accountRef',
      'deviceId',
      'credential',
      'mlsState',
      'vaultSequence',
      'vaultParentChain',
      'messageCache',
      'controlCursors',
      'pendingRoomOperations',
    ]);
    expect(
      state.toPortableJson()['messageCache'],
      <String, Object?>{
        'Y2lwaGVy': <String, Object?>{
          'plaintext': 'plaintext',
          'authorRef': accountRef,
          'messageRef': '2@example.com',
        },
      },
    );
    expect(
      () => MobileE2EEState.fromPortableJson(<String, Object?>{
        ...state.toPortableJson(),
        'messageCache': <String, Object?>{'Y2lwaGVy': 'plaintext'},
      }),
      throwsFormatException,
    );
  });

  test('plaintext cache evicts oldest entries to its byte budget', () {
    final cache = <String, MobileMessageCacheEntry>{
      'YQ': const MobileMessageCacheEntry(
        plaintext: 'first',
        authorRef: accountRef,
        messageRef: '2@example.com',
      ),
      'Yg': const MobileMessageCacheEntry(
        plaintext: 'second',
        authorRef: accountRef,
        messageRef: '3@example.com',
      ),
      'Yw': const MobileMessageCacheEntry(
        plaintext: 'third',
        authorRef: accountRef,
        messageRef: '4@example.com',
      ),
    };
    final twoNewestBytes = mobileMessageCacheSerializedBytes(
      <String, MobileMessageCacheEntry>{
        'Yg': cache['Yg']!,
        'Yw': cache['Yw']!,
      },
    );
    trimMobileMessageCache(cache, maximumBytes: twoNewestBytes);
    expect(cache.keys, <String>['Yg', 'Yw']);
    expect(mobileMessageCacheSerializedBytes(cache),
        lessThanOrEqualTo(twoNewestBytes));
  });

  test('password-reset rebase clears authenticated high-water and journals',
      () {
    final rebased = state
        .withPendingVaultWrite('0', envelope())
        .confirmed(
          '1',
          'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
          'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
        )
        .rebasedAfterPasswordReset();
    expect(rebased.vaultSequence, '1');
    expect(rebased.confirmedVaultRevision, isNull);
    expect(rebased.confirmedVaultDigest, isNull);
    expect(rebased.confirmedVaultChainRoot, isNull);
    expect(rebased.vaultParentChain, mobileZeroVaultChain);
    expect(rebased.pendingVaultEnvelope, isNull);
  });

  test('application operation matches the immutable message projection', () {
    final created = KaedeMessage(
      ref: EntityRef.parse('5@example.com'),
      channelRef: EntityRef.parse('9@example.com'),
      authorRef: EntityRef.parse(accountRef),
      createdAt: DateTime.utc(2026),
    );
    validateMobileE2EEMessageProjection(
      created,
      <String, Object?>{'operation': 'create'},
    );
    for (final invalid in <Map<String, Object?>>[
      <String, Object?>{'operation': 'create', 'target_message': null},
      <String, Object?>{
        'operation': 'edit',
        'target_message': created.ref.wire
      },
      <String, Object?>{'operation': 'welcome'},
      <String, Object?>{'operation': 'commit'},
    ]) {
      expect(
        () => validateMobileE2EEMessageProjection(created, invalid),
        throwsFormatException,
      );
    }

    final edited = created.copyWith(editedAt: DateTime.utc(2026, 1, 2));
    validateMobileE2EEMessageProjection(
      edited,
      <String, Object?>{
        'operation': 'edit',
        'target_message': edited.ref.wire,
      },
    );
    expect(
      () => validateMobileE2EEMessageProjection(
        edited,
        <String, Object?>{
          'operation': 'edit',
          'target_message': '6@example.com',
        },
      ),
      throwsFormatException,
    );
  });

  test('local pending-write metadata never enters the portable vault', () {
    final pending = state.withPendingVaultWrite('0', <String, Object?>{
      'version': 2,
      'cipher': 'AES-256-GCM',
      'sequence': '1',
      'nonce': 'AAECAwQFBgcICQoL',
      'ciphertext': expectedCiphertext,
    });
    expect(pending.toJson(), contains('pending_vault_base_revision'));
    expect(pending.toPortableJson(),
        isNot(contains('pending_vault_base_revision')));
    expect(
      pending
          .confirmed(
            '1',
            'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
            'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
          )
          .pendingVaultBaseRevision,
      isNull,
    );
    expect(
      () => MobileE2EEState.fromJson(<String, Object?>{
        ...pending.toJson(),
        'vault_parent_chain': expectedVaultChainRoot,
      }),
      throwsFormatException,
    );
  });

  test('pending writes replay only from their exact remote base revision', () {
    final envelope = <String, Object?>{
      'version': 2,
      'cipher': 'AES-256-GCM',
      'sequence': '8',
      'nonce': 'AAECAwQFBgcICQoL',
      'ciphertext': expectedCiphertext,
    };
    const journalState = MobileE2EEState(
      accountRef: accountRef,
      deviceId: 'ked_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
      credential: 'credential',
      mlsState: 'mls-state',
      vaultSequence: '8',
      vaultParentChain: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
      confirmedVaultRevision: '7',
      confirmedVaultDigest: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
      confirmedVaultChainRoot: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
    );
    final pending = journalState.withPendingVaultWrite('7', envelope);
    Map<String, Object?> remote(
      String revision, [
      Map<String, Object?>? remoteEnvelope,
    ]) =>
        <String, Object?>{
          'revision': revision,
          'envelope': remoteEnvelope ?? envelope,
        };

    expect(
      classifyMobileVaultJournal(pending, remote('7')),
      MobileVaultJournalDisposition.replay,
    );
    expect(
      classifyMobileVaultJournal(pending, remote('8')),
      MobileVaultJournalDisposition.confirmed,
    );
    expect(
      classifyMobileVaultJournal(
          pending,
          remote('8', <String, Object?>{
            ...envelope,
            'ciphertext': 'different',
          })),
      MobileVaultJournalDisposition.conflict,
    );
    expect(
      classifyMobileVaultJournal(pending, remote('9')),
      MobileVaultJournalDisposition.conflict,
    );
    expect(
      classifyMobileVaultJournal(state, remote('7')),
      MobileVaultJournalDisposition.none,
    );
  });

  test('portable state preserves explicit validated control cursors', () {
    const withCursor = MobileE2EEState(
      accountRef: accountRef,
      deviceId: 'ked_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
      credential: 'credential',
      mlsState: 'mls-state',
      vaultSequence: '1',
      vaultParentChain: mobileZeroVaultChain,
      controlCursors: <String, String>{
        '9@example.com': '11@authority.example',
      },
    );
    final restored = MobileE2EEState.fromPortableJson(
      withCursor.toPortableJson(),
    );
    expect(restored.controlCursors, withCursor.controlCursors);
    expect(
      () => MobileE2EEState.fromPortableJson(<String, Object?>{
        ...withCursor.toPortableJson(),
        'controlCursors': <String, String>{
          '9@example.com': 'not-a-composite-ref',
        },
      }),
      throwsFormatException,
    );
  });

  test('portable state round-trips the exact web room-operation journal', () {
    const operationId = 'keo_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';
    const operation = MobilePendingRoomOperation(
      operationId: operationId,
      channelRef: '9@example.com',
      kind: 'activate',
      phase: 'activating',
      policyGeneration: '1',
      groupId: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
      commit: 'AQ',
      welcome: 'Ag',
    );
    const withOperation = MobileE2EEState(
      accountRef: accountRef,
      deviceId: 'ked_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
      credential: 'credential',
      mlsState: 'mls-state',
      vaultSequence: '1',
      vaultParentChain: mobileZeroVaultChain,
      pendingRoomOperations: <String, MobilePendingRoomOperation>{
        operationId: operation,
      },
    );
    expect(
        withOperation.toPortableJson()['pendingRoomOperations'],
        <String, Object?>{
          operationId: <String, Object?>{
            'version': 1,
            'operationId': operationId,
            'channelRef': '9@example.com',
            'kind': 'activate',
            'phase': 'activating',
            'policyGeneration': '1',
            'groupId': 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
            'commit': 'AQ',
            'welcome': 'Ag',
          },
        });
    final restored = MobileE2EEState.fromPortableJson(
      withOperation.toPortableJson(),
    );
    expect(restored.pendingRoomOperations[operationId]?.welcome, 'Ag');

    final invalid = <String, Object?>{
      ...operation.toJson(),
      'unexpected': true,
    };
    expect(
      () => MobileE2EEState.fromPortableJson(<String, Object?>{
        ...withOperation.toPortableJson(),
        'pendingRoomOperations': <String, Object?>{operationId: invalid},
      }),
      throwsFormatException,
    );
  });

  test('control-log pages require ascending non-looping composite cursors', () {
    final channel = EntityRef.parse('9@authority.example');
    Map<String, Object?> control(String id, {String? channelRef}) {
      final target = EntityRef.parse(channelRef ?? channel.wire);
      return <String, Object?>{
        'id': id,
        'origin_domain': 'authority.example',
        'channel_id': target.id.value,
        'channel_domain': target.domain.value,
        'author_id': '5',
        'author_domain': 'example.com',
        'encryption_policy_generation': '1',
        'encryption_epoch': '1',
        'apply': true,
        'room_operation_id': 'keo_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
        'room_operation_domain': 'authority.example',
        'e2ee': <String, Object?>{'operation': 'welcome'},
      };
    }

    final page = parseMobileE2EEControlPage(
      <String, Object?>{
        'controls': <Object?>[control('10'), control('11')],
        'next_after': '11@authority.example',
      },
      after: EntityRef.parse('8@authority.example'),
      channel: channel,
    );
    expect(page.controls.map((item) => item.ref.wire), <String>[
      '10@authority.example',
      '11@authority.example',
    ]);
    expect(page.nextAfter?.wire, '11@authority.example');
    expect(page.controls.map((item) => item.apply), <bool>[true, true]);

    for (final response in <Map<String, Object?>>[
      <String, Object?>{
        'controls': <Object?>[],
        'next_after': '8@authority.example',
      },
      <String, Object?>{
        'controls': <Object?>[control('11'), control('10')],
        'next_after': null,
      },
      <String, Object?>{
        'controls': <Object?>[control('10', channelRef: '7@example.com')],
        'next_after': null,
      },
      <String, Object?>{
        'controls': <Object?>[
          <String, Object?>{...control('10')}..remove('apply'),
        ],
        'next_after': null,
      },
      <String, Object?>{
        'controls': <Object?>[
          <String, Object?>{
            ...control('10'),
            'origin_domain': 'participant.example',
          },
        ],
        'next_after': null,
      },
    ]) {
      expect(
        () => parseMobileE2EEControlPage(
          response,
          after: EntityRef.parse('8@authority.example'),
          channel: channel,
        ),
        throwsFormatException,
      );
    }
  });
}
