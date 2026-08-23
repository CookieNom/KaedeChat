import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';

/// Reconciles REST results, optimistic sends, and gateway echoes.
///
/// A server response may replace an optimistic message with a different
/// snowflake while retaining the client nonce. Composite references remain
/// authoritative for all other messages, so equal numeric snowflakes from
/// different instances never collide.
List<KaedeMessage> mergeMessages(Iterable<KaedeMessage> messages) {
  final byRef = <EntityRef, KaedeMessage>{};
  final nonceRefs = <String, EntityRef>{};
  for (final message in messages) {
    if (message.clientNonce case final nonce? when nonce.isNotEmpty) {
      final prior = nonceRefs[nonce];
      if (prior != null) byRef.remove(prior);
      nonceRefs[nonce] = message.ref;
    }
    byRef[message.ref] = message;
  }
  final result = byRef.values.toList()
    ..sort((left, right) {
      final chronological = left.createdAt.compareTo(right.createdAt);
      if (chronological != 0) return chronological;
      return left.ref.wire.compareTo(right.ref.wire);
    });
  return List<KaedeMessage>.unmodifiable(result);
}

/// Fast path of [mergeMessages] for the single newest incoming message.
///
/// Returns the merged list in O(n) when [incoming] would sort to the end of
/// [current] under the [mergeMessages] comparator and would replace nothing
/// (no ref or nonce collision). Returns null when the full merge is required
/// to preserve replacement and ordering semantics.
List<KaedeMessage>? appendNewestMessage(
  List<KaedeMessage> current,
  KaedeMessage incoming,
) {
  if (current.isEmpty) {
    return List.unmodifiable(<KaedeMessage>[incoming]);
  }
  final nonce = incoming.clientNonce;
  final hasNonce = nonce != null && nonce.isNotEmpty;
  var maxCreated = current.first.createdAt;
  var maxWire = current.first.ref.wire;
  for (final candidate in current) {
    if (candidate.ref == incoming.ref) return null;
    if (hasNonce && candidate.clientNonce == nonce) return null;
    final created = candidate.createdAt;
    if (created.isAfter(maxCreated) ||
        (created == maxCreated && candidate.ref.wire.compareTo(maxWire) > 0)) {
      maxCreated = created;
      maxWire = candidate.ref.wire;
    }
  }
  final created = incoming.createdAt;
  if (created.isBefore(maxCreated) ||
      (created == maxCreated && incoming.ref.wire.compareTo(maxWire) < 0)) {
    return null;
  }
  return List.unmodifiable([...current, incoming]);
}
