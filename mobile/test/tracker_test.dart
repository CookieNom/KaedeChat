import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/tracker_media_repository.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/guild/guild_management_screen.dart';
import 'package:kaede_mobile/src/features/tracker/tracker_channel_view.dart';
import 'package:kaede_mobile/src/features/tracker/tracker_custom_fields.dart';
import 'package:kaede_mobile/src/features/tracker/tracker_field_settings.dart';
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
      final permissions = [
        TrackerPermission.createTasks,
        TrackerPermission.editOwnTasks,
        TrackerPermission.manageTasks,
        TrackerPermission.assignTasks,
        TrackerPermission.manageTracker
      ];
      for (final granted in [BigInt.zero, ...permissions]) {
        final board = TrackerBoard.fromJson(<String, Object?>{
          ..._boardJson(),
          'permissions': granted.toString(),
        });
        for (final requested in permissions) {
          expect(board.allows(requested), granted == requested);
        }
      }
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
          trackerTaskCanEdit(board, active,
              EntityRef(active.creator.ref.id, Domain('other.example'))),
          isFalse);
      expect(
          trackerTaskCanEdit(board, completed,
              EntityRef(completed.assignee!.ref.id, Domain('other.example'))),
          isFalse);
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

    test('preserving a caller nonce across two explicit creates', () async {
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

  testWidgets('valid tracker prefix acceptance', (tester) async {
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

  test('security events invalidate open tracker editors', () {
    for (final name in const <String>{
      'CHANNEL_ACCESS_REVOKED',
      'CHANNEL_PERMISSION_UPDATE',
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

  test('reconnecting preserves open tracker tasks and unsaved editors', () {
    for (final name in const <String>{
      'READY',
      'RESUMED',
      'INVALID_SESSION',
      'GATEWAY_SEQUENCE_GAP',
    }) {
      expect(
        trackerGatewayEventInvalidatesOpenEditors(
          GatewayEvent(name, const <String, Object?>{}, 1),
        ),
        isFalse,
        reason: name,
      );
    }
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

  testWidgets('generated idempotency-key shape', (tester) async {
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
          customFields: TrackerCustomFields(fields: const [
            TrackerField(id: 'notes', name: 'Review notes', type: 'textarea')
          ], values: const {
            'notes': 'Approved for release'
          }, channel: task.channelRef),
        ),
      ),
    ));

    expect(find.text('Validate the release on phones and tablets.'),
        findsOneWidget);
    expect(find.text('Planned'), findsOneWidget);
    expect(find.text('Edit task'), findsNothing);
    expect(find.text('Approved for release'), findsOneWidget);
    expect(find.byType(TextFormField), findsNothing);
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
  test(
      'custom field definitions and task values survive projection and API updates',
      () async {
    const fields = [
      TrackerField(id: 'reviewer', name: 'Reviewer', type: 'users'),
      TrackerField(
          id: 'stage',
          name: 'Stage',
          type: 'select',
          options: ['Ready', 'Blocked'])
    ];
    final board = TrackerBoard.fromJson({
      ..._boardJson(),
      'custom_fields': fields.map((f) => f.toJson()).toList()
    });
    expect(TrackerBoard.fromJson(board.toJson()).customFields.last.options,
        ['Ready', 'Blocked']);
    final values = <String, Object?>{
      'reviewer': ['43@chat.example'],
      'stage': 'Ready',
      'files': [
        {'id': '91@chat.example', 'name': 'Design.png', 'type': 'image'}
      ]
    };
    final task = TrackerTask.fromJson({
      ..._taskJson(
          id: '23', laneId: '10', position: 0, key: 'LOU-23', title: 'Review'),
      'custom_values': values
    });
    expect(TrackerTask.fromJson(task.toJson()).customValues, values);
    final boardAdapter = _RecordingJsonAdapter(jsonEncode(board.toJson()));
    await _repository(boardAdapter).updateTrackerBoard(
        board.channelRef, board.version,
        customFields: fields);
    expect(boardAdapter.requests.single.data,
        {'custom_fields': fields.map((f) => f.toJson()).toList()});
    expect(boardAdapter.requests.single.headers['If-Match'], board.version);
    final taskAdapter = _RecordingJsonAdapter(jsonEncode(task.toJson()));
    final repo = _repository(taskAdapter);
    await repo.createTrackerTask(board.channelRef,
        lane: board.lanes.first.ref, title: 'Review', customValues: values);
    await repo.updateTrackerTask(board.channelRef, task.ref, task.version,
        customValues: {});
    expect(taskAdapter.requests.first.data['custom_values'], values);
    expect(taskAdapter.requests.last.data['custom_values'], isEmpty);
    expect(taskAdapter.requests.last.headers['If-Match'], task.version);
  });

  test(
      'tracker media uses channel-scoped capabilities and qualified attachment IDs',
      () async {
    final adapter = _RecordingJsonAdapter(
        jsonEncode({'url': 'https://storage.example/private'}));
    final repo = _repository(adapter);
    await repo.trackerMedia(EntityRef.parse('3@chat.example'), 'read',
        {'attachment_id': '91@remote.example'});
    expect(adapter.requests.single.path,
        '/api/v1/channels/3@chat.example/tracker/attachments/read');
    expect(
        adapter.requests.single.data, {'attachment_id': '91@remote.example'});
    for (final invalid in [
      'javascript:alert(1)',
      'https://user:secret@example.org/file',
      'https://example.org/a b',
      '//example.org/image'
    ]) {
      expect(trackerSafeUrl(invalid), isFalse);
    }
    expect(trackerSafeUrl('https://example.org/file?token=abc'), isTrue);
    await expectLater(
        repo.uploadTrackerFile(EntityRef.parse('3@chat.example'),
            File('/unused-cancelled-upload'), 'draft.png',
            contentType: 'image/png', isActive: () => false),
        throwsA(isA<UserInputException>()));
    expect(adapter.requests, hasLength(1));
  });

  testWidgets(
      'failed saves preserve custom values and nonce until a successful retry',
      (tester) async {
    final board = TrackerBoard.fromJson(_boardJson());
    final drafts = <TrackerTaskDraft>[];
    await tester.pumpWidget(MaterialApp(
        theme: kaedeTheme(),
        home: Builder(
            builder: (context) => Scaffold(
                    body: TextButton(
                  child: const Text('Open'),
                  onPressed: () => showModalBottomSheet<void>(
                      context: context,
                      isScrollControlled: true,
                      builder: (_) => TrackerTaskEditorSheet(
                          lanes: board.lanes,
                          initialLane: board.lanes.first,
                          members: const [],
                          canAssignOthers: true,
                          channel: board.channelRef,
                          fields: const [
                            TrackerField(
                                id: 'note',
                                name: 'Review notes',
                                type: 'textarea')
                          ],
                          onSave: (draft) async {
                            drafts.add(draft);
                            if (drafts.length == 1) {
                              throw const UserInputException(
                                  'Connection lost. Try again.');
                            }
                          })),
                )))));
    await tester.tap(find.text('Open'));
    await tester.pumpAndSettle();
    await tester.enterText(
        find.byKey(const ValueKey('tracker-task-title')), 'Release checklist');
    final note = find.descendant(
        of: find.byKey(const ValueKey('custom-field-note')),
        matching: find.byType(TextFormField));
    await tester.ensureVisible(note);
    await tester.enterText(note, 'Keep this draft');
    await tester.tap(find.byKey(const ValueKey('tracker-task-save')));
    await tester.pumpAndSettle();
    expect(find.byType(TrackerTaskEditorSheet), findsOneWidget);
    expect(drafts.single.customValues, {'note': 'Keep this draft'});
    expect(find.text('Keep this draft'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('tracker-task-save')));
    await tester.pumpAndSettle();
    expect(drafts.last.clientNonce, drafts.first.clientNonce);
    expect(drafts.last.customValues, drafts.first.customValues);
    expect(find.byType(TrackerTaskEditorSheet), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
      'all field types render at narrow, wide and landscape sizes with large text',
      (tester) async {
    final fields = [
      for (final entry in trackerFieldTypes.entries)
        TrackerField(
            id: entry.key,
            name: entry.value,
            type: entry.key,
            options: entry.key == 'select' || entry.key == 'multiselect'
                ? ['Ready', 'Blocked']
                : const [])
    ];
    final actor = KaedeUser.fromJson(_userJson('42', 'Alex'));
    final channel = KaedeChannel.fromJson(_channelJson());
    var values = <String, Object?>{
      'text': 'Launch notes',
      'textarea': 'Review copy and accessibility before release.',
      'select': 'Ready',
      'multiselect': ['Ready'],
      'number': 12.5,
      'date': '2026-09-14',
      'checkbox': true,
      'url': 'https://example.org/design',
      'users': [actor.ref.wire],
      'channels': [channel.ref.wire],
      'attachments': [
        {
          'name': 'Design walkthrough.mp4',
          'url': 'https://example.org/design.mp4',
          'type': 'video'
        },
        {
          'name': 'Mobile design.png',
          'url': 'https://example.org/design.png',
          'type': 'image'
        }
      ],
    };
    for (final size in [
      const Size(320, 640),
      const Size(800, 1000),
      const Size(740, 360)
    ]) {
      tester.view.reset();
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = size;
      await tester.pumpWidget(MaterialApp(
          theme: kaedeTheme(),
          home: MediaQuery(
              data: MediaQueryData(
                  size: size, textScaler: TextScaler.linear(1.3)),
              child: Scaffold(
                  body: StatefulBuilder(
                      builder: (context, setState) => Form(
                          child: SingleChildScrollView(
                              padding: const EdgeInsets.all(20),
                              child: TrackerCustomFields(
                                  fields: fields,
                                  values: values,
                                  channel: channel.ref,
                                  channels: [channel],
                                  actor: actor,
                                  canAssignOthers: true,
                                  onChanged: (v) =>
                                      setState(() => values = v)))))))));
      await tester.pumpAndSettle();
      for (final f in fields) {
        await tester
            .ensureVisible(find.byKey(ValueKey('custom-field-${f.id}')));
        await tester.pump();
        expect(tester.takeException(), isNull, reason: '${f.type} at $size');
      }
      expect(find.text('Tap to preview'), findsNWidgets(2));
    }
    tester.view.reset();
  });

  testWidgets(
      'people picker preserves locked assignments while adding yourself',
      (tester) async {
    List<String>? selected;
    await tester.pumpWidget(MaterialApp(
        home: Builder(
            builder: (context) => Scaffold(
                body: TextButton(
                    child: const Text('Open'),
                    onPressed: () async {
                      selected = await showModalBottomSheet<List<String>>(
                          context: context,
                          isScrollControlled: true,
                          builder: (_) => TrackerReferencePicker(
                              title: 'Reviewers',
                              labels: const {
                                '42@chat.example': 'Alex',
                                '43@chat.example': 'Casey'
                              },
                              selected: const ['43@chat.example'],
                              canChange: (id) => id == '42@chat.example'));
                    })))));
    await tester.tap(find.text('Open'));
    await tester.pumpAndSettle();
    final locked = tester.widget<CheckboxListTile>(
        find.widgetWithText(CheckboxListTile, 'Casey'));
    expect(locked.onChanged, isNull);
    await tester.tap(find.text('Alex'));
    await tester.tap(find.text('Done'));
    await tester.pumpAndSettle();
    expect(selected, containsAll(['43@chat.example', '42@chat.example']));
    expect(tester.takeException(), isNull);
  });

  testWidgets(
      'field settings validate names and choices on a phone keyboard layout',
      (tester) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(360, 800);
    addTearDown(tester.view.reset);
    await tester.pumpWidget(MaterialApp(
        theme: kaedeTheme(),
        home: Scaffold(
            body: MediaQuery(
                data: const MediaQueryData(
                    size: Size(360, 800),
                    viewInsets: EdgeInsets.only(bottom: 280)),
                child: const TrackerFieldEditorSheet(fields: [
                  TrackerField(id: 'reviewer', name: 'Reviewer', type: 'users')
                ])))));
    await tester.enterText(
        find.byKey(const ValueKey('tracker-field-name')), 'Reviewer');
    await tester.tap(find.text('Add field'));
    await tester.pumpAndSettle();
    expect(find.text('A field already has this name'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await tester.enterText(
        find.byKey(const ValueKey('tracker-field-name')), 'Status');
    await tester.tap(find.byType(DropdownButtonFormField<String>));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Dropdown').last);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Add field'));
    await tester.pumpAndSettle();
    expect(find.text('Add 1–100 choices, each up to 100 characters'),
        findsOneWidget);
    expect(tester.takeException(), isNull);
  });
  testWidgets(
      'attachment links validate before adding and retain preview metadata',
      (tester) async {
    var values = <String, Object?>{};
    await tester.pumpWidget(MaterialApp(
        theme: kaedeTheme(),
        home: Scaffold(
            body: StatefulBuilder(
                builder: (context, setState) => TrackerCustomFields(
                        fields: const [
                          TrackerField(
                              id: 'files', name: 'Files', type: 'attachments')
                        ],
                        values: values,
                        channel: EntityRef.parse('3@chat.example'),
                        onChanged: (next) => setState(() => values = next))))));
    await tester.tap(find.text('Add link'));
    await tester.pumpAndSettle();
    final dialog = find.byType(AlertDialog);
    await tester
        .tap(find.descendant(of: dialog, matching: find.text('Add link')));
    await tester.pumpAndSettle();
    expect(find.text('Enter a name'), findsOneWidget);
    final inputs =
        find.descendant(of: dialog, matching: find.byType(TextFormField));
    await tester.enterText(inputs.first, 'Design review');
    await tester.enterText(inputs.last, 'https://example.org/design.mp4');
    await tester.tap(find.byType(DropdownButtonFormField<String>));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Video').last);
    await tester.pumpAndSettle();
    await tester
        .tap(find.descendant(of: dialog, matching: find.text('Add link')));
    await tester.pumpAndSettle();
    expect(values['files'], [
      {
        'name': 'Design review',
        'url': 'https://example.org/design.mp4',
        'type': 'video'
      }
    ]);
    expect(find.text('Tap to preview'), findsOneWidget);
    await tester.tap(find.byTooltip('Remove attachment'));
    await tester.pumpAndSettle();
    expect(values, isEmpty);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
      'settings require removal confirmation and save the ordered field definitions',
      (tester) async {
    final board = TrackerBoard.fromJson({
      ..._boardJson(),
      'custom_fields': [
        const TrackerField(id: 'a', name: 'Effort', type: 'number').toJson(),
        const TrackerField(id: 'b', name: 'Approved', type: 'checkbox')
            .toJson(),
      ]
    });
    final adapter = _RecordingJsonAdapter(jsonEncode(board.toJson()));
    await tester.pumpWidget(MaterialApp(
        theme: kaedeTheme(),
        home: Builder(
            builder: (context) => Scaffold(
                body: TextButton(
                    child: const Text('Settings'),
                    onPressed: () => showModalBottomSheet<void>(
                        context: context,
                        isScrollControlled: true,
                        builder: (_) => TrackerFieldSettingsSheet(
                            board: board,
                            channel: board.channelRef,
                            repository: _repository(adapter))))))));
    await tester.tap(find.text('Settings'));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('Move field down').first);
    await tester.pumpAndSettle();
    expect(
        tester
            .widgetList<ListTile>(find.byType(ListTile))
            .where((w) => w.title is Text)
            .map((w) => (w.title! as Text).data)
            .where((title) => title != 'Tracker settings')
            .first,
        'Approved');
    await tester.tap(find.text('Remove').first);
    await tester.pumpAndSettle();
    expect(find.text('Remove Approved?'), findsOneWidget);
    expect(adapter.requests, isEmpty);
    await tester.tap(find.text('Remove field'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Save settings'));
    await tester.pumpAndSettle();
    expect(adapter.requests.single.data['custom_fields'],
        [const TrackerField(id: 'a', name: 'Effort', type: 'number').toJson()]);
    expect(adapter.requests.single.headers['If-Match'], board.version);
    expect(find.byType(TrackerFieldSettingsSheet), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
      'numeric and checkbox edits preserve JSON types and validate invalid numbers',
      (tester) async {
    var values = <String, Object?>{};
    final form = GlobalKey<FormState>();
    await tester.pumpWidget(MaterialApp(
        theme: kaedeTheme(),
        home: Scaffold(
            body: StatefulBuilder(
                builder: (context, setState) => Form(
                    key: form,
                    child: TrackerCustomFields(
                        fields: const [
                          TrackerField(
                              id: 'hours', name: 'Hours', type: 'number'),
                          TrackerField(
                              id: 'approved',
                              name: 'Approved',
                              type: 'checkbox')
                        ],
                        values: values,
                        channel: EntityRef.parse('3@chat.example'),
                        onChanged: (next) =>
                            setState(() => values = next)))))));
    await tester.enterText(find.byType(TextFormField), 'NaN');
    expect(form.currentState!.validate(), isFalse);
    await tester.enterText(find.byType(TextFormField), '3.5');
    await tester.tap(find.text('Approved'));
    await tester.pumpAndSettle();
    expect(form.currentState!.validate(), isTrue);
    expect(values, {'hours': 3.5, 'approved': true});
    await tester.enterText(find.byType(TextFormField), '');
    expect(values, {'approved': true});
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
