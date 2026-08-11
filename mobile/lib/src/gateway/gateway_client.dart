import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

final class GatewayEvent {
  const GatewayEvent(this.name, this.data, this.sequence);
  final String name;
  final Map<String, Object?> data;
  final int? sequence;
}

const maximumGatewayFrameCharacters = 1024 * 1024;

/// A validated protocol envelope. Keeping decoding pure makes the trust
/// boundary independently fuzzable and prevents malformed peer data from
/// partially mutating the reconnect state.
final class GatewayEnvelope {
  const GatewayEnvelope({
    required this.op,
    required this.data,
    this.sequence,
    this.eventName,
  });

  final int op;
  final Object? data;
  final int? sequence;
  final String? eventName;

  Map<String, Object?> get objectData => data! as Map<String, Object?>;
}

GatewayEnvelope decodeGatewayEnvelope(Object? raw) {
  if (raw is! String) {
    throw const FormatException('Gateway frames must be text');
  }
  if (raw.length > maximumGatewayFrameCharacters) {
    throw const FormatException('Gateway frame is too large');
  }
  final envelope = _gatewayObject(jsonDecode(raw));
  if (envelope == null) {
    throw const FormatException('Gateway envelope must be an object');
  }
  final rawOp = envelope['op'];
  if (rawOp is! num || !rawOp.isFinite || rawOp != rawOp.toInt()) {
    throw const FormatException('Gateway opcode must be an integer');
  }
  final op = rawOp.toInt();
  final rawSequence = envelope['s'];
  if (rawSequence != null &&
      (rawSequence is! num ||
          !rawSequence.isFinite ||
          rawSequence != rawSequence.toInt() ||
          rawSequence < 0)) {
    throw const FormatException('Gateway sequence must be non-negative');
  }
  final sequence = (rawSequence as num?)?.toInt();
  final rawData = envelope['d'];
  if (op == GatewayOp.hello.value) {
    final data = _gatewayObject(rawData);
    if (data == null) {
      throw const FormatException('HELLO data must be an object');
    }
    final rawInterval = data['heartbeat_interval'];
    if (rawInterval != null &&
        (rawInterval is! num ||
            !rawInterval.isFinite ||
            rawInterval != rawInterval.toInt())) {
      throw const FormatException('Heartbeat interval must be an integer');
    }
    final interval = (rawInterval as num?)?.toInt() ?? 41250;
    if (interval < 1000 || interval > 300000) {
      throw const FormatException('Heartbeat interval is outside limits');
    }
    return GatewayEnvelope(op: op, data: data, sequence: sequence);
  }
  if (op != GatewayOp.dispatch.value) {
    return GatewayEnvelope(op: op, data: rawData, sequence: sequence);
  }
  final name = envelope['t'];
  if (name is! String || name.isEmpty || name.length > 128) {
    throw const FormatException('Dispatch name is invalid');
  }
  final data =
      rawData == null ? const <String, Object?>{} : _gatewayObject(rawData);
  if (data == null) {
    throw const FormatException('Dispatch data must be an object');
  }
  if (name == 'READY') {
    final sessionId = data['session_id'];
    if (sessionId is! String || sessionId.isEmpty || sessionId.length > 256) {
      throw const FormatException('READY session identifier is invalid');
    }
  }
  return GatewayEnvelope(
    op: op,
    data: data,
    sequence: sequence,
    eventName: name,
  );
}

Map<String, Object?>? _gatewayObject(Object? value) {
  if (value is! Map) return null;
  try {
    return Map<String, Object?>.from(value);
  } on Object {
    return null;
  }
}

enum GatewaySequenceDecision { accept, duplicate, gap }

/// Classifies a dispatch sequence before it can mutate client state.
///
/// A resumed stream must be contiguous. Duplicate or regressing frames are
/// ignored, while a forward gap forces a clean identify so REST/READY can
/// reconcile state that may otherwise have been silently missed.
GatewaySequenceDecision classifyGatewaySequence(int? previous, int next) {
  if (previous == null || next == previous + 1) {
    return GatewaySequenceDecision.accept;
  }
  if (next <= previous) return GatewaySequenceDecision.duplicate;
  return GatewaySequenceDecision.gap;
}

final class GatewayClient {
  GatewayClient({required this.tokens});

  final Future<SessionTokens?> Function() tokens;
  final _events = StreamController<GatewayEvent>.broadcast();
  WebSocketChannel? _socket;
  StreamSubscription<Object?>? _subscription;
  Timer? _heartbeat;
  SessionTokens? _tokens;
  String? _sessionId;
  int? _sequence;
  int _generation = 0;
  int _reconnectAttempts = 0;
  bool _closed = false;
  bool _awaitingHeartbeatAck = false;
  Future<void>? _reconnecting;
  int _malformedFrames = 0;

  static const _maximumMalformedFrames = 3;

  Stream<GatewayEvent> get events => _events.stream;

  Future<void> connect(SessionTokens tokens) async {
    _tokens = tokens;
    _closed = false;
    try {
      await _openTransport(tokens);
    } on Object {
      if (!_closed) unawaited(_reconnect(_generation));
      rethrow;
    }
  }

  Future<void> _openTransport(SessionTokens tokens) async {
    final generation = ++_generation;
    await _disconnectTransport();
    final uri = Uri(
      scheme: 'wss',
      host: tokens.instance.value,
      path: '/gateway',
      queryParameters: const <String, String>{
        'v': '$protocolVersion',
        'encoding': 'json'
      },
    );
    final socket = WebSocketChannel.connect(uri);
    _socket = socket;
    await socket.ready;
    if (generation != _generation) {
      await socket.sink.close();
      return;
    }
    _malformedFrames = 0;
    _subscription = socket.stream.listen(
      (value) => unawaited(
        _receive(value, generation).onError(
          (_, __) => _rejectMalformedFrame(generation),
        ),
      ),
      onDone: () => _reconnect(generation),
      onError: (_) => _reconnect(generation),
      cancelOnError: true,
    );
  }

  void updatePresence(String status) =>
      _send(GatewayOp.presenceUpdate, <String, Object?>{'status': status});

  void requestMembers(String guildRef, {String query = '', int limit = 100}) =>
      _send(GatewayOp.requestMembers, <String, Object?>{
        'guild_id': guildRef,
        'query': query,
        'limit': limit
      });

  void subscribeMemberList(String guildRef, List<List<int>> ranges) => _send(
      GatewayOp.subscribeMemberList,
      <String, Object?>{'guild_id': guildRef, 'ranges': ranges});

  void voiceState(Map<String, Object?> state) =>
      _send(GatewayOp.voiceStateUpdate, state);

  Future<void> close() async {
    _closed = true;
    _generation += 1;
    await _disconnectTransport();
    await _events.close();
  }

  Future<void> disconnect() async {
    _closed = true;
    _generation += 1;
    _tokens = null;
    _sessionId = null;
    _sequence = null;
    _awaitingHeartbeatAck = false;
    _reconnecting = null;
    await _disconnectTransport();
  }

  Future<void> _receive(Object? raw, int generation) async {
    if (generation != _generation) return;
    late final GatewayEnvelope envelope;
    try {
      envelope = decodeGatewayEnvelope(raw);
    } on Object {
      await _rejectMalformedFrame(generation);
      return;
    }
    final op = envelope.op;
    final sequence = envelope.sequence;
    if (op == GatewayOp.hello.value) {
      final data = envelope.objectData;
      final rawInterval = data['heartbeat_interval'];
      final interval = (rawInterval as num?)?.toInt() ?? 41250;
      _heartbeat?.cancel();
      _awaitingHeartbeatAck = false;
      _heartbeat = Timer.periodic(Duration(milliseconds: interval), (_) {
        if (_awaitingHeartbeatAck) {
          unawaited(_reconnect(generation));
          return;
        }
        _awaitingHeartbeatAck = true;
        _send(GatewayOp.heartbeat, _sequence);
      });
      final currentTokens = _tokens;
      if (currentTokens == null) {
        await _reconnect(generation);
        return;
      }
      final token = currentTokens.accessToken;
      if (_sessionId != null && _sequence != null) {
        _send(GatewayOp.resume, <String, Object?>{
          'token': token,
          'session_id': _sessionId,
          'seq': _sequence
        });
      } else {
        _send(GatewayOp.identify, <String, Object?>{
          'token': token,
          'properties': <String, String>{
            'os': 'mobile',
            'client': 'kaede-mobile'
          },
        });
      }
      return;
    }
    if (op == GatewayOp.heartbeatAck.value) {
      _awaitingHeartbeatAck = false;
      return;
    }
    if (op == GatewayOp.reconnect.value) {
      await _reconnect(generation);
      return;
    }
    if (op == GatewayOp.invalidSession.value) {
      _sessionId = null;
      _sequence = null;
      await _reconnect(generation);
      return;
    }
    if (op != GatewayOp.dispatch.value) return;
    final name = envelope.eventName!;
    final data = envelope.objectData;
    if (sequence == null) {
      await _rejectMalformedFrame(generation);
      return;
    }
    // A new READY starts a new session at the server's authoritative sequence.
    // All other dispatches in an identified/resumed session must be monotonic.
    if (name != 'READY') {
      switch (classifyGatewaySequence(_sequence, sequence)) {
        case GatewaySequenceDecision.duplicate:
          return;
        case GatewaySequenceDecision.gap:
          _events.add(GatewayEvent(
            'GATEWAY_SEQUENCE_GAP',
            <String, Object?>{
              'expected': (_sequence ?? -1) + 1,
              'received': sequence,
            },
            sequence,
          ));
          _sessionId = null;
          _sequence = null;
          await _reconnect(generation);
          return;
        case GatewaySequenceDecision.accept:
          break;
      }
    }
    _sequence = sequence;
    if (name == 'READY') {
      _sessionId = data['session_id']! as String;
      _reconnectAttempts = 0;
    } else if (name == 'RESUMED') {
      _reconnectAttempts = 0;
    }
    _events.add(GatewayEvent(name, data, sequence));
  }

  Future<void> _rejectMalformedFrame(int generation) async {
    _malformedFrames += 1;
    if (_malformedFrames < _maximumMalformedFrames ||
        generation != _generation) {
      return;
    }
    // JSON is the only negotiated encoding. Reconnect instead of continuing
    // to parse an untrusted stream after repeated protocol violations.
    await _socket?.sink.close(4002, 'Malformed gateway payload');
    await _reconnect(generation);
  }

  void _send(GatewayOp op, Object? data) {
    final socket = _socket;
    if (socket == null || _closed) return;
    final payload = jsonEncode(<String, Object?>{'op': op.value, 'd': data});
    try {
      socket.sink.add(payload);
    } on Object {
      // A timer or UI action can race a transport close. Route that failure
      // through the one generation-fenced reconnect supervisor instead of
      // surfacing an unhandled asynchronous exception.
      unawaited(_reconnect(_generation));
    }
  }

  Future<void> _reconnect(int generation) async {
    if (_closed || generation != _generation) return;
    if (_reconnecting case final pending?) return pending;
    final pending = _runReconnect(generation);
    _reconnecting = pending;
    try {
      await pending;
    } finally {
      if (identical(_reconnecting, pending)) _reconnecting = null;
    }
  }

  Future<void> _runReconnect(int generation) async {
    var expectedGeneration = generation;
    while (!_closed && expectedGeneration == _generation) {
      final attempt = _reconnectAttempts++;
      final ceiling = min(30, 1 << min(attempt, 5));
      await Future<void>.delayed(Duration(
        milliseconds:
            (ceiling * 750) + Random.secure().nextInt(ceiling * 500 + 1),
      ));
      if (_closed || expectedGeneration != _generation) return;
      final current = await tokens();
      if (current == null || _closed || expectedGeneration != _generation) {
        return;
      }
      _tokens = current;
      try {
        await _openTransport(current);
        return;
      } on Object {
        // A failed DNS/TLS/WebSocket handshake has no stream callback to
        // schedule another attempt. Keep this single supervisor alive and
        // carry forward the generation created by the failed transport.
        expectedGeneration = _generation;
      }
    }
  }

  Future<void> _disconnectTransport() async {
    _heartbeat?.cancel();
    _heartbeat = null;
    _awaitingHeartbeatAck = false;
    await _subscription?.cancel();
    _subscription = null;
    await _socket?.sink.close();
    _socket = null;
  }
}
