import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/features/chat/invite_card.dart';

void main() {
  test('invite previews qualify, deduplicate, and limit references', () {
    expect(
      messageInviteReferences(
        'https://kaede.chat/invite/6eaJyk5M '
        'kaede.chat/invite/6eaJyk5M. '
        'https://remote.example/invite/Ab12Cd34%40remote.example '
        'https://kaede.chat/invite/YH05dLlw/ '
        'https://kaede.chat/invite/12345678',
        encrypted: false,
      ),
      ['6eaJyk5M@kaede.chat', 'Ab12Cd34@remote.example', 'YH05dLlw@kaede.chat'],
    );
  });

  test('private and invalid links never trigger invite previews', () {
    const url = 'https://kaede.chat/invite/6eaJyk5M';
    expect(messageInviteReferences(url, encrypted: true), isEmpty);
    for (final content in [
      '||$url||',
      '$url?query=1',
      '$url#fragment',
      'https://user@kaede.chat/invite/6eaJyk5M',
      'https://kaede.chat:8443/invite/6eaJyk5M',
      'https://kaede.chat/invite/6eaJyk5M%40other.example',
      'https://kaede.chat/invite/invalid',
    ]) {
      expect(messageInviteReferences(content, encrypted: false), isEmpty);
    }
  });

  testWidgets('invite card renders preview and unavailable state on a phone',
      (tester) async {
    const reference = '6eaJyk5M@kaede.chat';
    final provider = inviteCardPreviewProvider(reference);
    Widget app({required bool fail}) => ProviderScope(
          overrides: [
            provider.overrideWith((ref) async {
              if (fail) throw const FormatException('Expired');
              return {
                'guild': {'name': 'Test guild', 'origin_domain': 'kaede.chat'}
              };
            }),
          ],
          child: const MaterialApp(
            home: Scaffold(
                body: SizedBox(
                    width: 260, child: InviteCard(reference: reference))),
          ),
        );
    await tester.pumpWidget(app(fail: false));
    await tester.pumpAndSettle();
    expect(find.text('Test guild'), findsOneWidget);
    expect(find.text('kaede.chat'), findsOneWidget);
    expect(find.text('View invitation'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
    await tester.pumpWidget(app(fail: true));
    await tester.pumpAndSettle();
    expect(find.text('Invitation unavailable'), findsOneWidget);
    expect(find.text('Retry'), findsOneWidget);
  });
}
