import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/guild/guild_management_screen.dart';
import 'package:kaede_mobile/src/features/tracker/tracker_channel_view.dart';
import 'package:kaede_mobile/src/gateway/gateway_client.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

void main() {
  group('tracker models', () {
    test('decodes type 17 and an ordered, lossless board projection', () {
      expect(channelType(17), ChannelType.tracker);
      final channel = KaedeChannel.fromJson(_channelJson());
      expect(channel.type, ChannelType.tracker);
      expect(channel.toJson()['type'], 17);

      final board = TrackerBoard.fromJson(_boardJson());
      expect(board.channelRef.wire, '3@chat.example');
      expect(board.keyPrefix, 'LOU');
      expect(board.nextTaskNumber, 24);
      expect(board.lanes.map((lane) => lane.name), <String>['Planned', 'Done']);
      expect(board.tasks.map((task) => task.key), <String>['LOU-23', 'LOU-22']);
      expect(
          board.tasksFor(board.lanes.first).single.title, 'Ship mobile view');
      expect(board.tasks.last.assignee?.name, 'Casey');
      expect(board.tasks.last.completed, isTrue);
      expect(board.toJson()['permissions'], board.permissions.toString());
      expect(trackerLaneCanDelete(board, board.lanes.first), isFalse);
      expect(trackerLaneCanDelete(board, board.lanes.last), isFalse);
      final withEmptyDone = TrackerBoard.fromJson(<String, Object?>{
        ..._boardJson(),
        'tasks': <Object?>[
          _taskJson(
            id: '23',
            laneId: '10',
            position: 0,
            key: 'LOU-23',
            title: 'Ship mobile view',
          ),
        ],
      });
      expect(
        trackerLaneCanDelete(withEmptyDone, withEmptyDone.lanes.last),
        isTrue,
      );
    });

    test('uses exact BigInt checks for every high-bit tracker grant', () {
      final mask = TrackerPermission.createTasks |
          TrackerPermission.editOwnTasks |
          TrackerPermission.manageTasks |
          TrackerPermission.assignTasks |
          TrackerPermission.manageTracker;
      final board = TrackerBoard.fromJson(<String, Object?>{
        ..._boardJson(),
        'permissions': mask.toString(),
      });

      expect(board.allows(TrackerPermission.createTasks), isTrue);
      expect(board.allows(TrackerPermission.editOwnTasks), isTrue);
      expect(board.allows(TrackerPermission.manageTasks), isTrue);
      expect(board.allows(TrackerPermission.assignTasks), isTrue);
      expect(board.allows(TrackerPermission.manageTracker), isTrue);
      expect(mask > BigInt.from(0x1FFFFFFFFFFFFF), isTrue);
    });

    test('own-task editing includes creator and assignee but not a stranger',
        () {
      final board = TrackerBoard.fromJson(<String, Object?>{
        ..._boardJson(),
        'permissions': TrackerPermission.editOwnTasks.toString(),
      });
      final active = board.tasks.first;
      final completed = board.tasks.last;

      expect(trackerTaskCanEdit(board, active, active.creator.ref), isTrue);
      expect(
        trackerTaskCanEdit(board, completed, completed.assignee?.ref),
        isTrue,
      );
      expect(
        trackerTaskCanEdit(
          board,
          active,
          EntityRef.parse('999@chat.example'),
        ),
        isFalse,
      );
    });

    test('assignment-only access includes manager and self-service claims', () {
      final viewerBoard = TrackerBoard.fromJson(<String, Object?>{
        ..._boardJson(),
        'permissions': '0',
      });
      final unassigned = viewerBoard.tasks.first;
      final assigned = viewerBoard.tasks.last;
      final viewer = EntityRef.parse('999@chat.example');

      expect(trackerTaskCanEdit(viewerBoard, unassigned, viewer), isFalse);
      expect(trackerTaskCanAssign(viewerBoard, unassigned, viewer), isTrue);
      expect(trackerTaskCanOpenEditor(viewerBoard, unassigned, viewer), isTrue);
      expect(
        trackerTaskCanAssign(viewerBoard, assigned, assigned.assignee?.ref),
        isTrue,
      );
      expect(trackerTaskCanAssign(viewerBoard, assigned, viewer), isFalse);

      final managerBoard = TrackerBoard.fromJson(<String, Object?>{
        ..._boardJson(),
        'permissions': TrackerPermission.assignTasks.toString(),
      });
      expect(
        trackerTaskCanAssign(managerBoard, managerBoard.tasks.last, viewer),
        isTrue,
      );
    });
  });

  group('tracker API contract', () {
    test('fetches the board from the human channel endpoint', () async {
      final adapter = _RecordingJsonAdapter(jsonEncode(_boardJson()));
      final repository = _repository(adapter);

      final board = await repository.trackerBoard(
        EntityRef.parse('3@chat.example'),
      );

      expect(board.keyPrefix, 'LOU');
      expect(adapter.requests.single.method, 'GET');
      expect(
        adapter.requests.single.path,
        '/api/v1/channels/3@chat.example/tracker',
      );
    });

    test('retains the caller nonce across safe create retries', () async {
      final adapter = _RecordingJsonAdapter(jsonEncode(_taskJson(
        id: '90',
        laneId: '10',
        position: 1,
        key: 'LOU-24',
        title: 'Retry-safe task',
      )));
      final repository = _repository(adapter);
      final channel = EntityRef.parse('3@chat.example');
      final lane = EntityRef.parse('10@chat.example');

      for (var retry = 0; retry < 2; retry += 1) {
        await repository.createTrackerTask(
          channel,
          lane: lane,
          title: 'Retry-safe task',
          priority: TrackerPriority.high,
          clientNonce: 'mobile-create-123',
        );
      }

      expect(adapter.requests, hasLength(2));
      for (final request in adapter.requests) {
        expect(request.method, 'POST');
        expect(
          request.path,
          '/api/v1/channels/3@chat.example/tracker/tasks',
        );
        expect(request.data, containsPair('lane_id', lane.wire));
        expect(request.data, containsPair('priority', 'high'));
        expect(request.data, containsPair('client_nonce', 'mobile-create-123'));
      }
    });

    test('sends If-Match and explicit nullable task fields', () async {
      final adapter = _RecordingJsonAdapter(jsonEncode(_taskJson(
        id: '22',
        laneId: '10',
        position: 0,
        key: 'LOU-22',
        title: 'Updated',
      )));
      final repository = _repository(adapter);

      await repository.updateTrackerTask(
        EntityRef.parse('3@chat.example'),
        EntityRef.parse('22@chat.example'),
        'task-version',
        title: 'Updated',
        clearDescription: true,
        clearDueAt: true,
        clearAssignee: true,
      );

      final request = adapter.requests.single;
      expect(request.method, 'PATCH');
      expect(request.headers['If-Match'], 'task-version');
      expect(request.data, containsPair('description', null));
      expect(request.data, containsPair('due_at', null));
      expect(request.data, containsPair('assignee_id', null));
    });

    test('assignment-only patch omits task editing fields', () async {
      final adapter = _RecordingJsonAdapter(jsonEncode(_taskJson(
        id: '22',
        laneId: '10',
        position: 0,
        key: 'LOU-22',
        title: 'Assigned',
      )));
      final repository = _repository(adapter);

      await repository.updateTrackerTask(
        EntityRef.parse('3@chat.example'),
        EntityRef.parse('22@chat.example'),
        'task-version',
        assignee: EntityRef.parse('43@chat.example'),
      );

      expect(adapter.requests.single.data, <String, Object?>{
        'assignee_id': '43@chat.example',
      });
    });

    test('moves a task within its lane at an explicit insertion position',
        () async {
      final adapter = _RecordingJsonAdapter(jsonEncode(_taskJson(
        id: '23',
        laneId: '10',
        position: 0,
        key: 'LOU-23',
        title: 'Moved',
      )));
      final repository = _repository(adapter);

      await repository.moveTrackerTask(
        EntityRef.parse('3@chat.example'),
        EntityRef.parse('23@chat.example'),
        'task-version',
        lane: EntityRef.parse('10@chat.example'),
        position: 0,
      );

      final request = adapter.requests.single;
      expect(request.method, 'POST');
      expect(
        request.path,
        '/api/v1/channels/3@chat.example/tracker/tasks/23@chat.example/move',
      );
      expect(request.headers['If-Match'], 'task-version');
      expect(request.data, containsPair('lane_id', '10@chat.example'));
      expect(request.data, containsPair('position', 0));
    });
  });

  test('channel creation emits tracker type 17 and its key prefix', () {
    const draft = GuildChannelDraft(
      name: 'Raid prep',
      topic: 'Plan the next run',
      type: ChannelType.tracker,
      slowModeSeconds: 60,
      trackerKeyPrefix: 'RAID',
    );

    expect(draft.json, containsPair('type', 17));
    expect(draft.json, containsPair('tracker_key_prefix', 'RAID'));
    expect(draft.json, containsPair('rate_limit_per_user', 0));
  });

  testWidgets('channel editor exposes tracker creation and validates its key',
      (tester) async {
    GuildChannelDraft? result;
    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: Builder(
        builder: (context) => Scaffold(
          body: FilledButton(
            onPressed: () async {
              result = await showGuildChannelEditorSheet(context);
            },
            child: const Text('Open channel editor'),
          ),
        ),
      ),
    ));

    await tester.tap(find.text('Open channel editor'));
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('Task tracker'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Task tracker'));
    await tester.pumpAndSettle();
    expect(
      find.byKey(const ValueKey('tracker-key-prefix-field')),
      findsOneWidget,
    );
    await tester.enterText(
      find.byKey(const ValueKey('channel-name-field')),
      'release-plan',
    );
    await tester.enterText(
      find.byKey(const ValueKey('tracker-key-prefix-field')),
      'REL',
    );
    await tester
        .ensureVisible(find.byKey(const ValueKey('save-channel-button')));
    await tester.tap(find.byKey(const ValueKey('save-channel-button')));
    await tester.pumpAndSettle();

    expect(result?.type, ChannelType.tracker);
    expect(result?.trackerKeyPrefix, 'REL');
    expect(result?.json['type'], 17);
    expect(tester.takeException(), isNull);
  });

  test('tracker events refresh only the matching channel', () {
    final channel = EntityRef.parse('3@chat.example');
    expect(
      trackerGatewayEventMatchesChannel(
        const GatewayEvent(
          'TRACKER_TASK_UPDATE',
          <String, Object?>{
            'channel_id': '3',
            'channel_domain': 'chat.example',
          },
          4,
        ),
        channel,
      ),
      isTrue,
    );
    expect(
      trackerGatewayEventMatchesChannel(
        const GatewayEvent(
          'TRACKER_TASK_UPDATE',
          <String, Object?>{
            'channel_id': '4',
            'channel_domain': 'chat.example',
          },
          5,
        ),
        channel,
      ),
      isFalse,
    );
    expect(
      trackerGatewayEventMatchesChannel(
        const GatewayEvent(
          'CHANNEL_PERMISSION_UPDATE',
          <String, Object?>{
            'channel_id': '3',
            'channel_domain': 'chat.example',
          },
          6,
        ),
        channel,
      ),
      isTrue,
    );
    expect(
      trackerGatewayEventMatchesChannel(
        const GatewayEvent(
          'CHANNEL_ACCESS_REVOKED',
          <String, Object?>{
            'channel_id': '3',
            'channel_domain': 'chat.example',
          },
          7,
        ),
        channel,
      ),
      isTrue,
    );
    expect(
      trackerGatewayEventMatchesChannel(
        const GatewayEvent(
          'MESSAGE_UPDATE',
          <String, Object?>{
            'channel_id': '3',
            'channel_domain': 'chat.example',
          },
          6,
        ),
        channel,
      ),
      isFalse,
    );
  });

  test('security and resync events invalidate open tracker editors', () {
    for (final name in const <String>{
      'CHANNEL_ACCESS_REVOKED',
      'CHANNEL_PERMISSION_UPDATE',
      'READY',
      'RESUMED',
      'INVALID_SESSION',
      'GATEWAY_SEQUENCE_GAP',
    }) {
      expect(
        trackerGatewayEventInvalidatesOpenEditors(
          GatewayEvent(name, const <String, Object?>{}, 1),
        ),
        isTrue,
        reason: name,
      );
    }
    expect(
      trackerGatewayEventInvalidatesOpenEditors(
        const GatewayEvent(
          'TRACKER_TASK_UPDATE',
          <String, Object?>{},
          2,
        ),
      ),
      isFalse,
    );
  });

  test('revoked or missing access discards cached tracker contents', () {
    KaedeException failure(int status) => KaedeException(
          code: 'TEST',
          message: 'test',
          status: status,
        );

    expect(trackerRefreshMustDiscardBoard(failure(401)), isTrue);
    expect(trackerRefreshMustDiscardBoard(failure(403)), isTrue);
    expect(trackerRefreshMustDiscardBoard(failure(404)), isTrue);
    expect(trackerRefreshMustDiscardBoard(failure(503)), isFalse);
    expect(trackerRefreshMustDiscardBoard(StateError('offline')), isFalse);
  });

  testWidgets('touch task editor returns a stable idempotency key',
      (tester) async {
    TrackerTaskDraft? result;
    final lane = TrackerLane.fromJson(_laneJson(
      id: '10',
      name: 'Planned',
      position: 0,
      kind: 'planned',
    ));
    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: Builder(
        builder: (context) => Scaffold(
          body: FilledButton(
            onPressed: () async {
              result = await showModalBottomSheet<TrackerTaskDraft>(
                context: context,
                isScrollControlled: true,
                builder: (_) => TrackerTaskEditorSheet(
                  lanes: <TrackerLane>[lane],
                  initialLane: lane,
                  actor: KaedeUser.fromJson(_userJson('42', 'Alex')),
                  members: const <GuildMember>[],
                  canAssignOthers: false,
                ),
              );
            },
            child: const Text('Open task editor'),
          ),
        ),
      ),
    ));

    await tester.tap(find.text('Open task editor'));
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byKey(const ValueKey('tracker-task-title')),
      'Prepare runbook',
    );
    await tester.ensureVisible(find.byKey(const ValueKey('tracker-task-save')));
    await tester.tap(find.byKey(const ValueKey('tracker-task-save')));
    await tester.pumpAndSettle();

    expect(result?.title, 'Prepare runbook');
    expect(
      result?.clientNonce,
      matches(RegExp(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
      )),
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('assignment-only editor locks task details but saves assignee',
      (tester) async {
    TrackerTaskDraft? result;
    final board = TrackerBoard.fromJson(_boardJson());
    final lane = board.lanes.first;
    final task = board.tasks.first;
    final actor = KaedeUser.fromJson(_userJson('99', 'Morgan'));
    final assignee = KaedeUser.fromJson(_userJson('43', 'Casey'));

    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: Builder(
        builder: (context) => Scaffold(
          body: FilledButton(
            onPressed: () async {
              result = await showModalBottomSheet<TrackerTaskDraft>(
                context: context,
                isScrollControlled: true,
                builder: (_) => TrackerTaskEditorSheet(
                  lanes: board.lanes,
                  initialLane: lane,
                  task: task,
                  actor: actor,
                  members: <GuildMember>[
                    GuildMember(user: assignee, roleIds: const <String>[]),
                  ],
                  canAssignOthers: true,
                  canEditDetails: false,
                ),
              );
            },
            child: const Text('Open assignment editor'),
          ),
        ),
      ),
    ));

    await tester.tap(find.text('Open assignment editor'));
    await tester.pumpAndSettle();
    expect(find.text('Assign ${task.key}'), findsOneWidget);
    expect(find.text('Save assignment'), findsOneWidget);
    expect(
      tester
          .widget<TextFormField>(
              find.byKey(const ValueKey('tracker-task-title')))
          .enabled,
      isFalse,
    );
    expect(
      tester
          .widget<DropdownButtonFormField<EntityRef>>(
              find.byKey(const ValueKey('tracker-task-lane')))
          .onChanged,
      isNull,
    );
    expect(
      tester
          .widget<IconButton>(find.byWidgetPredicate(
            (widget) =>
                widget is IconButton && widget.tooltip == 'Change due date',
          ))
          .onPressed,
      isNull,
    );

    final assigneeField = find.byKey(const ValueKey('tracker-task-assignee'));
    await tester.ensureVisible(assigneeField);
    await tester.tap(assigneeField);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Casey').last);
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.byKey(const ValueKey('tracker-task-save')));
    await tester.tap(find.byKey(const ValueKey('tracker-task-save')));
    await tester.pumpAndSettle();

    expect(result?.title, task.title);
    expect(result?.lane, task.laneRef);
    expect(result?.assignee, assignee.ref);
    expect(tester.takeException(), isNull);
  });

  testWidgets('read-only task details expose the full description',
      (tester) async {
    final lane = TrackerLane.fromJson(_laneJson(
      id: '10',
      name: 'Planned',
      position: 0,
      kind: 'planned',
    ));
    final task = TrackerTask.fromJson(_taskJson(
      id: '23',
      laneId: '10',
      position: 0,
      key: 'LOU-23',
      title: 'Ship mobile view',
      description: 'Validate the release on phones and tablets.',
    ));

    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: Scaffold(
        body: TrackerTaskDetailsSheet(
          task: task,
          lane: lane,
          canEdit: false,
        ),
      ),
    ));

    expect(find.text('Validate the release on phones and tablets.'),
        findsOneWidget);
    expect(find.text('Planned'), findsOneWidget);
    expect(find.text('Edit task'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('move sheet can reorder a task within its current lane',
      (tester) async {
    TrackerTaskMoveDraft? result;
    final lane = TrackerLane.fromJson(_laneJson(
      id: '10',
      name: 'Planned',
      position: 0,
      kind: 'planned',
    ));
    final first = TrackerTask.fromJson(_taskJson(
      id: '21',
      laneId: '10',
      position: 0,
      key: 'LOU-21',
      title: 'First task',
    ));
    final moving = TrackerTask.fromJson(_taskJson(
      id: '22',
      laneId: '10',
      position: 1,
      key: 'LOU-22',
      title: 'Move me',
    ));

    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: Builder(
        builder: (context) => Scaffold(
          body: FilledButton(
            onPressed: () async {
              result = await showModalBottomSheet<TrackerTaskMoveDraft>(
                context: context,
                builder: (_) => TrackerTaskMoveSheet(
                  task: moving,
                  lanes: <TrackerLane>[lane],
                  tasks: <TrackerTask>[first, moving],
                ),
              );
            },
            child: const Text('Open move sheet'),
          ),
        ),
      ),
    ));

    await tester.tap(find.text('Open move sheet'));
    await tester.pumpAndSettle();
    await tester.tap(
      find.byKey(const ValueKey('tracker-task-move-position-10@chat.example')),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('At the top').last);
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('tracker-task-move-submit')));
    await tester.pumpAndSettle();

    expect(result?.lane, lane.ref);
    expect(result?.position, 0);
    expect(tester.takeException(), isNull);
  });
}

KaedeRepository _repository(_RecordingJsonAdapter adapter) => KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );

Map<String, Object?> _channelJson() => <String, Object?>{
      'id': '3',
      'origin_domain': 'chat.example',
      'guild_id': '1',
      'guild_domain': 'chat.example',
      'type': 17,
      'position': 2,
      'permissions': '0',
      'name': 'Raid prep',
    };

Map<String, Object?> _boardJson() {
  final permissions = TrackerPermission.createTasks |
      TrackerPermission.editOwnTasks |
      TrackerPermission.manageTracker;
  return <String, Object?>{
    'channel_id': '3',
    'channel_domain': 'chat.example',
    'key_prefix': 'LOU',
    'next_task_number': '24',
    'version': 'board-version',
    'permissions': permissions.toString(),
    'lanes': <Object?>[
      _laneJson(
        id: '11',
        name: 'Done',
        position: 1,
        kind: 'completed',
        completed: true,
      ),
      _laneJson(
        id: '10',
        name: 'Planned',
        position: 0,
        kind: 'planned',
      ),
    ],
    'tasks': <Object?>[
      _taskJson(
        id: '22',
        laneId: '11',
        position: 0,
        key: 'LOU-22',
        title: 'Publish strategy',
        completed: true,
        assignee: _userJson('43', 'Casey'),
      ),
      _taskJson(
        id: '23',
        laneId: '10',
        position: 0,
        key: 'LOU-23',
        title: 'Ship mobile view',
      ),
    ],
  };
}

Map<String, Object?> _laneJson({
  required String id,
  required String name,
  required int position,
  required String kind,
  bool completed = false,
}) =>
    <String, Object?>{
      'id': id,
      'origin_domain': 'chat.example',
      'channel_id': '3',
      'channel_domain': 'chat.example',
      'name': name,
      'color': completed ? 0x68B69B : 0xF5B700,
      'kind': kind,
      'completed': completed,
      'position': position,
      'task_count': 1,
      'version': 'lane-$id-version',
    };

Map<String, Object?> _taskJson({
  required String id,
  required String laneId,
  required int position,
  required String key,
  required String title,
  String? description,
  bool completed = false,
  Map<String, Object?>? assignee,
}) =>
    <String, Object?>{
      'id': id,
      'origin_domain': 'chat.example',
      'channel_id': '3',
      'channel_domain': 'chat.example',
      'lane_id': laneId,
      'lane_domain': 'chat.example',
      'number': key.split('-').last,
      'key': key,
      'title': title,
      'description': description,
      'priority': completed ? 'none' : 'high',
      'position': position,
      'due_at': completed ? null : '2026-09-01T17:00:00+00:00',
      'completed_at': completed ? '2026-08-26T10:00:00+00:00' : null,
      'creator': _userJson('42', 'Alex'),
      'assignee': assignee,
      'version': 'task-$id-version',
    };

Map<String, Object?> _userJson(String id, String name) => <String, Object?>{
      'id': id,
      'origin_domain': 'chat.example',
      'username': name.toLowerCase(),
      'display_name': name,
      'handle': '@${name.toLowerCase()}@chat.example',
    };

final class _RecordingJsonAdapter implements HttpClientAdapter {
  _RecordingJsonAdapter(this.body);

  final String body;
  final List<_RecordedRequest> requests = <_RecordedRequest>[];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    requests.add(_RecordedRequest(
      method: options.method,
      path: options.path,
      data: options.data is Map
          ? Map<String, Object?>.from(options.data! as Map)
          : const <String, Object?>{},
      headers: options.headers.map((key, value) => MapEntry(key, '$value')),
    ));
    return ResponseBody.fromString(
      body,
      200,
      headers: <String, List<String>>{
        Headers.contentTypeHeader: <String>[Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}

final class _RecordedRequest {
  const _RecordedRequest({
    required this.method,
    required this.path,
    required this.data,
    required this.headers,
  });

  final String method;
  final String path;
  final Map<String, Object?> data;
  final Map<String, String> headers;
}
