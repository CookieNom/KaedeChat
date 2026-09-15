import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/chat/channel_view.dart';
import 'package:kaede_mobile/src/features/chat/read_inbox_screen.dart';
import 'package:kaede_mobile/src/features/home/mobile_shell.dart';
import 'package:kaede_mobile/src/gateway/gateway_client.dart';
import 'package:kaede_mobile/src/platform/push_service.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:mocktail/mocktail.dart';
import 'package:record/record.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'local_database_test.dart' show seededDatabase;

class _Recorder extends Mock implements RecordPlatform {}

EntityRef messageRef(int id) => EntityRef.parse('$id@chat.example');
final channelRef = messageRef(500);

Future<(MobileController, List<RequestOptions>, void Function(bool))> fixture(
    {bool deleteAnchor = false}) async {
  FlutterSecureStorage.setMockInitialValues({});
  SharedPreferences.setMockInitialValues({});
  final log = <RequestOptions>[];
  var cursor = 20;
  var version = 0;
  var failAck = false;
  final messages = List.generate(
          200,
          (index) => KaedeMessage(
                ref: messageRef(index + 1),
                channelRef: channelRef,
                authorRef: messageRef(900),
                content: 'Message ${index + 1}',
                createdAt: DateTime.utc(2026).add(Duration(seconds: index)),
              ))
      .where((message) => !deleteAnchor || message.ref != messageRef(20))
      .toList();
  final dio = Dio()
    ..interceptors.add(InterceptorsWrapper(onRequest: (request, handler) {
      log.add(request);
      Object? data = <Object?>[];
      if (request.path.endsWith('/messages')) {
        var selected = messages.toList();
        final query = request.queryParameters;
        int id(Object? ref) => int.parse('$ref'.split('@').first);
        if (query['around'] != null) {
          final target = id(query['around']);
          selected = [
            ...messages
                .where((m) => id(m.ref.wire) < target)
                .toList()
                .reversed
                .take(25),
            ...messages.where((m) => id(m.ref.wire) >= target).take(25)
          ];
        } else if (query['after'] != null) {
          selected = messages
              .where((m) => id(m.ref.wire) > id(query['after']))
              .take(50)
              .toList();
        } else if (query['before'] != null) {
          selected = messages
              .where((m) => id(m.ref.wire) < id(query['before']))
              .toList()
              .reversed
              .take(50)
              .toList();
        } else {
          selected = messages.reversed.take(50).toList();
        }
        selected.sort((a, b) => compareReadPositions(b.ref, a.ref));
        data = selected.map((m) => m.toJson()).toList();
      } else if (request.path.endsWith('/ack')) {
        if (failAck) {
          handler.reject(DioException(
              requestOptions: request, type: DioExceptionType.connectionError));
          return;
        }
        final target = int.parse(
            '${(request.data as Map)['message_id']}'.split('@').first);
        final payload = request.data as Map;
        if (payload['read_version'] != version) {
          handler.reject(DioException(
              requestOptions: request,
              response: Response(requestOptions: request, statusCode: 409),
              type: DioExceptionType.badResponse));
          return;
        }
        if (payload['mark_unread'] == true) {
          cursor = target - 1;
          version++;
        } else if (target > cursor) {
          cursor = target;
        }
        data = null;
      } else if (request.path.endsWith('/read-states')) {
        data = [
          {
            'channel_id': '500',
            'channel_domain': 'chat.example',
            'read_version': version,
            'last_message_id': '200',
            'last_message_domain': 'chat.example',
            'first_unread_message_id': '${cursor + 1}',
            'first_unread_message_domain': 'chat.example',
            'first_unread_at': cursor < 200
                ? messages[cursor].createdAt.toIso8601String()
                : null,
            'read_message_id': cursor == 0 ? null : '$cursor',
            'read_message_domain': cursor == 0 ? null : 'chat.example',
            'unread_count': 200 - cursor,
            'mention_count': cursor < 200 ? 1 : 0
          }
        ];
      }
      handler.resolve(Response(
          requestOptions: request,
          statusCode: data == null ? 204 : 200,
          data: data));
    }));
  final api = KaedeApiClient(vault: const SessionVault(), httpClient: dio);
  await api.useTokens(SessionTokens(
      instance: Domain('chat.example'),
      accessToken: 'test',
      refreshToken: 'test',
      userRef: messageRef(901)));
  final database = await seededDatabase();
  final gateway = GatewayClient(
      tokens: () async => null,
      socketConnector: (_) => throw UnimplementedError());
  final controller = MobileController(KaedeRepository(api), api, gateway,
      database, PushService.test(firebaseReady: true));
  final channel = KaedeChannel(
      ref: channelRef,
      type: ChannelType.dm,
      position: 0,
      permissions: BigInt.zero);
  controller.state = MobileState(
      phase: SessionPhase.ready,
      dms: [channel],
      user: KaedeUser(
          ref: messageRef(901),
          username: 'reader',
          handle: '@reader@chat.example'),
      readPositions: {channelRef: messageRef(20)},
      unreadCounts: {channelRef: 180},
      mentionCounts: {channelRef: 1});
  addTearDown(() async {
    if (controller.mounted) controller.dispose();
    await gateway.close();
    await database.close();
  });
  return (controller, log, (bool value) => failAck = value);
}

void main() {
  testWidgets('Inbox opens from the account bar immediately left of Settings',
      (tester) async {
    final fonts = FontLoader('Inter');
    for (final weight in [
      'Regular',
      'Medium',
      'SemiBold',
      'Bold',
      'ExtraBold'
    ]) {
      fonts.addFont(rootBundle.load('assets/fonts/Inter-$weight.ttf'));
    }
    await fonts.load();
    final messenger =
        TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    final audioChannels = <String>{
      'xyz.luan/audioplayers.global',
      'xyz.luan/audioplayers.global/events',
      'xyz.luan/audioplayers'
    };
    for (final channel in audioChannels.toList()) {
      messenger.setMockMethodCallHandler(MethodChannel(channel), (call) async {
        if (call.method == 'create') {
          final events =
              'xyz.luan/audioplayers/events/${(call.arguments as Map)['playerId']}';
          audioChannels.add(events);
          messenger.setMockMethodCallHandler(
              MethodChannel(events), (_) async => null);
        }
        return null;
      });
    }
    addTearDown(() {
      for (final channel in audioChannels) {
        messenger.setMockMethodCallHandler(MethodChannel(channel), null);
      }
    });
    final result = await tester.runAsync(() => fixture());
    final controller = result!.$1;
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.binding.setSurfaceSize(const Size(390, 844));
    await tester.pumpWidget(ProviderScope(
      overrides: [mobileControllerProvider.overrideWith((ref) => controller)],
      child: MaterialApp(theme: kaedeTheme(), home: const MobileShell()),
    ));
    await tester.pumpAndSettle();
    for (final width in [390.0, 320.0]) {
      if (width == 320) {
        final guildRef = messageRef(700);
        controller.state = controller.state.copyWith(
          selectedGuild: guildRef,
          guilds: [
            KaedeGuild(
                ref: guildRef,
                name: 'Kaede Chat Official',
                ownerRef: controller.state.user!.ref,
                permissions: BigInt.zero,
                unavailable: false)
          ],
        );
      }
      await tester.binding.setSurfaceSize(Size(width, 844));
      await tester.pumpAndSettle();
      final inbox = tester.getRect(find.byTooltip('Inbox'));
      final settings = tester.getRect(find.byTooltip('Settings'));
      expect(inbox.center.dy, closeTo(settings.center.dy, 1));
      expect(inbox.right, lessThanOrEqualTo(settings.left));
      expect(inbox.width, greaterThanOrEqualTo(44));
      expect(inbox.bottom, greaterThan(750));
      expect(find.byType(FloatingActionButton), findsNothing);
      expect(tester.takeException(), isNull);
    }
    await tester.tap(find.byTooltip('Inbox'));
    await tester
        .runAsync(() => Future<void>.delayed(const Duration(milliseconds: 50)));
    await tester.pumpAndSettle();
    expect(find.byType(ReadInboxScreen), findsOneWidget);
    expect(result.$2.where((request) => request.path.endsWith('/read-states')),
        isNotEmpty);
    await tester.pageBack();
    await tester.pumpAndSettle();
    expect(find.byTooltip('Inbox'), findsOneWidget);
    await tester.pumpWidget(const SizedBox.shrink());
    await tester
        .runAsync(() => Future<void>.delayed(const Duration(milliseconds: 50)));
  });

  test(
      'manual unread survives visible reads, including the first message, and bulk read clears it',
      () async {
    final (controller, log, _) = await fixture();
    controller.setConversationPaneVisible(true);
    await controller.selectDm(controller.state.dms.single);
    final first = controller.state.messages.first;
    await controller.markMessageUnread(first);
    expect(controller.state.readPositions[channelRef], isNull);
    expect(controller.state.visitReadPosition, first.ref);
    expect(controller.state.visitReadInclusive, isTrue);
    final requests = log.where((r) => r.path.endsWith('/ack')).length;
    await controller.acknowledgeVisibleMessage(
        channelRef, controller.state.messages.last.ref);
    expect(log.where((r) => r.path.endsWith('/ack')).length, requests);
    await controller.selectDm(controller.state.dms.single);
    expect(
        log
            .where((r) => r.path.endsWith('/messages'))
            .last
            .queryParameters['around'],
        first.ref.wire);
    await controller.markChannelsRead(channel: channelRef);
    expect(controller.state.readPositions[channelRef], messageRef(200));
    expect(controller.state.unreadCounts[channelRef] ?? 0, 0);
  });

  TestWidgetsFlutterBinding.ensureInitialized();

  test(
      'read cursors preserve composite identity and never rewind on a stale snapshot',
      () {
    final snapshot = decodeReadBadgeSnapshot([
      {
        'channel_id': '500',
        'channel_domain': 'chat.example',
        'read_message_id': '9007199254740993',
        'read_message_domain': 'remote.example',
        'unread': true,
      }
    ]);
    final cursor = EntityRef.parse('9007199254740993@remote.example');
    expect(snapshot.positions[channelRef], cursor);
    expect(
        mergeReadPositions(snapshot.positions, {
          channelRef: EntityRef.parse('9007199254740992@remote.example')
        })[channelRef],
        cursor);
    expect(compareReadPositions(messageRef(9), messageRef(10)), lessThan(0));
  });

  test(
      'opening, paging forward, latest and saved-position jumps preserve the visit and draft',
      () async {
    final (controller, log, _) = await fixture();
    controller.setConversationPaneVisible(true);
    await controller.selectDm(controller.state.dms.single);
    expect(
        log
            .where((r) => r.path.endsWith('/messages'))
            .single
            .queryParameters['around'],
        messageRef(20).wire);
    expect(log.where((r) => r.path.endsWith('/ack')), isEmpty);
    expect(controller.state.messageJump?.message, messageRef(20));
    expect(controller.state.channelsWithNewerMessages, contains(channelRef));
    controller.setDraft(channelRef, 'Unsent draft');
    final newestLoaded = controller.state.messages.last.ref;
    await controller.loadMessages(newer: true);
    expect(
        log
            .where((r) => r.path.endsWith('/messages'))
            .last
            .queryParameters['after'],
        newestLoaded.wire);
    expect(controller.state.messages, hasLength(94));
    await controller.jumpToLatest();
    expect(controller.state.messages.last.ref, messageRef(200));
    expect(controller.state.channelsWithNewerMessages, isEmpty);
    expect(
        log
            .where((r) => r.path.endsWith('/messages'))
            .last
            .queryParameters
            .keys,
        ['limit']);
    await controller.acknowledgeVisibleMessage(channelRef, messageRef(200));
    expect(controller.state.unreadCounts[channelRef], isNull);
    await controller.jumpToReadPosition();
    expect(controller.state.messageJump?.message, messageRef(20));
    expect(controller.state.visitReadPosition, messageRef(20));
    expect(controller.state.drafts[channelRef], 'Unsent draft');
  });

  test(
      'hidden panes and failed acknowledgements retain unreads; successful partial reads retain newer mentions',
      () async {
    final (controller, log, failAck) = await fixture();
    await controller.selectDm(controller.state.dms.single);
    await controller.acknowledgeVisibleMessage(channelRef, messageRef(30));
    expect(log.where((r) => r.path.endsWith('/ack')), isEmpty);
    controller.setConversationPaneVisible(true);
    failAck(true);
    await controller.acknowledgeVisibleMessage(channelRef, messageRef(30));
    expect(controller.state.readPositions[channelRef], messageRef(20));
    expect(controller.state.unreadCounts[channelRef], 180);
    expect(controller.state.degradedWarnings,
        contains(DegradedFeature.acknowledgements));
    failAck(false);
    await controller.acknowledgeVisibleMessage(channelRef, messageRef(35));
    expect(controller.state.readPositions[channelRef], messageRef(35));
    expect(controller.state.unreadCounts[channelRef], 165);
    expect(controller.state.mentionCounts[channelRef], 1);
    final count = log.where((r) => r.path.endsWith('/ack')).length;
    await controller.acknowledgeVisibleMessage(channelRef, messageRef(25));
    expect(log.where((r) => r.path.endsWith('/ack')).length, count);
  });

  test('a deleted saved message reveals the nearest retained newer message',
      () async {
    final (controller, _, _) = await fixture(deleteAnchor: true);
    await controller.selectDm(controller.state.dms.single);
    expect(controller.state.messageJump?.message, messageRef(21));
    expect(controller.state.visitReadPosition, messageRef(20));
  });

  testWidgets('mobile history renders its saved divider and both jump controls',
      (tester) async {
    final previousRecorder = RecordPlatform.instance;
    final recorder = _Recorder();
    RecordPlatform.instance = recorder;
    when(() => recorder.create(any())).thenAnswer((_) async {});
    when(() => recorder.dispose(any())).thenAnswer((_) async {});
    when(() => recorder.onStateChanged(any()))
        .thenAnswer((_) => const Stream<RecordState>.empty());
    addTearDown(() => RecordPlatform.instance = previousRecorder);
    final result = await tester.runAsync(() async {
      final result = await fixture();
      await result.$1.selectDm(result.$1.state.dms.single);
      return result;
    });
    final controller = result!.$1;
    // Keep acknowledgements disabled here; controller tests exercise their network path.
    await tester.pumpWidget(ProviderScope(
      overrides: [mobileControllerProvider.overrideWith((ref) => controller)],
      child: MaterialApp(
          theme: kaedeTheme(), home: const Scaffold(body: ChannelView())),
    ));
    await tester.pumpAndSettle();
    expect(
        find.descendant(
            of: find.byKey(const ValueKey('unread-history-bar')),
            matching: find.text('180 new messages')),
        findsOneWidget);
    expect(
        tester
            .getSize(find.ancestor(
                of: find.text('180 new messages').first,
                matching: find.byType(TextButton)))
            .height,
        lessThanOrEqualTo(44));
    expect(find.text('Mark as read'), findsOneWidget);
    expect(find.textContaining('since '), findsOneWidget);
    expect(find.text('New messages'), findsOneWidget);
    expect(find.byIcon(Icons.arrow_downward_rounded), findsOneWidget);
    expect(tester.takeException(), isNull);
    // A historical page's latest row is not the conversation's latest message.
    expect(controller.state.messages.last.ref, isNot(messageRef(200)));
    await tester.tap(find.byIcon(Icons.arrow_downward_rounded));
    for (var attempt = 0;
        attempt < 50 &&
            (controller.state.messages.last.ref != messageRef(200) ||
                controller.state.loadingMessages);
        attempt++) {
      await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 30)));
      // The SQLite isolate and the widget clock must both advance during a jump.
      await tester.pump(const Duration(milliseconds: 100));
    }
    expect(controller.state.loadingMessages, isFalse);
    expect(controller.state.messages.last.ref, messageRef(200));
    await tester.pumpAndSettle();
    expect(controller.state.error, isNull);
    expect(
        result.$2
            .where((r) => r.path.endsWith('/messages'))
            .last
            .queryParameters
            .keys,
        ['limit']);
    expect(controller.state.messages.last.ref, messageRef(200));
    expect(controller.state.visitReadPosition, messageRef(20));
    result.$3(true);
    await tester.tap(find.text('Mark as read'));
    await tester
        .runAsync(() => Future<void>.delayed(const Duration(milliseconds: 50)));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('unread-history-bar')), findsOneWidget);
    expect(controller.state.readPositions[channelRef], messageRef(20));
    result.$3(false);
    await tester.tap(find.text('Mark as read'));
    await tester
        .runAsync(() => Future<void>.delayed(const Duration(milliseconds: 50)));
    await tester.pumpAndSettle();
    expect(controller.state.readPositions[channelRef], messageRef(200));
    expect(controller.state.unreadCounts[channelRef] ?? 0, 0);
    expect(find.byKey(const ValueKey('unread-history-bar')), findsNothing);
    expect(controller.state.visitReadPosition, messageRef(20));
    // A server snapshot from another device must also remove an open banner.
    controller.state =
        controller.state.copyWith(unreadCounts: {channelRef: 180});
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('unread-history-bar')), findsOneWidget);
    controller.state = controller.state.copyWith(unreadCounts: {});
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('unread-history-bar')), findsNothing);
    await tester.pumpWidget(const SizedBox.shrink());
    await tester
        .runAsync(() => Future<void>.delayed(const Duration(milliseconds: 50)));
  });
}
