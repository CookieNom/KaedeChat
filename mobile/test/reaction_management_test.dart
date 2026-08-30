import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/reaction_emoji.dart';
import 'package:kaede_mobile/src/domain/reaction_management.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';

void main() {
  final channelRef = EntityRef(Snowflake('20'), Domain('remote.example'));
  final message = EntityRef(Snowflake('10'), Domain('remote.example'));

  test('clearing one emoji preserves every other reaction group', () {
    final counts = <String, int>{
      '🔥': 2,
      '<:party:7@remote.example>': 3,
    };
    final mine = <String>{'🔥', '<:party:7@remote.example>'};

    final result = reconcileClearedReactions(
      counts,
      mine,
      emoji: '<:party:7@remote.example>',
    );

    expect(result.counts, <String, int>{'🔥': 2});
    expect(result.reactedEmoji, <String>{'🔥'});
    expect(counts, containsPair('<:party:7@remote.example>', 3));
    expect(mine, contains('<:party:7@remote.example>'));
  });

  test('clearing all resets counts and the current-user reaction set', () {
    final result = reconcileClearedReactions(
      <String, int>{'🔥': 2},
      <String>{'🔥'},
    );

    expect(result.counts, isEmpty);
    expect(result.reactedEmoji, isEmpty);
  });

  test('clear routes use the standard group path separate from self-removal',
      () {
    expect(
      reactionClearEndpoint(channelRef, message),
      '/api/v1/channels/20@remote.example/messages/10@remote.example/reactions',
    );
    expect(
      reactionClearEndpoint(
        channelRef,
        message,
        emoji: '<:party:7@remote.example>',
      ),
      '/api/v1/channels/20@remote.example/messages/10@remote.example/'
      'reactions/%3C%3Aparty%3A7%40remote.example%3E',
    );
  });

  test('reaction canonicalization strips presentation selectors', () {
    expect(canonicalReactionEmoji('❤️'), '❤');
    expect(canonicalReactionEmoji('❤︎'), '❤');
    expect(
      canonicalReactionEmoji('<a:party:7@REMOTE.EXAMPLE.>'),
      '<a:party:7@remote.example>',
    );
    expect(() => canonicalReactionEmoji('heart'), throwsFormatException);
  });

  test('reaction presentation restores color selectors without changing keys',
      () {
    expect(reactionEmojiPresentation('❤'), '❤️');
    expect(reactionEmojiPresentation('🏳‍⚧'), '🏳️‍⚧️');
    expect(reactionEmojiPresentation('1⃣'), '1️⃣');
    expect(reactionEmojiPresentation('😂'), '😂');
    expect(
      reactionEmojiPresentation('<:party:7@remote.example>'),
      '<:party:7@remote.example>',
    );
    expect(canonicalReactionEmoji(reactionEmojiPresentation('❤')), '❤');
  });

  test('toggle aliases remove the active canonical reaction', () {
    final channel = KaedeChannel(
      ref: channelRef,
      type: ChannelType.dm,
      position: 0,
      permissions: BigInt.zero,
    );
    final message = KaedeMessage(
      ref: EntityRef.parse('10@remote.example'),
      channelRef: channelRef,
      authorRef: EntityRef.parse('30@remote.example'),
      createdAt: DateTime.utc(2026),
      reactionCounts: const <String, int>{'❤': 2},
      reactedEmoji: const <String>{'❤️'},
    );

    expect(
      reactionToggleDecision(channel, message, '❤'),
      (emoji: '❤', removing: true),
    );
  });

  test('dedicated gateway reactions reconcile structured emoji aliases', () {
    final emoji = gatewayReactionEmoji(<String, Object?>{
      'reaction': '❤️',
      'emoji': <String, Object?>{
        'id': null,
        'name': '❤',
        'animated': false,
      },
    });
    final added = reconcileReactionUpdate(
      const <String, int>{},
      const <String>{},
      emoji: emoji!,
      removed: false,
      currentUser: true,
    );
    final removed = reconcileReactionUpdate(
      added.counts,
      added.reactedEmoji,
      emoji: '❤️',
      removed: true,
      currentUser: true,
    );

    expect(added.counts, <String, int>{'❤': 1});
    expect(added.reactedEmoji, <String>{'❤'});
    expect(removed.counts, isEmpty);
    expect(removed.reactedEmoji, isEmpty);
  });

  test('reaction clearing requires a live guild channel and Manage Messages',
      () {
    KaedeChannel makeChannel({
      EntityRef? guildRef,
      int permissions = 0,
      bool archived = false,
    }) =>
        KaedeChannel(
          ref: channelRef,
          guildRef: guildRef,
          type: guildRef == null ? ChannelType.dm : ChannelType.text,
          position: 0,
          permissions: BigInt.from(permissions),
          archived: archived,
        );

    final guild = EntityRef.parse('30@remote.example');
    expect(
      canClearMessageReactions(
        makeChannel(guildRef: guild, permissions: Permission.manageMessages),
      ),
      isTrue,
    );
    expect(
      canClearMessageReactions(makeChannel(guildRef: guild)),
      isFalse,
    );
    expect(
      canClearMessageReactions(
        makeChannel(
          guildRef: guild,
          permissions: Permission.manageMessages,
          archived: true,
        ),
      ),
      isFalse,
    );
    expect(
      canClearMessageReactions(
        makeChannel(permissions: Permission.manageMessages),
      ),
      isFalse,
    );
  });
}
