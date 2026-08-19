import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

typedef GatewaySocketConnector = WebSocketChannel Function(Uri uri);

final class GatewayEvent {
  const GatewayEvent(this.name, this.data, this.sequence);
  final String name;
  final Map<String, Object?> data;
  final int? sequence;
}

enum GatewayConnectionPhase { connecting, connected, reconnecting, offline }

/// User-visible health for the realtime transport. The message is deliberately
/// written here, at the protocol boundary, so WebSocket errors and close
/// reasons never leak implementation details into the UI.
final class GatewayHealth {
  const GatewayHealth(this.phase, {this.message});

  final GatewayConnectionPhase phase;
  final String? message;

  bool get isConnected => phase == GatewayConnectionPhase.connected;
}

String gatewayCloseMessage(int? code) => switch (code) {
      4003 => 'The realtime session expired. Reconnecting…',
      4004 =>
        'Realtime sign-in was rejected. Sign out and back in if this continues.',
      4006 => 'Realtime updates fell out of sync. Refreshing the session…',
      4008 => 'Realtime updates were rate limited. Retrying in 5 seconds…',
      4009 => 'The realtime session timed out. Reconnecting…',
      1000 => 'The realtime connection closed. Reconnecting…',
      _ => 'Realtime updates were interrupted. Reconnecting…',
    };

final class GatewayCloseDetails {
  const GatewayCloseDetails(this.message, {this.retryAfter});

  final String message;
  final Duration? retryAfter;
}

/// Parses the small, structured subset of close metadata that is safe and
/// useful to a user. Arbitrary server close text is never displayed.
GatewayCloseDetails gatewayCloseDetails(int? code, Object? rawReason) {
  Map<String, Object?>? payload;
  if (rawReason is String && rawReason.length <= 2048) {
    try {
      payload = _gatewayObject(jsonDecode(rawReason));
    } on Object {
      // Non-JSON close reasons are intentionally ignored.
    }
  }
  final reasonCode =
      '${payload?['code'] ?? payload?['error'] ?? ''}'.trim().toUpperCase();
  final rawRetry = payload?['retry_after_ms'];
  final retryMs = rawRetry is num &&
          rawRetry.isFinite &&
          rawRetry >= 0 &&
          rawRetry <= 300000
      ? rawRetry.ceil()
      : null;
  final retryAfter = retryMs == null ? null : Duration(milliseconds: retryMs);
  final retryText = retryMs == null
      ? ''
      : ' Retrying in ${max(1, (retryMs / 1000).ceil())} seconds.';
  if (reasonCode == 'RATE_LIMITED' ||
      code == GatewayCloseCode.rateLimited.value &&
          reasonCode != 'SESSION_LIMIT') {
    final effectiveRetry = retryAfter ?? const Duration(seconds: 5);
    return GatewayCloseDetails(
      'Realtime updates were rate limited.${retryText.isEmpty ? ' Retrying in 5 seconds.' : retryText}',
      retryAfter: effectiveRetry,
    );
  }
  if (reasonCode == 'SESSION_LIMIT') {
    final effectiveRetry = retryAfter ?? const Duration(seconds: 30);
    return GatewayCloseDetails(
      'This account has too many active realtime sessions. Close Kaede on another device, then retry.${retryText.isEmpty ? ' Kaede will retry in 30 seconds.' : retryText}',
      retryAfter: effectiveRetry,
    );
  }
  return GatewayCloseDetails(
    gatewayCloseMessage(code),
    retryAfter: code == GatewayCloseCode.rateLimited.value
        ? const Duration(seconds: 5)
        : null,
  );
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

/// Renews early enough to absorb ordinary mobile main-isolate scheduling
/// stalls instead of using the server's entire heartbeat grace period.
Duration gatewayHeartbeatCadence(int advertisedMilliseconds) => Duration(
      milliseconds: max(1000, advertisedMilliseconds * 3 ~/ 4),
    );

final class GatewayClient {
  GatewayClient({
    required this.tokens,
    GatewaySocketConnector? socketConnector,
    this.transportReadyTimeout = const Duration(seconds: 12),
    this.sessionReadyTimeout = const Duration(seconds: 15),
    this.transportCloseTimeout = const Duration(seconds: 1),
    this.recoveryWatchdogInterval = const Duration(seconds: 30),
  }) : _socketConnector =
            socketConnector ?? ((uri) => WebSocketChannel.connect(uri));

  final Future<SessionTokens?> Function() tokens;
  final GatewaySocketConnector _socketConnector;
  final Duration transportReadyTimeout;
  final Duration sessionReadyTimeout;
  final Duration transportCloseTimeout;
  final Duration recoveryWatchdogInterval;
  final _events = StreamController<GatewayEvent>.broadcast();
  final _healthEvents = StreamController<GatewayHealth>.broadcast(sync: true);
  WebSocketChannel? _socket;
  StreamSubscription<Object?>? _subscription;
  Timer? _heartbeat;
  Timer? _sessionReadyWatchdog;
  Timer? _recoveryWatchdog;
  SessionTokens? _tokens;
  String? _sessionId;
  int? _sequence;
  int _generation = 0;
  int _reconnectAttempts = 0;
  bool _closed = false;
  bool _awaitingHeartbeatAck = false;
  Future<void>? _reconnecting;
  Future<void>? _foregroundReconnect;
  int _malformedFrames = 0;
  var _health = const GatewayHealth(
    GatewayConnectionPhase.offline,
    message: 'Realtime updates are not connected.',
  );

  static const _maximumMalformedFrames = 3;

  Stream<GatewayEvent> get events => _events.stream;
  Stream<GatewayHealth> get health => _healthEvents.stream;
  GatewayHealth get currentHealth => _health;

  Future<void> connect(SessionTokens tokens) async {
    _tokens = tokens;
    _closed = false;
    final generation = ++_generation;
    _setHealth(
      const GatewayHealth(
        GatewayConnectionPhase.connecting,
        message: 'Connecting realtime updates…',
      ),
    );
    try {
      await _openTransport(tokens, generation);
    } on Object {
      if (!_closed && generation == _generation) {
        _setHealth(const GatewayHealth(
          GatewayConnectionPhase.reconnecting,
          message: 'Could not connect realtime updates. Retrying…',
        ));
        unawaited(_reconnect(
          generation,
          reason: 'Could not connect realtime updates. Retrying…',
        ));
      }
      rethrow;
    }
  }

  /// Replaces a transport attempt that may have been suspended by the mobile
  /// operating system while the app was in the background.
  ///
  /// Dart timers and socket handshakes are not guaranteed to resume promptly
  /// after Android freezes a process. A fresh foreground attempt is therefore
  /// generation-fenced from the old supervisor instead of waiting for its
  /// stale backoff timer. The controller calls this only on a real background
  /// to foreground transition, so even a socket whose health still appears
  /// connected is replaced after Android may have suspended it.
  Future<void> reconnectAfterForeground(SessionTokens tokens) {
    if (_foregroundReconnect case final pending?) return pending;
    late final Future<void> pending;
    pending = _restartAfterForeground(tokens).whenComplete(() {
      if (identical(_foregroundReconnect, pending)) {
        _foregroundReconnect = null;
      }
    });
    _foregroundReconnect = pending;
    return pending;
  }

  Future<void> _restartAfterForeground(SessionTokens tokens) async {
    _reconnecting = null;
    _reconnectAttempts = 0;
    await connect(tokens);
    if (_health.isConnected) return;
    // Keep the foreground operation shared until the replacement transport is
    // actually identified/resumed. WebSocketChannel.ready only means the HTTP
    // upgrade succeeded; treating it as completion lets a second lifecycle
    // callback tear the socket down before HELLO/READY arrives.
    try {
      await health
          .firstWhere((health) => health.isConnected)
          .timeout(sessionReadyTimeout + const Duration(seconds: 1));
    } on TimeoutException {
      if (!_closed && !_health.isConnected) {
        unawaited(_reconnect(
          _generation,
          reason: 'Realtime updates did not respond. Retrying…',
        ));
      }
    }
  }

  Future<void> _openTransport(SessionTokens tokens, int generation) async {
    await _disconnectTransport();
    if (_closed || generation != _generation) return;
    final uri = Uri(
      scheme: 'wss',
      host: tokens.instance.value,
      path: '/gateway',
      queryParameters: const <String, String>{
        'v': '$protocolVersion',
        'encoding': 'json'
      },
    );
    final socket = _socketConnector(uri);
    _socket = socket;
    try {
      await socket.ready.timeout(transportReadyTimeout);
    } on Object {
      if (generation == _generation) await _disconnectTransport();
      rethrow;
    }
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
      onDone: () {
        final close = gatewayCloseDetails(
          socket.closeCode,
          socket.closeReason,
        );
        unawaited(_reconnect(
          generation,
          reason: close.message,
          minimumDelay: close.retryAfter,
        ));
      },
      onError: (_) => unawaited(_reconnect(generation,
          reason: 'Could not reach realtime updates. Retrying…')),
      cancelOnError: true,
    );
    _sessionReadyWatchdog?.cancel();
    _sessionReadyWatchdog = Timer(sessionReadyTimeout, () {
      if (_closed || generation != _generation || _health.isConnected) return;
      unawaited(_reconnect(
        generation,
        reason: 'Realtime updates did not respond. Retrying…',
      ));
    });
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
    _recoveryWatchdog?.cancel();
    _recoveryWatchdog = null;
    await _disconnectTransport();
    await _events.close();
    await _healthEvents.close();
  }

  Future<void> disconnect() async {
    _closed = true;
    _generation += 1;
    _recoveryWatchdog?.cancel();
    _recoveryWatchdog = null;
    _tokens = null;
    _sessionId = null;
    _sequence = null;
    _awaitingHeartbeatAck = false;
    _reconnecting = null;
    _foregroundReconnect = null;
    await _disconnectTransport();
    _setHealth(const GatewayHealth(GatewayConnectionPhase.offline));
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
      // The gateway permits only a small scheduling grace after its advertised
      // interval. Mobile main isolates can easily be delayed longer than that
      // while waking, decoding media, or changing networks, so renew at 75%
      // of the interval and send once immediately after authentication.
      final heartbeatCadence = gatewayHeartbeatCadence(interval);
      _heartbeat = Timer.periodic(heartbeatCadence, (_) {
        if (_awaitingHeartbeatAck) {
          unawaited(_reconnect(
            generation,
            reason: 'Realtime updates stopped responding. Reconnecting…',
          ));
          return;
        }
        _awaitingHeartbeatAck = true;
        _send(GatewayOp.heartbeat, _sequence);
      });
      final currentTokens = _tokens;
      if (currentTokens == null) {
        await _reconnect(
          generation,
          reason: 'The realtime session is unavailable. Reconnecting…',
        );
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
      _awaitingHeartbeatAck = true;
      _send(GatewayOp.heartbeat, _sequence);
      return;
    }
    if (op == GatewayOp.heartbeatAck.value) {
      _awaitingHeartbeatAck = false;
      return;
    }
    if (op == GatewayOp.reconnect.value) {
      await _reconnect(
        generation,
        reason: 'The server requested a realtime reconnect…',
      );
      return;
    }
    if (op == GatewayOp.invalidSession.value) {
      _sessionId = null;
      _sequence = null;
      await _reconnect(
        generation,
        reason: 'The realtime session expired. Starting a new session…',
      );
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
          await _reconnect(
            generation,
            reason: 'Realtime updates fell out of sync. Refreshing…',
          );
          return;
        case GatewaySequenceDecision.accept:
          break;
      }
    }
    _sequence = sequence;
    if (name == 'READY') {
      _sessionId = data['session_id']! as String;
      _reconnectAttempts = 0;
      _sessionReadyWatchdog?.cancel();
      _sessionReadyWatchdog = null;
      _setHealth(const GatewayHealth(GatewayConnectionPhase.connected));
    } else if (name == 'RESUMED') {
      _reconnectAttempts = 0;
      _sessionReadyWatchdog?.cancel();
      _sessionReadyWatchdog = null;
      _setHealth(const GatewayHealth(GatewayConnectionPhase.connected));
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
    _setHealth(
      const GatewayHealth(
        GatewayConnectionPhase.reconnecting,
        message:
            'Kaede received repeated invalid realtime updates. Reconnecting safely…',
      ),
    );
    await _socket?.sink.close(4002, 'Malformed gateway payload');
    await _reconnect(
      generation,
      reason:
          'Kaede received repeated invalid realtime updates. Reconnecting safely…',
    );
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
      unawaited(_reconnect(
        _generation,
        reason: 'Could not send a realtime update. Reconnecting…',
      ));
    }
  }

  Future<void> _reconnect(
    int generation, {
    String? reason,
    Duration? minimumDelay,
  }) async {
    if (_closed || generation != _generation) return;
    _setHealth(GatewayHealth(
      GatewayConnectionPhase.reconnecting,
      message: reason ?? 'Realtime updates were interrupted. Reconnecting…',
    ));
    if (_reconnecting case final pending?) return pending;
    final pending = _runReconnect(generation, minimumDelay: minimumDelay);
    _reconnecting = pending;
    try {
      await pending;
    } finally {
      if (identical(_reconnecting, pending)) _reconnecting = null;
    }
  }

  Future<void> _runReconnect(
    int generation, {
    Duration? minimumDelay,
  }) async {
    var expectedGeneration = generation;
    while (!_closed && expectedGeneration == _generation) {
      final attempt = _reconnectAttempts++;
      if (attempt >= 2) {
        _setHealth(const GatewayHealth(
          GatewayConnectionPhase.offline,
          message:
              'Realtime updates are offline. Messages may be delayed while Kaede keeps retrying.',
        ));
      }
      final ceiling = min(30, 1 << min(attempt, 5));
      final backoff = Duration(
        milliseconds:
            (ceiling * 750) + Random.secure().nextInt(ceiling * 500 + 1),
      );
      final delay = minimumDelay != null && minimumDelay > backoff
          ? minimumDelay
          : backoff;
      minimumDelay = null;
      await Future<void>.delayed(delay);
      if (_closed || expectedGeneration != _generation) return;
      final current = await tokens();
      if (current == null || _closed || expectedGeneration != _generation) {
        return;
      }
      _tokens = current;
      try {
        _setHealth(const GatewayHealth(
          GatewayConnectionPhase.reconnecting,
          message: 'Reconnecting realtime updates…',
        ));
        expectedGeneration = ++_generation;
        await _openTransport(current, expectedGeneration);
        return;
      } on Object {
        // A failed DNS/TLS/WebSocket handshake has no stream callback to
        // schedule another attempt. Keep this single supervisor alive and
        // carry forward the generation created by the failed transport.
        if (_closed || expectedGeneration != _generation) return;
        expectedGeneration = _generation;
      }
    }
  }

  Future<void> _disconnectTransport() async {
    _sessionReadyWatchdog?.cancel();
    _sessionReadyWatchdog = null;
    _heartbeat?.cancel();
    _heartbeat = null;
    _awaitingHeartbeatAck = false;
    // Detach first. Android can freeze a socket while the app is backgrounded,
    // and waiting indefinitely for that stale stream/sink to acknowledge
    // cancellation prevents the replacement connection from ever opening.
    final subscription = _subscription;
    _subscription = null;
    final socket = _socket;
    _socket = null;
    if (subscription != null) {
      try {
        await subscription.cancel().timeout(transportCloseTimeout);
      } on Object {
        // The generation fence already makes callbacks from this abandoned
        // transport inert. Foreground recovery must not wait on its teardown.
      }
    }
    if (socket != null) {
      try {
        await socket.sink.close().timeout(transportCloseTimeout);
      } on Object {
        // The OS/network stack may never finish closing a suspended socket.
      }
    }
  }

  void _syncRecoveryWatchdog() {
    _recoveryWatchdog?.cancel();
    _recoveryWatchdog = null;
    if (_closed || _tokens == null || _health.isConnected) return;
    _recoveryWatchdog = Timer(recoveryWatchdogInterval, () {
      _recoveryWatchdog = null;
      if (_closed || _tokens == null || _health.isConnected) {
        return;
      }
      if (_reconnecting != null) {
        _syncRecoveryWatchdog();
        return;
      }
      // Socket callbacks and delayed futures are not guaranteed to survive a
      // long event-loop stall. If they disappear, a non-connected health state
      // would otherwise remain forever even though the app is still running.
      _reconnectAttempts = 0;
      unawaited(_reconnect(
        _generation,
        reason: 'Restoring realtime updates…',
      ));
    });
  }

  void _setHealth(GatewayHealth health) {
    if (_health.phase == health.phase && _health.message == health.message) {
      _syncRecoveryWatchdog();
      return;
    }
    _health = health;
    _syncRecoveryWatchdog();
    if (!_healthEvents.isClosed) _healthEvents.add(health);
  }
}
