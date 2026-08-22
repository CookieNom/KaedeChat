import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/features/chat/channel_view.dart';
import 'package:kaede_mobile/src/features/chat/swipe_to_reply.dart';

void main() {
  group('swipe to reply', () {
    late PageController pages;
    late int replies;

    Future<void> pumpConversation(WidgetTester tester,
        {bool enabled = true}) async {
      pages = PageController(initialPage: 1);
      replies = 0;
      await tester.pumpWidget(MaterialApp(
        home: PageView(
          controller: pages,
          allowImplicitScrolling: true,
          children: [
            const Center(child: Text('channel list')),
            ListView(
              children: [
                for (var index = 0; index < 40; index++)
                  SwipeToReply(
                    enabled: enabled,
                    onReply: () => replies++,
                    child: SizedBox(
                      height: 48,
                      child: Text('message $index'),
                    ),
                  ),
              ],
            ),
          ],
        ),
      ));
      await tester.pumpAndSettle();
    }

    tearDown(() => pages.dispose());

    testWidgets('a leftward drag on a message replies and keeps the page',
        (tester) async {
      await pumpConversation(tester);

      await tester.drag(find.text('message 3'), const Offset(-90, 0));
      await tester.pumpAndSettle();

      expect(replies, 1);
      expect(pages.page, 1);
    });

    testWidgets('a leftward fling replies instead of navigating',
        (tester) async {
      await pumpConversation(tester);

      await tester.fling(find.text('message 3'), const Offset(-300, 0), 1000);
      await tester.pumpAndSettle();

      expect(replies, 1);
      expect(pages.page, 1);
      expect(find.text('channel list'), findsNothing);
    });

    testWidgets('a wobble at touch-down does not kill the reply drag',
        (tester) async {
      await pumpConversation(tester);
      final gesture =
          await tester.startGesture(tester.getCenter(find.text('message 3')));

      // A finger settling on the glass often slides the wrong way first.
      await gesture.moveBy(const Offset(8, 0));
      for (var step = 0; step < 4; step++) {
        await gesture.moveBy(const Offset(-25, 0));
      }
      await gesture.up();
      await tester.pumpAndSettle();

      expect(replies, 1);
      expect(pages.page, 1);
    });

    testWidgets('a moderately diagonal swipe still replies', (tester) async {
      await pumpConversation(tester);
      final gesture =
          await tester.startGesture(tester.getCenter(find.text('message 3')));

      // Real thumbs travel in an arc; reply should not demand a nearly
      // pixel-perfect horizontal line.
      for (var step = 0; step < 4; step++) {
        await gesture.moveBy(const Offset(-20, -24));
      }
      await gesture.up();
      await tester.pumpAndSettle();

      expect(replies, 1);
      expect(pages.page, 1);
    });

    testWidgets('crossing the trigger stays armed through a small rebound',
        (tester) async {
      await pumpConversation(tester);
      final gesture =
          await tester.startGesture(tester.getCenter(find.text('message 3')));

      await gesture.moveBy(const Offset(-55, 0));
      await gesture.moveBy(const Offset(12, 0));
      await gesture.up();
      await tester.pumpAndSettle();

      expect(replies, 1);
      expect(pages.page, 1);
    });

    testWidgets('dragging back deliberately cancels an armed reply',
        (tester) async {
      await pumpConversation(tester);
      final gesture =
          await tester.startGesture(tester.getCenter(find.text('message 3')));

      await gesture.moveBy(const Offset(-55, 0));
      await gesture.moveBy(const Offset(35, 0));
      await gesture.up();
      await tester.pumpAndSettle();

      expect(replies, 0);
      expect(pages.page, 1);
    });

    testWidgets('a steep diagonal drag scrolls instead of replying',
        (tester) async {
      await pumpConversation(tester);
      final before = tester.getTopLeft(find.text('message 3')).dy;
      final gesture =
          await tester.startGesture(tester.getCenter(find.text('message 3')));

      await gesture.moveBy(const Offset(-14, -40));
      await gesture.moveBy(const Offset(-14, -60));
      await gesture.up();
      await tester.pumpAndSettle();

      expect(replies, 0);
      expect(pages.page, 1);
      expect(tester.getTopLeft(find.text('message 3')).dy, lessThan(before));
    });

    testWidgets('a short leftward drag does not reach the reply trigger',
        (tester) async {
      await pumpConversation(tester);

      await tester.drag(find.text('message 3'), const Offset(-30, 0));
      await tester.pumpAndSettle();

      expect(replies, 0);
      expect(pages.page, 1);
    });

    testWidgets('a cancelled drag settles without replying', (tester) async {
      await pumpConversation(tester);
      final gesture =
          await tester.startGesture(tester.getCenter(find.text('message 3')));

      await gesture.moveBy(const Offset(-90, 0));
      await gesture.cancel();
      await tester.pumpAndSettle();

      expect(replies, 0);
      expect(pages.page, 1);
    });

    testWidgets('a rightward drag anywhere returns to the channel list',
        (tester) async {
      await pumpConversation(tester);

      await tester.fling(find.text('message 3'), const Offset(300, 0), 1000);
      await tester.pumpAndSettle();

      expect(replies, 0);
      expect(pages.page, 0);
      expect(find.text('channel list'), findsOneWidget);
    });

    testWidgets('a vertical drag still scrolls the message list',
        (tester) async {
      await pumpConversation(tester);
      final before = tester.getTopLeft(find.text('message 3')).dy;

      await tester.drag(find.text('message 3'), const Offset(0, -120));
      await tester.pumpAndSettle();

      expect(replies, 0);
      expect(pages.page, 1);
      expect(tester.getTopLeft(find.text('message 3')).dy, lessThan(before));
    });

    testWidgets('a drag that starts on rendered message text still replies',
        (tester) async {
      // Message bodies are markdown, and a selectable one installs its own
      // horizontal drag recognizer that would swallow the swipe.
      pages = PageController(initialPage: 1);
      replies = 0;
      await tester.pumpWidget(MaterialApp(
        home: PageView(
          controller: pages,
          children: [
            const Center(child: Text('channel list')),
            SwipeToReply(
              enabled: true,
              onReply: () => replies++,
              child: const KaedeMessageMarkdown(
                content: 'ship it on friday',
                state: MobileState(),
              ),
            ),
          ],
        ),
      ));
      await tester.pumpAndSettle();

      await tester.drag(
          find.byType(KaedeMessageMarkdown), const Offset(-90, 0));
      await tester.pumpAndSettle();

      expect(replies, 1);
      expect(pages.page, 1);
    });

    testWidgets('a nested horizontal recognizer cannot steal the reply swipe',
        (tester) async {
      pages = PageController(initialPage: 1);
      replies = 0;
      await tester.pumpWidget(MaterialApp(
        home: PageView(
          controller: pages,
          children: [
            const Center(child: Text('channel list')),
            SwipeToReply(
              enabled: true,
              onReply: () => replies++,
              child: GestureDetector(
                behavior: HitTestBehavior.opaque,
                onHorizontalDragUpdate: (_) {},
                child: const SizedBox.expand(
                  child: Center(child: Text('interactive message content')),
                ),
              ),
            ),
          ],
        ),
      ));
      await tester.pumpAndSettle();

      await tester.drag(
        find.text('interactive message content'),
        const Offset(-80, 0),
      );
      await tester.pumpAndSettle();

      expect(replies, 1);
      expect(pages.page, 1);
    });

    testWidgets('a disabled row leaves both directions to the page view',
        (tester) async {
      await pumpConversation(tester, enabled: false);

      await tester.fling(find.text('message 3'), const Offset(-300, 0), 1000);
      await tester.pumpAndSettle();

      expect(replies, 0);
      expect(pages.page, 1, reason: 'there is no third page to reach');

      await tester.fling(find.text('message 3'), const Offset(300, 0), 1000);
      await tester.pumpAndSettle();

      expect(pages.page, 0);
    });
  });
}
