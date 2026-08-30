import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/reaction_emoji.dart';
import 'package:kaede_mobile/src/domain/thread_permissions.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';

bool canClearMessageReactions(KaedeChannel channel) =>
    channel.guildRef != null &&
    !channel.archived &&
    channel.allows(Permission.manageMessages);

final class ReconciledReactions {
  const ReconciledReactions({
    required this.counts,
    required this.reactedEmoji,
  });

  final Map<String, int> counts;
  final Set<String> reactedEmoji;
}

/// Applies an authoritative clear-one-group or clear-all reaction result.
ReconciledReactions reconcileClearedReactions(
  Map<String, int> currentCounts,
  Set<String> currentUserReactions, {
  String? emoji,
}) {
  final counts = Map<String, int>.of(canonicalReactionCounts(currentCounts));
  final reacted = Set<String>.of(canonicalReactedEmoji(currentUserReactions));
  if (emoji == null) {
    return const ReconciledReactions(
      counts: <String, int>{},
      reactedEmoji: <String>{},
    );
  }
  final canonical = canonicalReactionEmoji(emoji);
  counts.remove(canonical);
  reacted.remove(canonical);
  return ReconciledReactions(
    counts: Map<String, int>.unmodifiable(counts),
    reactedEmoji: Set<String>.unmodifiable(reacted),
  );
}

typedef ReactionToggleDecision = ({String emoji, bool removing});

/// Resolves aliases before deciding whether a tap adds or removes a reaction.
ReactionToggleDecision? reactionToggleDecision(
  KaedeChannel channel,
  KaedeMessage message,
  String emoji,
) {
  final canonical = tryParseReactionEmoji(emoji)?.value;
  if (canonical == null) return null;
  final counts = canonicalReactionCounts(message.reactionCounts);
  final removing =
      canonicalReactedEmoji(message.reactedEmoji).contains(canonical);
  if (!removing &&
      !canAddMessageReaction(
        channel,
        emojiExists: (counts[canonical] ?? 0) > 0,
      )) {
    return null;
  }
  return (emoji: canonical, removing: removing);
}

ReconciledReactions reconcileReactionUpdate(
  Map<String, int> currentCounts,
  Set<String> currentUserReactions, {
  required String emoji,
  required bool removed,
  required bool currentUser,
}) {
  final canonical = canonicalReactionEmoji(emoji);
  final counts = Map<String, int>.of(canonicalReactionCounts(currentCounts));
  final reacted = Set<String>.of(canonicalReactedEmoji(currentUserReactions));
  final count = (counts[canonical] ?? 0) + (removed ? -1 : 1);
  if (count <= 0) {
    counts.remove(canonical);
  } else {
    counts[canonical] = count;
  }
  if (currentUser) {
    if (removed) {
      reacted.remove(canonical);
    } else {
      reacted.add(canonical);
    }
  }
  return ReconciledReactions(
    counts: Map<String, int>.unmodifiable(counts),
    reactedEmoji: Set<String>.unmodifiable(reacted),
  );
}

/// Clear-all uses `/reactions`; one emoji uses Discord's standard group route.
/// Self-removal is disambiguated by the repository's trailing `/@me`.
String reactionClearEndpoint(
  EntityRef channel,
  EntityRef message, {
  String? emoji,
}) {
  final base = '/api/v1/channels/${channel.wire}/messages/${message.wire}';
  return emoji == null
      ? '$base/reactions'
      : '$base/reactions/${Uri.encodeComponent(canonicalReactionEmoji(emoji))}';
}
