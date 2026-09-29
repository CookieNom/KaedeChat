import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/chat/channel_view.dart';
import 'package:kaede_mobile/src/features/chat/composer_pickers.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:mocktail/mocktail.dart';
import 'package:record/record.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'discord_settings_visual_test.dart'
    show fixtureController, fixtureGuild, fixtureUser;

class _Recorder extends Mock implements RecordPlatform {}

void main() {
  for (final width in [320.0, 600.0]) {
    testWidgets('composer overflow at width $width', (tester) async {
      tester.view.physicalSize = Size(width, 800);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      FlutterSecureStorage.setMockInitialValues({});
      SharedPreferences.setMockInitialValues({});
      final user = fixtureUser();
      final controller = await fixtureController(fixtureGuild(user), user, []);
      await controller.api.useTokens(SessionTokens(
        instance: Domain('chat.example'),
        userRef: user.ref,
        accessToken: 'access',
        refreshToken: 'refresh',
      ));
      final channel = KaedeChannel(
        ref: EntityRef.parse('2@chat.example'),
        type: ChannelType.text,
        guildRef: fixtureGuild(user).ref,
        position: 0,
        permissions: BigInt.from(Permission.viewChannel |
            Permission.readMessageHistory |
            Permission.sendMessages),
      );
      controller.state = MobileState(
        phase: SessionPhase.ready,
        user: user,
        dms: [channel],
        selectedChannel: channel.ref,
      );
      await tester.pumpWidget(ProviderScope(
        overrides: [mobileControllerProvider.overrideWith((ref) => controller)],
        child: MaterialApp(
          theme: kaedeTheme(),
          home: const Scaffold(body: ChannelView()),
        ),
      ));
      await tester.pumpAndSettle();
      final input = find.byType(TextField);
      await tester.enterText(input, 'a' * 3001);
      await tester.pumpAndSettle();
      expect(tester.widget<TextField>(input).controller!.text.length, 3001);
      final counter = tester.widget<Text>(find.text('-1'));
      expect(counter.style!.color, kaedeTheme().colorScheme.error);
      final send = find.widgetWithIcon(IconButton, Icons.arrow_upward_rounded);
      expect(tester.widget<IconButton>(send).onPressed, isNull);
      await tester.tap(send);
      await tester.pump();
      expect(tester.widget<TextField>(input).controller!.text.length, 3001);
      controller.setDraft(channel.ref, 'a' * 3001);
      expect(controller.state.drafts[channel.ref], 'a' * 3001);
      await tester.enterText(input, 'a' * 3000);
      await tester.pumpAndSettle();
      expect(find.text('0'), findsOneWidget);
      expect(tester.widget<IconButton>(send).onPressed, isNotNull);
      await tester.enterText(input, 'a' * 2800);
      await tester.pumpAndSettle();
      expect(find.text('200'), findsOneWidget);
      await tester.enterText(input, 'a' * 2799);
      await tester.pumpAndSettle();
      expect(find.text('201'), findsNothing);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pump();
    });
  }
  for (final platform in [TargetPlatform.android, TargetPlatform.iOS]) {
    for (final type in [
      ChannelType.dm,
      ChannelType.groupDm,
      ChannelType.text
    ]) {
      for (final history in <List<String>>[
        [],
        ['🎉'],
        ['🎉', '😎', '🚀', '👀', '💯']
      ]) {
        testWidgets('$platform $type actions with reaction history $history',
            (tester) async {
          final user = fixtureUser();
          final ownMessage = history.length == 1;
          final moderator = history.length == 5;
          final isGuild = type == ChannelType.text;
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
            type: type,
            guildRef: isGuild ? fixtureGuild(user).ref : null,
            position: 0,
            permissions: BigInt.from(Permission.viewChannel |
                Permission.readMessageHistory |
                Permission.addReactions |
                (ownMessage ? Permission.sendMessages : 0) |
                (moderator ? Permission.manageMessages : 0)),
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
                  authorRef: ownMessage
                      ? user.ref
                      : EntityRef.parse('999@chat.example'),
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
          final canReply = !isGuild || ownMessage;
          expect(reply, canReply ? findsOneWidget : findsNothing);
          expect(
              find.descendant(
                  of: sheet,
                  matching: find.widgetWithText(ListTile, 'Delete message')),
              ownMessage || (isGuild && moderator)
                  ? findsOneWidget
                  : findsNothing);
          expect(
              find.descendant(
                  of: sheet,
                  matching: find.widgetWithText(ListTile, 'Edit message')),
              ownMessage ? findsOneWidget : findsNothing);
          expect(
              find.descendant(
                  of: sheet,
                  matching: find.widgetWithText(ListTile, 'Pin message')),
              isGuild ? findsNothing : findsOneWidget);
          for (final element in reactions.evaluate()) {
            final emoji = find.byWidget(element.widget);
            if (canReply) {
              expect(tester.getBottomLeft(emoji).dy,
                  lessThanOrEqualTo(tester.getTopLeft(reply).dy));
            }
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
}
