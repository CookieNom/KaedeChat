import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/chat/channel_view.dart';
import 'package:kaede_mobile/src/features/chat/composer_pickers.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:mocktail/mocktail.dart';
import 'package:record/record.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'discord_settings_visual_test.dart'
    show fixtureController, fixtureGuild, fixtureUser;

class _Recorder extends Mock implements RecordPlatform {}

void main() {
  for (final platform in [TargetPlatform.android, TargetPlatform.iOS]) {
    for (final history in <List<String>>[
      [],
      ['🎉'],
      ['🎉', '😎', '🚀', '👀', '💯']
    ]) {
      testWidgets('$platform keeps four reactions first with $history',
          (tester) async {
        final user = fixtureUser();
        FlutterSecureStorage.setMockInitialValues({});
        SharedPreferences.setMockInitialValues({
          'message-reaction-history:${user.ref.wire}': history,
        });
        final previousRecorder = RecordPlatform.instance;
        final recorder = _Recorder();
        RecordPlatform.instance = recorder;
        when(() => recorder.create(any())).thenAnswer((_) async {});
        when(() => recorder.dispose(any())).thenAnswer((_) async {});
        when(() => recorder.onStateChanged(any()))
            .thenAnswer((_) => const Stream<RecordState>.empty());
        addTearDown(() => RecordPlatform.instance = previousRecorder);
        final controller =
            await fixtureController(fixtureGuild(user), user, []);
        final channel = KaedeChannel(
          ref: EntityRef.parse('2@chat.example'),
          type: ChannelType.dm,
          position: 0,
          permissions: BigInt.zero,
        );
        controller.state = MobileState(
          phase: SessionPhase.ready,
          user: user,
          dms: [channel],
          selectedChannel: channel.ref,
          messageStore: {
            channel.ref: [
              KaedeMessage(
                ref: EntityRef.parse('3@chat.example'),
                channelRef: channel.ref,
                authorRef: user.ref,
                content: 'Open this message menu',
                createdAt: DateTime.utc(2026),
              )
            ],
          },
        );
        await tester.pumpWidget(ProviderScope(
          overrides: [
            mobileControllerProvider.overrideWith((ref) => controller)
          ],
          child: MaterialApp(
            theme: kaedeTheme().copyWith(platform: platform),
            home: const Scaffold(body: ChannelView()),
          ),
        ));
        await tester.pumpAndSettle();
        await tester.longPress(find.text('Open this message menu'));
        await tester.pumpAndSettle();
        final sheet = find.byType(BottomSheet);
        expect(sheet, findsOneWidget);
        final scroll = find.descendant(
            of: sheet, matching: find.byType(SingleChildScrollView));
        final column =
            tester.widget<SingleChildScrollView>(scroll).child! as Column;
        final reactions = find.descendant(
          of: find.byWidget(column.children.first),
          matching: find.byType(ReactionEmojiGlyph),
        );
        expect(reactions, findsNWidgets(4));
        expect(
            tester
                .widgetList<ReactionEmojiGlyph>(reactions)
                .map((glyph) => glyph.emoji)
                .toSet(),
            hasLength(4));
        final reply = find.descendant(
            of: sheet, matching: find.widgetWithText(ListTile, 'Reply'));
        expect(reply, findsOneWidget);
        for (final element in reactions.evaluate()) {
          final emoji = find.byWidget(element.widget);
          expect(tester.getBottomLeft(emoji).dy,
              lessThanOrEqualTo(tester.getTopLeft(reply).dy));
          expect(find.ancestor(of: emoji, matching: find.byType(InkWell)),
              findsWidgets);
        }
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox.shrink());
        await tester.pumpAndSettle();
      });
    }
  }
}
