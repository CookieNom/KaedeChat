import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/storage/crypto_worker.dart';
import 'package:kaede_mobile/src/storage/local_database.dart';
import 'package:sqflite/sqflite.dart';

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
    final database = _RecordingDatabase();
    final local = await LocalDatabase.openWithDatabase(database);

    await local
        .upsertSnapshots('account', 'messages:channel', <String, Object?>{
      '1@chat.example': <String, Object?>{'content': 'updated'},
    });

    expect(database.transactionRecorder.inserts, hasLength(1));
    expect(
      database.transactionRecorder.inserts.single.conflictAlgorithm,
      ConflictAlgorithm.replace,
    );
  });

  test('trimming to an empty snapshot window clears stale rows', () async {
    final database = _RecordingDatabase();
    final local = await LocalDatabase.openWithDatabase(database);

    await local.trimSnapshotRows(
      'account',
      'messages:channel',
      const <String>[],
    );

    expect(database.deletes, hasLength(1));
    expect(database.deletes.single.table, 'snapshots');
    expect(database.deletes.single.where, 'account_key = ? AND kind = ?');
    expect(
      database.deletes.single.whereArgs,
      <Object?>['account', 'messages:channel'],
    );
  });
}

final class _RecordingDatabase implements Database {
  final transactionRecorder = _RecordingTransaction();
  final List<({String table, String? where, List<Object?>? whereArgs})>
      deletes = <({String table, String? where, List<Object?>? whereArgs})>[];

  @override
  Future<T> transaction<T>(
    Future<T> Function(Transaction txn) action, {
    bool? exclusive,
  }) =>
      action(transactionRecorder);

  @override
  Future<int> delete(
    String table, {
    String? where,
    List<Object?>? whereArgs,
  }) async {
    deletes.add((
      table: table,
      where: where,
      whereArgs: whereArgs == null ? null : List<Object?>.of(whereArgs),
    ));
    return 0;
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

final class _RecordingTransaction implements Transaction {
  final List<
      ({
        String table,
        Map<String, Object?> values,
        ConflictAlgorithm? conflictAlgorithm,
      })> inserts = <({
    String table,
    Map<String, Object?> values,
    ConflictAlgorithm? conflictAlgorithm,
  })>[];

  @override
  Future<int> insert(
    String table,
    Map<String, Object?> values, {
    String? nullColumnHack,
    ConflictAlgorithm? conflictAlgorithm,
  }) async {
    inserts.add((
      table: table,
      values: Map<String, Object?>.of(values),
      conflictAlgorithm: conflictAlgorithm,
    ));
    return 1;
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}
