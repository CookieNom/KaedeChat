import 'dart:convert';
import 'dart:math';

import 'package:cryptography/cryptography.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:sqflite/sqflite.dart';

final class LocalDatabase {
  LocalDatabase._(this.database, this._cipher);

  final Database database;
  final _LocalCipher _cipher;

  /// Test-only factory: wraps an injected [Database] (for example a no-op
  /// test double) and skips the path_provider, sqflite and secure-storage
  /// channel setup by generating an ephemeral cipher key in memory.
  @visibleForTesting
  static Future<LocalDatabase> openWithDatabase(Database database) async {
    final random = Random.secure();
    final key = List<int>.generate(32, (_) => random.nextInt(256));
    return LocalDatabase._(database, _LocalCipher(SecretKey(key)));
  }

  static Future<LocalDatabase> open() async {
    final directory = await getApplicationSupportDirectory();
    // Version 1 stored cached message bodies as plaintext. Start with a new
    // encrypted-payload database and remove the legacy cache rather than
    // trying to copy sensitive data through another plaintext transaction.
    final legacyPath = p.join(directory.path, 'kaede-mobile.db');
    if (await databaseExists(legacyPath)) await deleteDatabase(legacyPath);
    final cipher = await _LocalCipher.create();
    final db = await openDatabase(
      p.join(directory.path, 'kaede-mobile-secure.db'),
      version: 2,
      onConfigure: (database) async {
        await database.execute('PRAGMA foreign_keys = ON');
        // journal_mode is a query-style PRAGMA: SQLite returns the mode it
        // actually selected. Android rejects it through execute(), leaving
        // the app on the native launch screen before runApp can be reached.
        await database.rawQuery('PRAGMA journal_mode = WAL');
      },
      onCreate: (database, _) async {
        await database.execute('''
          CREATE TABLE snapshots (
            account_key TEXT NOT NULL,
            kind TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            payload TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (account_key, kind, entity_key)
          )
        ''');
        await database.execute('''
          CREATE TABLE outbox (
            nonce TEXT PRIMARY KEY,
            account_key TEXT NOT NULL,
            channel_ref TEXT NOT NULL,
            payload TEXT NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            last_error TEXT
          )
        ''');
        await database.execute(
          'CREATE INDEX outbox_due_idx ON outbox(account_key, state, next_attempt_at)',
        );
        await database.execute('''
          CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
          )
        ''');
      },
      onUpgrade: (database, oldVersion, _) async {
        if (oldVersion >= 2) return;
        // Version 1 encrypted the message payload but left its destination in
        // plaintext. Encrypt existing destinations in place so a copied app
        // database does not disclose who the user was trying to contact.
        final rows = await database.query(
          'outbox',
          columns: const <String>['nonce', 'channel_ref'],
        );
        for (final row in rows) {
          final channelRef = row['channel_ref']! as String;
          if (channelRef.startsWith('v1:')) continue;
          await database.update(
            'outbox',
            <String, Object?>{
              'channel_ref': await cipher.encryptString(channelRef),
            },
            where: 'nonce = ?',
            whereArgs: <Object?>[row['nonce']],
          );
        }
      },
    );
    return LocalDatabase._(db, cipher);
  }

  Future<void> putSnapshot(
    String accountKey,
    String kind,
    String entityKey,
    Object? payload,
  ) async =>
      database.insert(
        'snapshots',
        <String, Object?>{
          'account_key': accountKey,
          'kind': kind,
          'entity_key': entityKey,
          'payload': await _cipher.encryptJson(payload),
          'updated_at': DateTime.now().millisecondsSinceEpoch,
        },
        conflictAlgorithm: ConflictAlgorithm.replace,
      );

  Future<List<Map<String, Object?>>> snapshots(
      String accountKey, String kind) async {
    final rows = await database.query(
      'snapshots',
      columns: const <String>['payload'],
      where: 'account_key = ? AND kind = ?',
      whereArgs: <Object?>[accountKey, kind],
      orderBy: 'updated_at ASC',
    );
    final values = await Future.wait(
      rows.map((row) => _cipher.decryptJson(row['payload']! as String)),
    );
    return values.whereType<Map<Object?, Object?>>().map((value) {
      return value.map((key, item) => MapEntry('$key', item));
    }).toList();
  }

  Future<void> removeSnapshot(
          String accountKey, String kind, String entityKey) =>
      database.delete(
        'snapshots',
        where: 'account_key = ? AND kind = ? AND entity_key = ?',
        whereArgs: <Object?>[accountKey, kind, entityKey],
      );

  Future<void> clearSnapshots(String accountKey, String kind) =>
      database.delete(
        'snapshots',
        where: 'account_key = ? AND kind = ?',
        whereArgs: <Object?>[accountKey, kind],
      );

  Future<void> replaceSnapshots(
    String accountKey,
    String kind,
    Map<String, Object?> values,
  ) async {
    final now = DateTime.now().millisecondsSinceEpoch;
    final encrypted = <String, String>{};
    for (final entry in values.entries) {
      encrypted[entry.key] = await _cipher.encryptJson(entry.value);
    }
    await database.transaction((transaction) async {
      await transaction.delete(
        'snapshots',
        where: 'account_key = ? AND kind = ?',
        whereArgs: <Object?>[accountKey, kind],
      );
      for (final entry in encrypted.entries) {
        await transaction.insert('snapshots', <String, Object?>{
          'account_key': accountKey,
          'kind': kind,
          'entity_key': entry.key,
          'payload': entry.value,
          'updated_at': now,
        });
      }
    });
  }

  /// Replaces several snapshot kinds in one transaction.
  ///
  /// Navigation state is a single logical snapshot. Updating each list in a
  /// separate transaction allowed an interrupted write to pair a new guild
  /// list with stale identity or preference data on the next offline launch.
  Future<void> replaceSnapshotGroups(
    String accountKey,
    Map<String, Map<String, Object?>> groups,
  ) async {
    final now = DateTime.now().millisecondsSinceEpoch;
    final encrypted = <String, Map<String, String>>{};
    for (final group in groups.entries) {
      final values = <String, String>{};
      for (final entry in group.value.entries) {
        values[entry.key] = await _cipher.encryptJson(entry.value);
      }
      encrypted[group.key] = values;
    }
    await database.transaction((transaction) async {
      for (final group in encrypted.entries) {
        await transaction.delete(
          'snapshots',
          where: 'account_key = ? AND kind = ?',
          whereArgs: <Object?>[accountKey, group.key],
        );
        for (final entry in group.value.entries) {
          await transaction.insert('snapshots', <String, Object?>{
            'account_key': accountKey,
            'kind': group.key,
            'entity_key': entry.key,
            'payload': entry.value,
            'updated_at': now,
          });
        }
      }
    });
  }

  Future<void> clearSnapshotsWithPrefix(String accountKey, String prefix) =>
      database.delete(
        'snapshots',
        where: 'account_key = ? AND kind LIKE ? ESCAPE \'\\\'',
        whereArgs: <Object?>[
          accountKey,
          '${prefix.replaceAll(r'\\', r'\\\\').replaceAll('%', r'\\%').replaceAll('_', r'\\_')}%',
        ],
      );

  Future<void> purgeAccount(String accountKey) async {
    await database.transaction((transaction) async {
      await transaction.delete('snapshots',
          where: 'account_key = ?', whereArgs: <Object?>[accountKey]);
      await transaction.delete('outbox',
          where: 'account_key = ?', whereArgs: <Object?>[accountKey]);
    });
  }

  Future<void> enqueue({
    required String nonce,
    required String accountKey,
    required String channelRef,
    required Map<String, Object?> payload,
  }) async =>
      database.insert(
        'outbox',
        <String, Object?>{
          'nonce': nonce,
          'account_key': accountKey,
          'channel_ref': await _cipher.encryptString(channelRef),
          'payload': await _cipher.encryptJson(payload),
          'state': 'pending',
          'attempts': 0,
          'next_attempt_at': 0,
          'created_at': DateTime.now().millisecondsSinceEpoch,
        },
        conflictAlgorithm: ConflictAlgorithm.ignore,
      );

  Future<List<OutboxItem>> dueOutbox(String accountKey,
      {int limit = 25}) async {
    final rows = await database.query(
      'outbox',
      where:
          "account_key = ? AND state IN ('pending', 'retry') AND next_attempt_at <= ?",
      whereArgs: <Object?>[accountKey, DateTime.now().millisecondsSinceEpoch],
      orderBy: 'created_at ASC',
      limit: limit,
    );
    return Future.wait(rows.map(_outboxItem));
  }

  Future<List<OutboxItem>> outboxForAccount(String accountKey) async {
    final rows = await database.query(
      'outbox',
      where: 'account_key = ?',
      whereArgs: <Object?>[accountKey],
      orderBy: 'created_at ASC',
    );
    return Future.wait(rows.map(_outboxItem));
  }

  Future<void> completeOutbox(String nonce) => database
      .delete('outbox', where: 'nonce = ?', whereArgs: <Object?>[nonce]);

  Future<void> retryOutbox(String nonce, int attempts, String error) async =>
      database.update(
        'outbox',
        <String, Object?>{
          'state': attempts >= 8 ? 'failed' : 'retry',
          'attempts': attempts,
          'next_attempt_at': DateTime.now()
              .add(outboxRetryDelay(attempts))
              .millisecondsSinceEpoch,
          'last_error': await _cipher.encryptString(error),
        },
        where: 'nonce = ?',
        whereArgs: <Object?>[nonce],
      );

  Future<void> failOutbox(String nonce, String error) async => database.update(
        'outbox',
        <String, Object?>{
          'state': 'failed',
          'last_error': await _cipher.encryptString(error),
        },
        where: 'nonce = ?',
        whereArgs: <Object?>[nonce],
      );

  Future<void> retryOutboxNow(String nonce) => database.update(
        'outbox',
        <String, Object?>{
          'state': 'pending',
          'next_attempt_at': 0,
          'last_error': null,
        },
        where: 'nonce = ?',
        whereArgs: <Object?>[nonce],
      );

  Future<void> deleteOutbox(String nonce) => database.delete(
        'outbox',
        where: 'nonce = ?',
        whereArgs: <Object?>[nonce],
      );

  Future<OutboxItem> _outboxItem(Map<String, Object?> row) async {
    final payload = await _cipher.decryptJson(row['payload']! as String);
    final rawError = row['last_error'] as String?;
    return OutboxItem(
      nonce: row['nonce']! as String,
      channelRef: await _cipher.decryptString(row['channel_ref']! as String),
      payload: Map<String, Object?>.from(payload! as Map),
      attempts: row['attempts']! as int,
      state: row['state']! as String,
      createdAt: DateTime.fromMillisecondsSinceEpoch(row['created_at']! as int),
      lastError:
          rawError == null ? null : await _cipher.decryptString(rawError),
    );
  }

  Future<void> close() => database.close();
}

/// Exponential retry bounded at one minute so offline sends neither hammer the
/// home instance nor disappear for an unexpectedly long period after it
/// recovers. This is pure to keep the durable-outbox policy regression tested.
Duration outboxRetryDelay(int attempts) =>
    Duration(seconds: 1 << attempts.clamp(0, 6).toInt());

final class OutboxItem {
  const OutboxItem({
    required this.nonce,
    required this.channelRef,
    required this.payload,
    required this.attempts,
    required this.state,
    required this.createdAt,
    this.lastError,
  });

  final String nonce;
  final String channelRef;
  final Map<String, Object?> payload;
  final int attempts;
  final String state;
  final DateTime createdAt;
  final String? lastError;
}

final class _LocalCipher {
  const _LocalCipher(this._key);

  static const _storage = FlutterSecureStorage(
    aOptions: AndroidOptions(encryptedSharedPreferences: true),
    iOptions: IOSOptions(
      accessibility: KeychainAccessibility.first_unlock_this_device,
      synchronizable: false,
    ),
  );
  static const _storageKey = 'kaede.mobile.cache-key.v1';
  static final _algorithm = AesGcm.with256bits();

  final SecretKey _key;

  static Future<_LocalCipher> create() async {
    final stored = await _storage.read(key: _storageKey);
    late final List<int> bytes;
    if (stored == null) {
      final random = Random.secure();
      bytes = List<int>.generate(32, (_) => random.nextInt(256));
      await _storage.write(key: _storageKey, value: base64UrlEncode(bytes));
    } else {
      bytes = base64Url.decode(stored);
      if (bytes.length != 32) {
        throw StateError('The protected local cache key is invalid.');
      }
    }
    return _LocalCipher(SecretKey(bytes));
  }

  Future<String> encryptJson(Object? value) => encryptString(jsonEncode(value));

  Future<Object?> decryptJson(String value) async {
    return jsonDecode(await decryptString(value));
  }

  Future<String> encryptString(String value) async {
    final box = await _algorithm.encrypt(
      utf8.encode(value),
      secretKey: _key,
    );
    return 'v1:${base64UrlEncode(box.nonce)}:'
        '${base64UrlEncode(box.cipherText)}:'
        '${base64UrlEncode(box.mac.bytes)}';
  }

  Future<String> decryptString(String value) async {
    final pieces = value.split(':');
    if (pieces.length != 4 || pieces.first != 'v1') {
      throw const FormatException('Unsupported encrypted cache value.');
    }
    final box = SecretBox(
      base64Url.decode(pieces[2]),
      nonce: base64Url.decode(pieces[1]),
      mac: Mac(base64Url.decode(pieces[3])),
    );
    return utf8.decode(await _algorithm.decrypt(box, secretKey: _key));
  }
}
