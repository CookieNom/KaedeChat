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
