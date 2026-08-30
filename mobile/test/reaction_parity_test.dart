import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/chat/channel_view.dart';
import 'package:kaede_mobile/src/features/chat/composer_pickers.dart';

void main() {
  testWidgets('reaction picker returns the backend-canonical heart',
      (tester) async {
    final selected = <String>[];
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: ComposerEmojiPicker(
          loader: () async => const <Map<String, Object?>>[],
          channel: KaedeChannel(
            ref: EntityRef.parse('30@home.example'),
            type: ChannelType.dm,
            position: 0,
            permissions: BigInt.zero,
          ),
          categories: const <String, List<String>>{
            'Symbols': <String>['❤️'],
          },
          semanticAction: 'React with',
          canonicalizeReactions: true,
          embedded: true,
          onSelected: selected.add,
        ),
      ),
    ));
    await tester.pump();
    await tester.pump();

    await tester.tap(find.byKey(const ValueKey('reaction-emoji-❤')));
    await tester.pump();

    expect(
      tester.widget<Text>(find.byKey(const ValueKey('reaction-emoji-❤'))).data,
      '❤️',
    );
    expect(selected, const <String>['❤']);
  });

  testWidgets('qualified custom reaction renders its authority asset',
      (tester) async {
    const token = '<a:party_blob:41@emoji.example>';
    await tester.pumpWidget(const MaterialApp(
      home: Scaffold(
        body: ReactionEmojiGlyph(emoji: token, size: 24),
      ),
    ));

    expect(find.byType(CustomEmojiImage), findsOneWidget);
    expect(find.byKey(const ValueKey('reaction-emoji-$token')), findsOneWidget);
    final image =
        tester.widget<CustomEmojiImage>(find.byType(CustomEmojiImage));
    expect(image.ref.wire, '41@emoji.example');
    expect(image.label, ':party_blob:');
  });

  testWidgets('message double-tap invokes quick reaction and keeps long-press',
      (tester) async {
    var reactions = 0;
    var menus = 0;
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: MessageGestureSurface(
          onDoubleTapReaction: () => reactions += 1,
          onLongPress: () => menus += 1,
          child: const SizedBox(
            key: ValueKey('message-body'),
            width: 180,
            height: 80,
          ),
        ),
      ),
    ));

    final message = find.byKey(const ValueKey('message-body'));
    await tester.tap(message, warnIfMissed: false);
    await tester.pump(const Duration(milliseconds: 50));
    await tester.tap(message, warnIfMissed: false);
    await tester.pumpAndSettle();

    expect(reactions, 1);
    expect(menus, 0);

    await tester.longPress(message, warnIfMissed: false);
    await tester.pump();
    expect(menus, 1);
  });
}
