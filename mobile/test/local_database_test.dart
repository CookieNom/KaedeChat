import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/storage/crypto_worker.dart';
import 'package:kaede_mobile/src/storage/local_database.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('cache crypto worker starts and round-trips single and batch payloads',
      () async {
    final worker = await CacheCryptoWorker.start(
      List<int>.generate(32, (index) => index),
    ).timeout(const Duration(seconds: 5));
    addTearDown(worker.close);

    final singlePlain = utf8.encode('single cache value');
    final singleBox = await worker.encrypt(singlePlain);
    expect(
      await worker.decrypt(singleBox.$1, singleBox.$2, singleBox.$3),
      singlePlain,
    );

    final batchPlain = <List<int>>[
      utf8.encode('{"id":1}'),
      utf8.encode('{"id":2,"content":"message"}'),
    ];
    final batchBoxes = await worker.encryptBatch(batchPlain);
    expect(await worker.decryptBatch(batchBoxes), batchPlain);
  });

  test('snapshot upserts replace existing primary-key rows', () async {
    final local = await seededDatabase();
    addTearDown(local.close);
    await local
        .upsertSnapshots('account', 'messages:channel', <String, Object?>{
      '1@chat.example': <String, Object?>{'content': 'updated'},
    });
    expect(await local.snapshots('account', 'messages:channel'), [
      <String, Object?>{'content': 'updated'},
    ]);
    expect(await local.snapshots('other-account', 'messages:channel'), [
      <String, Object?>{'content': 'other account'},
    ]);
    expect(await local.snapshots('account', 'messages:other'), [
      <String, Object?>{'content': 'other kind'},
    ]);
  });

  test('trimming to an empty snapshot window clears stale rows', () async {
    final local = await seededDatabase();
    addTearDown(local.close);
    expect(await local.snapshots('account', 'messages:channel'), isNotEmpty);
    await local.trimSnapshotRows('account', 'messages:channel', const []);
    expect(await local.snapshots('account', 'messages:channel'), isEmpty);
    expect(await local.snapshots('other-account', 'messages:channel'), [
      <String, Object?>{'content': 'other account'},
    ]);
    expect(await local.snapshots('account', 'messages:other'), [
      <String, Object?>{'content': 'other kind'},
    ]);
  });
}

Future<LocalDatabase> seededDatabase() async {
  sqfliteFfiInit();
  final database = await databaseFactoryFfi.openDatabase(inMemoryDatabasePath);
  await database.execute("""
    CREATE TABLE snapshots (
      account_key TEXT NOT NULL, kind TEXT NOT NULL, entity_key TEXT NOT NULL,
      payload TEXT NOT NULL, updated_at INTEGER NOT NULL,
      PRIMARY KEY (account_key, kind, entity_key)
    )
  """);
  final local = await LocalDatabase.openWithDatabase(database);
  for (final entry in [
    ('account', 'messages:channel', 'original'),
    ('other-account', 'messages:channel', 'other account'),
    ('account', 'messages:other', 'other kind'),
  ]) {
    await local.upsertSnapshots(entry.$1, entry.$2, <String, Object?>{
      '1@chat.example': <String, Object?>{'content': entry.$3},
    });
  }
  return local;
}
