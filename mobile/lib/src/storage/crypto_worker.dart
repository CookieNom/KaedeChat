import 'dart:async';
import 'dart:isolate';

import 'package:cryptography/cryptography.dart';

/// Runs the pure-Dart AES-GCM cache crypto off the UI isolate.
///
/// `package:cryptography`'s [AesGcm] is implemented in pure Dart, so every
/// snapshot/outbox row encrypted or decrypted on the UI isolate costs real
/// frame time on Android. This worker owns one long-lived isolate that
/// performs only the AES-GCM primitive; all envelope formatting (the
/// `v1:nonce:cipher:mac` wire shape), validation and JSON work stay on the
/// caller so the on-disk format and error behavior are unchanged.
///
/// Wire protocol between the isolates:
///   request: `[int id, String op, Object? arg]`
///   reply:   `[int id, bool ok, Object? payload]`
/// where `payload` is the list of (nonce, cipher, mac) triples for `enc*`,
/// a list of UTF-8 byte lists for `dec*`, or an error map on failure.
final class CacheCryptoWorker {
  CacheCryptoWorker._(this._sendPort, this._receivePort, this._isolate);

  /// Spawns the worker isolate and hands it the 256-bit key.
  static Future<CacheCryptoWorker> start(List<int> key) async {
    final ready = Completer<SendPort>();
    final fromWorker = RawReceivePort((Object? message) {
      if (message is SendPort && !ready.isCompleted) {
        ready.complete(message);
      } else if (!ready.isCompleted) {
        ready.completeError(
          StateError('The cache crypto worker could not be started.'),
        );
      }
    });
    late final CacheCryptoWorker worker;
    final toWorker = RawReceivePort((Object? message) {
      worker._onMessage(message);
    });
    final isolate = await Isolate.spawn(
      _workerMain,
      <Object?>[fromWorker.sendPort, toWorker.sendPort, key],
      onError: fromWorker.sendPort,
      onExit: fromWorker.sendPort,
    );
    try {
      final workerSend = await ready.future;
      worker = CacheCryptoWorker._(workerSend, toWorker, isolate);
      return worker;
    } on Object {
      toWorker.close();
      isolate.kill(priority: Isolate.immediate);
      rethrow;
    } finally {
      fromWorker.close();
    }
  }

  final SendPort _sendPort;
  final RawReceivePort _receivePort;
  final Isolate _isolate;
  final Map<int, Completer<Object?>> _pending = <int, Completer<Object?>>{};
  var _nextId = 1;
  var _closed = false;

  /// Encrypts UTF-8 bytes; returns (nonce, cipherText, mac).
  Future<(List<int>, List<int>, List<int>)> encrypt(List<int> utf8) async {
    final payload = await _call('enc', utf8);
    return _triple(payload);
  }

  /// One isolate round-trip for a batch of payloads.
  Future<List<(List<int>, List<int>, List<int>)>> encryptBatch(
    List<List<int>> payloads,
  ) async {
    final result = await _call('encBatch', payloads);
    return (result as List<Object?>).map(_triple).toList(growable: false);
  }

  /// Decrypts a (nonce, cipherText, mac) triple to UTF-8 bytes.
  Future<List<int>> decrypt(
    List<int> nonce,
    List<int> cipherText,
    List<int> mac,
  ) async {
    return (await _call('dec', <Object?>[nonce, cipherText, mac])) as List<int>;
  }

  /// One isolate round-trip for a batch of triples.
  Future<List<List<int>>> decryptBatch(
    List<(List<int>, List<int>, List<int>)> rows,
  ) async {
    final result = await _call(
      'decBatch',
      rows.map((row) => <Object?>[row.$1, row.$2, row.$3]).toList(),
    );
    return (result as List<Object?>).cast<List<int>>().toList(growable: false);
  }

  (List<int>, List<int>, List<int>) _triple(Object? payload) {
    final parts = payload as List<Object?>;
    return (
      parts[0] as List<int>,
      parts[1] as List<int>,
      parts[2] as List<int>
    );
  }

  Future<Object?> _call(String op, Object? arg) {
    if (_closed) {
      return Future<Object?>.error(
        StateError('The cache crypto worker is closed.'),
      );
    }
    final id = _nextId++;
    final completer = Completer<Object?>();
    _pending[id] = completer;
    _sendPort.send(<Object?>[id, op, arg]);
    return completer.future;
  }

  void _onMessage(Object? message) {
    if (message is! List || message.length != 3) return;
    final id = message[0];
    if (id is! int) return;
    final completer = _pending.remove(id);
    if (completer == null || completer.isCompleted) return;
    if (message[1] == true) {
      completer.complete(message[2]);
      return;
    }
    final error = message[2] as Map<Object?, Object?>;
    if (error['type'] == 'FormatException') {
      completer.completeError(
        const FormatException('Unsupported encrypted cache value.'),
      );
    } else {
      completer.completeError(
        StateError('The local cache could not be decrypted.'),
      );
    }
  }

  /// Closes the isolate. Calls in flight settle with [StateError].
  Future<void> close() async {
    if (_closed) return;
    _closed = true;
    final pending = _pending.values.toList();
    _pending.clear();
    try {
      _sendPort.send(<Object?>[0, 'close', null]);
      _receivePort.close();
      _isolate.kill(priority: Isolate.beforeNextEvent);
    } on Object {
      // The isolate may already be gone; nothing left to clean up locally.
    }
    for (final completer in pending) {
      if (!completer.isCompleted) {
        completer.completeError(
          StateError('The cache crypto worker is closed.'),
        );
      }
    }
  }
}

void _workerMain(Object? message) {
  final request = message as List<Object?>;
  final ready = request[0] as SendPort;
  final toMain = request[1] as SendPort;
  final key = SecretKey(request[2] as List<int>);
  final algorithm = AesGcm.with256bits();

  void reply(int id, Object? payload) {
    toMain.send(<Object?>[id, true, payload]);
  }

  void fail(int id, Object error) {
    toMain.send(<Object?>[
      id,
      false,
      <Object?, Object?>{
        'type': error is FormatException ? 'FormatException' : 'StateError',
      },
    ]);
  }

  Future<void> encryptOne(int id, List<int> utf8) async {
    try {
      final box = await algorithm.encrypt(utf8, secretKey: key);
      reply(id, <Object?>[box.nonce, box.cipherText, box.mac.bytes]);
    } on Object catch (error) {
      fail(id, error);
    }
  }

  Future<void> encryptMany(int id, List<List<int>> payloads) async {
    try {
      final results = <Object?>[];
      for (final payload in payloads) {
        final box = await algorithm.encrypt(payload, secretKey: key);
        results.add(<Object?>[box.nonce, box.cipherText, box.mac.bytes]);
      }
      reply(id, results);
    } on Object catch (error) {
      fail(id, error);
    }
  }

  Future<void> decryptOne(int id, List<Object?> triple) async {
    try {
      final box = SecretBox(
        triple[1] as List<int>,
        nonce: triple[0] as List<int>,
        mac: Mac(triple[2] as List<int>),
      );
      reply(id, await algorithm.decrypt(box, secretKey: key));
    } on Object catch (error) {
      fail(id, error);
    }
  }

  Future<void> decryptMany(int id, List<List<Object?>> rows) async {
    try {
      final results = <Object?>[];
      for (final triple in rows) {
        final box = SecretBox(
          triple[1] as List<int>,
          nonce: triple[0] as List<int>,
          mac: Mac(triple[2] as List<int>),
        );
        results.add(await algorithm.decrypt(box, secretKey: key));
      }
      reply(id, results);
    } on Object catch (error) {
      fail(id, error);
    }
  }

  late final RawReceivePort receive;
  receive = RawReceivePort((Object? message) {
    if (message is! List || message.length != 3) return;
    final id = message[0];
    if (id is! int) return;
    final op = message[1];
    final arg = message[2];
    switch (op) {
      case 'enc':
        encryptOne(id, arg as List<int>);
      case 'encBatch':
        encryptMany(id, arg as List<List<int>>);
      case 'dec':
        decryptOne(id, arg as List<Object?>);
      case 'decBatch':
        decryptMany(id, arg as List<List<Object?>>);
      case 'close':
        receive.close();
        Isolate.current.kill(priority: Isolate.beforeNextEvent);
      default:
        fail(id, StateError('Unknown cache crypto operation.'));
    }
  });
  ready.send(receive.sendPort);
}
