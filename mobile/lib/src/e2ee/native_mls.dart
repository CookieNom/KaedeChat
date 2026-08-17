import 'dart:convert';
import 'dart:ffi';
import 'dart:io';
import 'dart:typed_data';

import 'package:ffi/ffi.dart';

final class _NativeBuffer extends Struct {
  external Pointer<Uint8> data;

  @UintPtr()
  external int length;
}

typedef _InvokeNative = _NativeBuffer Function(
  Uint64,
  Pointer<Uint8>,
  Size,
  Pointer<Uint8>,
  Size,
);
typedef _InvokeDart = _NativeBuffer Function(
  int,
  Pointer<Uint8>,
  int,
  Pointer<Uint8>,
  int,
);
typedef _FreeNative = Void Function(_NativeBuffer);
typedef _FreeDart = void Function(_NativeBuffer);
typedef _CloseNative = Void Function(Uint64);
typedef _CloseDart = void Function(int);

DynamicLibrary _openLibrary() {
  final override = Platform.environment['KAEDE_E2EE_LIBRARY'];
  if (override != null && override.isNotEmpty) {
    return DynamicLibrary.open(override);
  }
  if (Platform.isIOS || Platform.isMacOS) return DynamicLibrary.process();
  if (Platform.isAndroid) return DynamicLibrary.open('libkaede_e2ee_ffi.so');
  final names = Platform.isWindows
      ? const <String>['kaede_e2ee_ffi.dll']
      : const <String>[
          'libkaede_e2ee_ffi.so',
          '../desktop/target/debug/libkaede_e2ee_ffi.so',
          'desktop/target/debug/libkaede_e2ee_ffi.so',
        ];
  for (final name in names) {
    try {
      return DynamicLibrary.open(name);
    } on ArgumentError {
      // Try the next development location.
    }
  }
  throw StateError('The native Kaede MLS library is unavailable.');
}

final class NativeMlsException implements Exception {
  const NativeMlsException(this.message);

  final String message;

  @override
  String toString() => message;
}

final class NativeMlsProcessed {
  const NativeMlsProcessed(
    this.kind,
    this.application,
    this.aad,
    this.credential,
  );

  final String kind;
  final Uint8List? application;
  final Uint8List? aad;
  final Uint8List? credential;
}

final class NativeMlsPendingCommit {
  const NativeMlsPendingCommit(this.commit, this.welcome);

  final Uint8List commit;
  final Uint8List welcome;
}

final class NativeMlsClient {
  NativeMlsClient._(this._handle);

  static final DynamicLibrary _library = _openLibrary();
  static final _InvokeDart _invoke =
      _library.lookupFunction<_InvokeNative, _InvokeDart>('kaede_e2ee_invoke');
  static final _FreeDart _free =
      _library.lookupFunction<_FreeNative, _FreeDart>('kaede_e2ee_buffer_free');
  static final _CloseDart _close =
      _library.lookupFunction<_CloseNative, _CloseDart>('kaede_e2ee_close');

  final int _handle;
  var _closed = false;

  static NativeMlsClient generate(Uint8List credential) {
    final result = _call(0, 'generate', <String, Object?>{
      'credential': _base64url(credential),
    });
    return NativeMlsClient._(int.parse(result['handle']! as String));
  }

  static NativeMlsClient restore(Uint8List state) {
    final result = _call(0, 'restore', <String, Object?>{
      'state': _base64url(state),
    });
    return NativeMlsClient._(int.parse(result['handle']! as String));
  }

  static Map<String, Object?> _call(
    int handle,
    String method,
    Map<String, Object?> input,
  ) {
    final methodBytes = utf8.encode(method);
    final inputBytes = utf8.encode(jsonEncode(input));
    final methodPointer = calloc<Uint8>(methodBytes.length);
    final inputPointer = calloc<Uint8>(inputBytes.length);
    try {
      methodPointer.asTypedList(methodBytes.length).setAll(0, methodBytes);
      inputPointer.asTypedList(inputBytes.length).setAll(0, inputBytes);
      final output = _invoke(
        handle,
        methodPointer,
        methodBytes.length,
        inputPointer,
        inputBytes.length,
      );
      try {
        final decoded = jsonDecode(
          utf8.decode(output.data.asTypedList(output.length),
              allowMalformed: false),
        );
        if (decoded is! Map<Object?, Object?>) {
          throw const NativeMlsException(
              'The native MLS response was invalid.');
        }
        final response = decoded.map((key, value) => MapEntry('$key', value));
        if (response['ok'] != true) {
          throw NativeMlsException('${response['error'] ?? 'MLS failed.'}');
        }
        final result = response['result'];
        if (result is! Map<Object?, Object?>) {
          throw const NativeMlsException('The native MLS result was invalid.');
        }
        return result.map((key, value) => MapEntry('$key', value));
      } finally {
        _free(output);
      }
    } finally {
      methodPointer
          .asTypedList(methodBytes.length)
          .fillRange(0, methodBytes.length, 0);
      inputPointer
          .asTypedList(inputBytes.length)
          .fillRange(0, inputBytes.length, 0);
      calloc.free(methodPointer);
      calloc.free(inputPointer);
    }
  }

  Map<String, Object?> _request(String method, Map<String, Object?> input) {
    if (_closed) throw const NativeMlsException('The MLS client is closed.');
    return _call(_handle, method, input);
  }

  Uint8List exportState() =>
      _bytes(_request('export_state', const {})['state']);
  Uint8List publicIdentityKey() =>
      _bytes(_request('public_identity_key', const {})['bytes']);
  Uint8List sign(Uint8List input) => _bytes(_request('sign', <String, Object?>{
        'input': _base64url(input),
      })['bytes']);
  Uint8List generateKeyPackage() =>
      _bytes(_request('generate_key_package', const {})['bytes']);

  void createGroup(Uint8List groupId) =>
      _request('create_group', <String, Object?>{
        'group_id': _base64url(groupId),
      });

  NativeMlsPendingCommit addMembers(
    Uint8List groupId,
    List<Uint8List> packages,
  ) {
    final result = _request('add_members', <String, Object?>{
      'group_id': _base64url(groupId),
      'key_packages': packages.map(_base64url).toList(growable: false),
    });
    return NativeMlsPendingCommit(
      _bytes(result['commit']),
      _bytes(result['welcome']),
    );
  }

  void mergePendingCommit(Uint8List groupId) =>
      _request('merge_pending_commit', <String, Object?>{
        'group_id': _base64url(groupId),
      });

  Uint8List joinGroup(Uint8List welcome) =>
      _bytes(_request('join_group', <String, Object?>{
        'welcome': _base64url(welcome),
      })['group_id']);

  bool hasGroup(Uint8List groupId) =>
      _request('has_group', <String, Object?>{
        'group_id': _base64url(groupId),
      })['exists'] ==
      true;

  Uint8List encrypt(Uint8List groupId, Uint8List plaintext, Uint8List aad) =>
      _bytes(_request('encrypt', <String, Object?>{
        'group_id': _base64url(groupId),
        'plaintext': _base64url(plaintext),
        'aad': _base64url(aad),
      })['bytes']);

  NativeMlsProcessed process(Uint8List groupId, Uint8List message) {
    final result = _request('process', <String, Object?>{
      'group_id': _base64url(groupId),
      'message': _base64url(message),
    });
    return NativeMlsProcessed(
      '${result['kind']}',
      result['application'] == null ? null : _bytes(result['application']),
      result['aad'] == null ? null : _bytes(result['aad']),
      result['credential'] == null ? null : _bytes(result['credential']),
    );
  }

  Uint8List memberRoster(Uint8List groupId) =>
      _bytes(_request('member_roster', <String, Object?>{
        'group_id': _base64url(groupId),
      })['bytes']);

  Uint8List exportEpochSecret(
    Uint8List groupId,
    String label,
    Uint8List context,
    int length,
  ) =>
      _bytes(_request('export_epoch_secret', <String, Object?>{
        'group_id': _base64url(groupId),
        'label': label,
        'context': _base64url(context),
        'length': length,
      })['bytes']);

  void close() {
    if (_closed) return;
    _closed = true;
    _close(_handle);
  }
}

String _base64url(List<int> value) =>
    base64Url.encode(value).replaceAll('=', '');

Uint8List _bytes(Object? value) {
  if (value is! String || !RegExp(r'^[A-Za-z0-9_-]+$').hasMatch(value)) {
    throw const NativeMlsException('The native MLS byte result was invalid.');
  }
  final decoded =
      base64Url.decode(value.padRight((value.length + 3) & ~3, '='));
  if (_base64url(decoded) != value) {
    throw const NativeMlsException(
        'The native MLS byte result was non-canonical.');
  }
  return Uint8List.fromList(decoded);
}
