import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/features/chat/link_privacy.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  test('removes only si from supported sharing links', () {
    const cases = {
      'https://open.spotify.com/track/7?si=abc&utm_source=copy-link':
          'https://open.spotify.com/track/7?utm_source=copy-link',
      'https://music.youtube.com/watch?v=123&si=abc':
          'https://music.youtube.com/watch?v=123',
      'See [this](https://youtu.be/123?si=abc&t=42#part).':
          'See [this](https://youtu.be/123?t=42#part).',
      'https://www.youtube.com/watch?si=a&v=123&si=b&x=%20+%2f':
          'https://www.youtube.com/watch?v=123&x=%20+%2f',
      'https://youtu.be/123?%73i=&t=4': 'https://youtu.be/123?t=4',
      'https://youtu.be/123?si=abc https://open.spotify.com/track/7?si=def':
          'https://youtu.be/123 https://open.spotify.com/track/7',
      'https://youtube.com.evil.test/?si=a https://example.com/?si=b':
          'https://youtube.com.evil.test/?si=a https://example.com/?si=b',
      'https://youtube.com/watch?v=1#fragment?si=keep':
          'https://youtube.com/watch?v=1#fragment?si=keep',
    };
    for (final entry in cases.entries) {
      expect(stripLinkTracking(entry.key), entry.value);
    }
  });
  for (final enabled in [true, false]) {
    testWidgets('blocks sending and remembers choice $enabled', (tester) async {
      tester.view.physicalSize = const Size(375, 812);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      SharedPreferences.setMockInitialValues({});
      String? sent;
      const input = 'https://youtu.be/123?si=abc&t=4';
      await tester.pumpWidget(MaterialApp(home: Builder(builder: (context) {
        return TextButton(
            onPressed: () async {
              sent = await preparePrivateLinks(context, input, 'test-$enabled');
            },
            child: const Text('Send'));
      })));
      await tester.tap(find.text('Send'));
      await tester.pumpAndSettle();
      expect(sent, isNull);
      await tester.binding.handlePopRoute();
      await tester.pumpAndSettle();
      expect(sent, isNull);
      expect(find.text('Remove link tracking?'), findsOneWidget);
      await tester
          .tap(find.text(enabled ? 'Remove tracking' : 'Keep tracking'));
      await tester.pumpAndSettle();
      final expected = enabled ? 'https://youtu.be/123?t=4' : input;
      expect(sent, expected);
      expect(
          (await SharedPreferences.getInstance())
              .getBool('kaede:strip-link-si:test-$enabled'),
          enabled);
      sent = null;
      await tester.tap(find.text('Send'));
      await tester.pumpAndSettle();
      expect(sent, expected);
      expect(find.text('Remove link tracking?'), findsNothing);
    });
  }
}
