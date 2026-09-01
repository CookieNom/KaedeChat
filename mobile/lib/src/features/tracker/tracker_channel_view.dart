import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/shared/remote_media.dart';
import 'package:kaede_mobile/src/gateway/gateway_client.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:uuid/uuid.dart';

const _trackerEvents = <String>{
  'CHANNEL_ACCESS_REVOKED',
  'CHANNEL_PERMISSION_UPDATE',
  'TRACKER_BOARD_UPDATE',
  'TRACKER_LANE_CREATE',
  'TRACKER_LANE_UPDATE',
  'TRACKER_LANE_DELETE',
  'TRACKER_TASK_CREATE',
  'TRACKER_TASK_UPDATE',
  'TRACKER_TASK_DELETE',
};

bool trackerGatewayEventInvalidatesOpenEditors(GatewayEvent event) => const {
      'CHANNEL_ACCESS_REVOKED',
      'CHANNEL_PERMISSION_UPDATE',
      'READY',
      'RESUMED',
      'INVALID_SESSION',
      'GATEWAY_SEQUENCE_GAP',
    }.contains(event.name);

bool trackerRefreshMustDiscardBoard(Object error) =>
    error is KaedeException &&
    (error.status == 401 || error.status == 403 || error.status == 404);

bool trackerTaskCanEdit(
  TrackerBoard board,
  TrackerTask task,
  EntityRef? actor,
) {
  if (board.allows(TrackerPermission.manageTasks)) return true;
  if (!board.allows(TrackerPermission.editOwnTasks) || actor == null) {
    return false;
  }
  return task.creator.ref == actor || task.assignee?.ref == actor;
}

bool trackerTaskCanAssign(
  TrackerBoard board,
  TrackerTask task,
  EntityRef? actor,
) =>
    board.allows(TrackerPermission.assignTasks) ||
    (actor != null && (task.assignee == null || task.assignee?.ref == actor));

bool trackerTaskCanOpenEditor(
  TrackerBoard board,
  TrackerTask task,
  EntityRef? actor,
) =>
    trackerTaskCanEdit(board, task, actor) ||
    trackerTaskCanAssign(board, task, actor);

bool trackerLaneCanDelete(TrackerBoard board, TrackerLane lane) =>
    board.lanes.length > 1 && board.tasksFor(lane).isEmpty;

bool trackerGatewayEventMatchesChannel(
  GatewayEvent event,
  EntityRef channel,
) {
  if (!_trackerEvents.contains(event.name)) return false;
  final id = '${event.data['channel_id'] ?? ''}';
  final domain = '${event.data['channel_domain'] ?? ''}';
  return id == channel.id.value && domain == channel.domain.value;
}

String trackerPriorityLabel(TrackerPriority priority) => switch (priority) {
      TrackerPriority.none => 'No priority',
      TrackerPriority.low => 'Low',
      TrackerPriority.medium => 'Medium',
      TrackerPriority.high => 'High',
      TrackerPriority.urgent => 'Urgent',
    };

final class TrackerChannelView extends ConsumerStatefulWidget {
  const TrackerChannelView({super.key, required this.channel});

  final KaedeChannel channel;

  @override
  ConsumerState<TrackerChannelView> createState() => _TrackerChannelViewState();
}

final class _TrackerChannelViewState extends ConsumerState<TrackerChannelView> {
  TrackerBoard? _board;
  StreamSubscription<GatewayEvent>? _gatewaySubscription;
  Timer? _gatewayRefresh;
  String? _error;
  var _loading = true;
  var _mutating = false;
  var _requestGeneration = 0;
  var _trackerOverlayGeneration = 0;
  var _trackerOverlayOpen = false;
  Route<dynamic>? _trackerOverlayRoute;
  NavigatorState? _trackerOverlayNavigator;
  final Set<EntityRef> _collapsed = <EntityRef>{};

  KaedeRepository get _repository =>
      ref.read(mobileControllerProvider.notifier).repository;

  @override
  void initState() {
    super.initState();
    final controller = ref.read(mobileControllerProvider.notifier);
    _gatewaySubscription = controller.gateway.events.listen(_onGatewayEvent);
    unawaited(_load());
  }

  @override
  void didUpdateWidget(covariant TrackerChannelView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.channel.ref != widget.channel.ref) {
      _dismissTrackerOverlay();
      _board = null;
      _error = null;
      _collapsed.clear();
      _loading = true;
      unawaited(_load());
    }
  }

  @override
  void dispose() {
    _dismissTrackerOverlay(defer: true);
    _requestGeneration += 1;
    _gatewayRefresh?.cancel();
    unawaited(_gatewaySubscription?.cancel());
    super.dispose();
  }

  void _onGatewayEvent(GatewayEvent event) {
    final resync = const {
      'READY',
      'RESUMED',
      'INVALID_SESSION',
      'GATEWAY_SEQUENCE_GAP',
    }.contains(event.name);
    if (!resync &&
        !trackerGatewayEventMatchesChannel(event, widget.channel.ref)) {
      return;
    }
    if (trackerGatewayEventInvalidatesOpenEditors(event)) {
      _dismissTrackerOverlay();
    }
    _gatewayRefresh?.cancel();
    _gatewayRefresh = Timer(Duration(milliseconds: 180), () {
      if (mounted) unawaited(_load(background: true));
    });
  }

  Future<T?> _showTrackerOverlay<T>({
    required Future<T?> Function(WidgetBuilder builder) show,
    required WidgetBuilder builder,
  }) async {
    final generation = ++_trackerOverlayGeneration;
    _trackerOverlayOpen = true;
    _trackerOverlayNavigator = Navigator.of(context);
    try {
      return await show((overlayContext) {
        if (_trackerOverlayOpen && generation == _trackerOverlayGeneration) {
          _trackerOverlayRoute = ModalRoute.of(overlayContext);
          _trackerOverlayNavigator = Navigator.of(overlayContext);
        }
        return builder(overlayContext);
      });
    } finally {
      if (generation == _trackerOverlayGeneration) {
        _trackerOverlayOpen = false;
        _trackerOverlayRoute = null;
        _trackerOverlayNavigator = null;
      }
    }
  }

  void _dismissTrackerOverlay({bool defer = false}) {
    if (!_trackerOverlayOpen) return;
    final route = _trackerOverlayRoute;
    final navigator = _trackerOverlayNavigator;
    _trackerOverlayOpen = false;
    _trackerOverlayRoute = null;
    _trackerOverlayNavigator = null;
    _trackerOverlayGeneration += 1;

    void closeOwnedRoute() {
      if (navigator == null || !navigator.mounted) return;
      if (route != null && route.isActive) {
        // Close transient routes opened from the editor (for example, its date
        // picker) before removing the owned route itself.
        navigator.popUntil((candidate) => identical(candidate, route));
        if (route.isActive) navigator.removeRoute(route);
        return;
      }

      // Modal helpers push synchronously but may not have built their child
      // yet. In that narrow window the owned route is still topmost.
      unawaited(navigator.maybePop());
    }

    if (defer) {
      WidgetsBinding.instance.addPostFrameCallback((_) => closeOwnedRoute());
    } else {
      closeOwnedRoute();
    }
  }

  Future<void> _load({bool background = false}) async {
    final generation = ++_requestGeneration;
    if (!background && _board == null && mounted) {
      setState(() {
        _loading = true;
        _error = null;
      });
    }
    try {
      final board = await _repository.trackerBoard(widget.channel.ref);
      if (!mounted || generation != _requestGeneration) return;
      setState(() {
        _board = board;
        _loading = false;
        _error = null;
        _collapsed.removeWhere(
          (lane) => !board.lanes.any((candidate) => candidate.ref == lane),
        );
      });
    } on Object catch (error) {
      if (!mounted || generation != _requestGeneration) return;
      setState(() {
        _loading = false;
        _error = userFacingError(error, summary: 'Could not load this tracker');
        // Never leave tracker contents visible after access is revoked. For a
        // transient background failure, retain the last board but show the
        // persistent retry banner so stale data is not presented as current.
        if (trackerRefreshMustDiscardBoard(error)) _board = null;
      });
    }
  }

  Future<void> _mutate(
    String failureSummary,
    Future<void> Function() mutation, {
    String? success,
    Future<void> Function()? retry,
  }) async {
    if (_mutating) return;
    setState(() => _mutating = true);
    try {
      await mutation();
      await _load(background: true);
      if (mounted && success != null) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(success)),
        );
      }
    } on Object catch (error) {
      final conflict = error is KaedeException &&
          (error.status == 412 || error.status == 428);
      if (conflict) await _load(background: true);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(conflict
              ? 'This tracker changed on another client. It has been refreshed; try again.'
              : userFacingError(error, summary: failureSummary)),
          backgroundColor: context.kaede.dangerSoft,
          action: retry == null
              ? null
              : SnackBarAction(
                  label: 'Retry',
                  onPressed: () => unawaited(retry()),
                ),
        ));
      }
    } finally {
      if (mounted) setState(() => _mutating = false);
    }
  }

  Future<void> _createTask([TrackerLane? initialLane]) async {
    final board = _board;
    if (board == null || board.lanes.isEmpty) return;
    final state = ref.read(mobileControllerProvider);
    final draft = await _showTrackerOverlay<TrackerTaskDraft>(
      show: (builder) => showModalBottomSheet<TrackerTaskDraft>(
        context: context,
        isScrollControlled: true,
        useSafeArea: true,
        showDragHandle: true,
        builder: builder,
      ),
      builder: (_) => TrackerTaskEditorSheet(
        lanes: board.lanes,
        initialLane: initialLane ?? board.lanes.first,
        actor: state.user,
        members: state.activeGuildMembers,
        canAssignOthers: board.allows(TrackerPermission.assignTasks),
      ),
    );
    if (draft == null) return;
    await _submitCreateTask(draft);
  }

  Future<void> _submitCreateTask(TrackerTaskDraft draft) async {
    await _mutate('Could not create the task', () async {
      await _repository.createTrackerTask(
        widget.channel.ref,
        lane: draft.lane,
        title: draft.title,
        description: draft.description,
        priority: draft.priority,
        dueAt: draft.dueAt,
        assignee: draft.assignee,
        clientNonce: draft.clientNonce,
      );
    }, success: 'Task created', retry: () => _submitCreateTask(draft));
  }

  Future<void> _editTask(TrackerTask task) async {
    final board = _board;
    if (board == null) return;
    final state = ref.read(mobileControllerProvider);
    final canEditDetails = trackerTaskCanEdit(board, task, state.user?.ref);
    if (!canEditDetails &&
        !trackerTaskCanAssign(board, task, state.user?.ref)) {
      return;
    }
    final lane = board.lanes
        .where((candidate) => candidate.ref == task.laneRef)
        .firstOrNull;
    if (lane == null) return;
    final draft = await _showTrackerOverlay<TrackerTaskDraft>(
      show: (builder) => showModalBottomSheet<TrackerTaskDraft>(
        context: context,
        isScrollControlled: true,
        useSafeArea: true,
        showDragHandle: true,
        builder: builder,
      ),
      builder: (_) => TrackerTaskEditorSheet(
        lanes: board.lanes,
        initialLane: lane,
        task: task,
        actor: state.user,
        members: state.activeGuildMembers,
        canAssignOthers: board.allows(TrackerPermission.assignTasks),
        canEditDetails: canEditDetails,
      ),
    );
    if (draft == null) return;
    if (!canEditDetails && draft.assignee == task.assignee?.ref) return;
    if (_mutating) return;
    setState(() => _mutating = true);
    var detailsSaved = false;
    try {
      final updated = canEditDetails
          ? await _repository.updateTrackerTask(
              widget.channel.ref,
              task.ref,
              task.version,
              title: draft.title,
              description: draft.description,
              clearDescription: draft.description == null,
              priority: draft.priority,
              dueAt: draft.dueAt,
              clearDueAt: draft.dueAt == null,
              assignee: draft.assignee,
              clearAssignee: draft.assignee == null,
            )
          : await _repository.updateTrackerTask(
              widget.channel.ref,
              task.ref,
              task.version,
              assignee: draft.assignee,
              clearAssignee: draft.assignee == null,
            );
      detailsSaved = true;
      if (canEditDetails && draft.lane != task.laneRef) {
        await _repository.moveTrackerTask(
          widget.channel.ref,
          task.ref,
          updated.version,
          lane: draft.lane,
          position: board.tasks
              .where((candidate) => candidate.laneRef == draft.lane)
              .length,
        );
      }
      await _load(background: true);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Task saved')),
        );
      }
    } on Object catch (error) {
      final conflict = error is KaedeException &&
          (error.status == 412 || error.status == 428);
      await _load(background: true);
      if (mounted) {
        final message = detailsSaved
            ? 'Task details were saved, but its lane could not be changed. The tracker has been refreshed.'
            : conflict
                ? 'This task changed on another client. It has been refreshed; review it before trying again.'
                : userFacingError(error, summary: 'Could not save the task');
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(message),
          backgroundColor: context.kaede.dangerSoft,
        ));
      }
    } finally {
      if (mounted) setState(() => _mutating = false);
    }
  }

  Future<void> _moveTask(TrackerTask task) async {
    final board = _board;
    if (board == null) return;
    final target = await _showTrackerOverlay<TrackerTaskMoveDraft>(
      show: (builder) => showModalBottomSheet<TrackerTaskMoveDraft>(
        context: context,
        useSafeArea: true,
        showDragHandle: true,
        isScrollControlled: true,
        builder: builder,
      ),
      builder: (sheetContext) => TrackerTaskMoveSheet(
        task: task,
        lanes: board.lanes,
        tasks: board.tasks,
      ),
    );
    if (target == null ||
        (target.lane == task.laneRef && target.position == task.position)) {
      return;
    }
    await _mutate('Could not move the task', () async {
      await _repository.moveTrackerTask(
        widget.channel.ref,
        task.ref,
        task.version,
        lane: target.lane,
        position: target.position,
      );
    }, success: 'Task moved');
  }

  Future<void> _viewTask(TrackerTask task) async {
    final board = _board;
    if (board == null) return;
    final lane = board.lanes
        .where((candidate) => candidate.ref == task.laneRef)
        .firstOrNull;
    if (lane == null) return;
    final actor = ref.read(mobileControllerProvider).user?.ref;
    final canEdit = trackerTaskCanEdit(board, task, actor);
    final shouldEdit = await _showTrackerOverlay<bool>(
      show: (builder) => showModalBottomSheet<bool>(
        context: context,
        useSafeArea: true,
        isScrollControlled: true,
        showDragHandle: true,
        builder: builder,
      ),
      builder: (_) => TrackerTaskDetailsSheet(
        task: task,
        lane: lane,
        canEdit: canEdit || trackerTaskCanAssign(board, task, actor),
        assignmentOnly: !canEdit,
      ),
    );
    if (shouldEdit == true && mounted) {
      final latest = _board?.tasks
          .where((candidate) => candidate.ref == task.ref)
          .firstOrNull;
      if (latest != null) await _editTask(latest);
    }
  }

  Future<void> _toggleCompleted(TrackerTask task) async {
    final board = _board;
    if (board == null) return;
    final target = board.lanes
        .where((lane) => lane.completed != task.completed)
        .firstOrNull;
    if (target == null) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(
            'Add both an active lane and a completed lane to use this shortcut.'),
      ));
      return;
    }
    await _mutate(
      task.completed
          ? 'Could not reopen the task'
          : 'Could not complete the task',
      () async {
        await _repository.moveTrackerTask(
          widget.channel.ref,
          task.ref,
          task.version,
          lane: target.ref,
          position: board.tasksFor(target).length,
        );
      },
      success: task.completed ? 'Task reopened' : 'Task completed',
    );
  }

  Future<void> _deleteTask(TrackerTask task) async {
    final confirmed = await _confirm(
      'Delete ${task.key}?',
      'This permanently removes “${task.title}”.',
      destructive: true,
    );
    if (!confirmed) return;
    await _mutate('Could not delete the task', () async {
      await _repository.deleteTrackerTask(
        widget.channel.ref,
        task.ref,
        task.version,
      );
    }, success: 'Task deleted');
  }

  Future<void> _editLane([TrackerLane? lane]) async {
    final board = _board;
    if (board == null) return;
    final draft = await _showTrackerOverlay<TrackerLaneDraft>(
      show: (builder) => showModalBottomSheet<TrackerLaneDraft>(
        context: context,
        isScrollControlled: true,
        useSafeArea: true,
        showDragHandle: true,
        builder: builder,
      ),
      builder: (_) => TrackerLaneEditorSheet(lane: lane),
    );
    if (draft == null) return;
    await _mutate(
      lane == null ? 'Could not create the lane' : 'Could not save the lane',
      () async {
        if (lane == null) {
          await _repository.createTrackerLane(
            widget.channel.ref,
            name: draft.name,
            color: draft.color,
            kind: draft.kind,
            completed: draft.completed,
          );
        } else {
          await _repository.updateTrackerLane(
            widget.channel.ref,
            lane.ref,
            lane.version,
            name: draft.name,
            color: draft.color,
            kind: draft.kind,
            completed: draft.completed,
          );
        }
      },
      success: lane == null ? 'Lane created' : 'Lane saved',
    );
  }

  Future<void> _moveLane(TrackerLane lane, int position) async {
    await _mutate('Could not reorder the lane', () async {
      await _repository.moveTrackerLane(
        widget.channel.ref,
        lane.ref,
        lane.version,
        position,
      );
    });
  }

  Future<void> _deleteLane(TrackerLane lane) async {
    final board = _board;
    if (board == null) return;
    if (!trackerLaneCanDelete(board, lane)) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(board.lanes.length <= 1
            ? 'A tracker must keep at least one lane.'
            : 'Move or delete all tasks in this lane first.'),
      ));
      return;
    }
    final confirmed = await _confirm(
      'Delete ${lane.name}?',
      'This permanently removes the lane.',
      destructive: true,
    );
    if (!confirmed) return;
    await _mutate('Could not delete the lane', () async {
      await _repository.deleteTrackerLane(
        widget.channel.ref,
        lane.ref,
        lane.version,
      );
    }, success: 'Lane deleted');
  }

  Future<void> _editBoard() async {
    final board = _board;
    if (board == null) return;
    final controller = TextEditingController(text: board.keyPrefix);
    final formKey = GlobalKey<FormState>();
    final prefix = await _showTrackerOverlay<String>(
      show: (builder) => showDialog<String>(
        context: context,
        builder: builder,
      ),
      builder: (dialogContext) => AlertDialog(
        title: Text('Tracker settings'),
        content: Form(
          key: formKey,
          child: TextFormField(
            key: ValueKey('tracker-prefix-field'),
            controller: controller,
            autofocus: true,
            maxLength: 10,
            textCapitalization: TextCapitalization.characters,
            decoration: InputDecoration(
              labelText: 'Task key prefix',
              helperText: '2–10 letters or digits; starts with a letter.',
              prefixIcon: Icon(Icons.tag_rounded),
            ),
            validator: (value) => RegExp(r'^[A-Za-z][A-Za-z0-9]{1,9}$')
                    .hasMatch(value?.trim() ?? '')
                ? null
                : 'Enter a valid key prefix',
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext),
            child: Text('Cancel'),
          ),
          FilledButton(
            onPressed: () {
              if (formKey.currentState?.validate() == true) {
                Navigator.pop(
                    dialogContext, controller.text.trim().toUpperCase());
              }
            },
            child: Text('Save'),
          ),
        ],
      ),
    );
    controller.dispose();
    if (prefix == null || prefix == board.keyPrefix) return;
    await _mutate('Could not update tracker settings', () async {
      await _repository.updateTrackerBoard(
        widget.channel.ref,
        board.version,
        keyPrefix: prefix,
      );
    }, success: 'Tracker settings saved');
  }

  Future<bool> _confirm(
    String title,
    String message, {
    bool destructive = false,
  }) async =>
      await _showTrackerOverlay<bool>(
        show: (builder) => showDialog<bool>(
          context: context,
          builder: builder,
        ),
        builder: (dialogContext) => AlertDialog(
          title: Text(title),
          content: Text(message),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext, false),
              child: Text('Cancel'),
            ),
            FilledButton(
              style: destructive
                  ? FilledButton.styleFrom(
                      backgroundColor: context.kaede.danger)
                  : null,
              onPressed: () => Navigator.pop(dialogContext, true),
              child: Text(destructive ? 'Delete' : 'Continue'),
            ),
          ],
        ),
      ) ??
      false;

  @override
  Widget build(BuildContext context) {
    final board = _board;
    if (_loading && board == null) {
      return Center(
        child: Semantics(
          liveRegion: true,
          label: 'Loading task tracker',
          child: CircularProgressIndicator(),
        ),
      );
    }
    if (board == null) {
      return _TrackerFailure(
        message: _error ?? 'This tracker is unavailable.',
        retry: _load,
      );
    }
    final actor =
        ref.watch(mobileControllerProvider.select((state) => state.user?.ref));
    final canCreate = board.allows(TrackerPermission.createTasks);
    final canManage = board.allows(TrackerPermission.manageTracker);
    return ColoredBox(
      color: context.kaede.canvas,
      child: Column(
        children: [
          _TrackerToolbar(
            title: widget.channel.name?.trim().isNotEmpty == true
                ? widget.channel.name!.trim()
                : 'Task tracker',
            taskCount: board.tasks.length,
            busy: _mutating,
            canCreate: canCreate,
            canManage: canManage,
            createTask: _createTask,
            createLane: () => _editLane(),
            settings: _editBoard,
            refresh: () => _load(),
          ),
          if (_error != null)
            MaterialBanner(
              content: Text(_error!),
              actions: [
                TextButton(onPressed: _load, child: Text('Retry')),
              ],
            ),
          Expanded(
            child: RefreshIndicator(
              onRefresh: _load,
              child: board.lanes.isEmpty
                  ? ListView(
                      physics: AlwaysScrollableScrollPhysics(),
                      children: [
                        _TrackerEmpty(
                          icon: Icons.view_kanban_outlined,
                          title: 'No lanes yet',
                          message: canManage
                              ? 'Create a lane to start organizing tasks.'
                              : 'A tracker manager needs to create the first lane.',
                          action: canManage ? () => _editLane() : null,
                          actionLabel: 'Create lane',
                        ),
                      ],
                    )
                  : ListView.builder(
                      key: PageStorageKey<String>(
                        'tracker-${widget.channel.ref.wire}',
                      ),
                      physics: AlwaysScrollableScrollPhysics(),
                      padding: EdgeInsets.fromLTRB(12, 10, 12, 100),
                      itemCount: board.lanes.length,
                      itemBuilder: (context, index) {
                        final lane = board.lanes[index];
                        final tasks = board.tasksFor(lane);
                        final collapsed = _collapsed.contains(lane.ref);
                        return _TrackerLaneSection(
                          lane: lane,
                          tasks: tasks,
                          collapsed: collapsed,
                          canManage: canManage,
                          canDeleteLane: trackerLaneCanDelete(board, lane),
                          canCreate: canCreate,
                          busy: _mutating,
                          actor: actor,
                          board: board,
                          toggle: () => setState(() {
                            if (!_collapsed.add(lane.ref)) {
                              _collapsed.remove(lane.ref);
                            }
                          }),
                          createTask: () => _createTask(lane),
                          editLane: () => _editLane(lane),
                          moveLaneUp: index > 0
                              ? () => _moveLane(lane, index - 1)
                              : null,
                          moveLaneDown: index + 1 < board.lanes.length
                              ? () => _moveLane(lane, index + 1)
                              : null,
                          deleteLane: () => _deleteLane(lane),
                          viewTask: _viewTask,
                          editTask: _editTask,
                          moveTask: _moveTask,
                          toggleTask: _toggleCompleted,
                          deleteTask: _deleteTask,
                        );
                      },
                    ),
            ),
          ),
        ],
      ),
    );
  }
}

final class _TrackerToolbar extends StatelessWidget {
  const _TrackerToolbar({
    required this.title,
    required this.taskCount,
    required this.busy,
    required this.canCreate,
    required this.canManage,
    required this.createTask,
    required this.createLane,
    required this.settings,
    required this.refresh,
  });

  final String title;
  final int taskCount;
  final bool busy;
  final bool canCreate;
  final bool canManage;
  final VoidCallback createTask;
  final VoidCallback createLane;
  final VoidCallback settings;
  final VoidCallback refresh;

  @override
  Widget build(BuildContext context) => Material(
        color: context.kaede.panel,
        child: SafeArea(
          bottom: false,
          child: Padding(
            padding: EdgeInsets.fromLTRB(16, 10, 8, 10),
            child: Row(
              children: [
                Icon(Icons.view_kanban_rounded, color: context.kaede.coral),
                SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        title,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: Theme.of(context)
                            .textTheme
                            .titleLarge
                            ?.copyWith(fontWeight: FontWeight.w900),
                      ),
                      Text(
                        '$taskCount task${taskCount == 1 ? '' : 's'}',
                        style: Theme.of(context).textTheme.bodySmall,
                      ),
                    ],
                  ),
                ),
                if (canCreate)
                  FilledButton.icon(
                    key: ValueKey('tracker-create-task'),
                    onPressed: busy ? null : createTask,
                    icon: Icon(Icons.add_task_rounded, size: 19),
                    label: Text('Task'),
                  ),
                PopupMenuButton<String>(
                  tooltip: 'Tracker actions',
                  enabled: !busy,
                  onSelected: (action) => switch (action) {
                    'lane' => createLane(),
                    'settings' => settings(),
                    _ => refresh(),
                  },
                  itemBuilder: (_) => [
                    PopupMenuItem(
                      value: 'refresh',
                      child: ListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        leading: Icon(Icons.refresh_rounded),
                        title: Text('Refresh'),
                      ),
                    ),
                    if (canManage)
                      PopupMenuItem(
                        value: 'lane',
                        child: ListTile(
                          dense: true,
                          contentPadding: EdgeInsets.zero,
                          leading: Icon(Icons.view_week_outlined),
                          title: Text('Create lane'),
                        ),
                      ),
                    if (canManage)
                      PopupMenuItem(
                        value: 'settings',
                        child: ListTile(
                          dense: true,
                          contentPadding: EdgeInsets.zero,
                          leading: Icon(Icons.settings_outlined),
                          title: Text('Tracker settings'),
                        ),
                      ),
                  ],
                ),
              ],
            ),
          ),
        ),
      );
}

final class _TrackerLaneSection extends StatelessWidget {
  const _TrackerLaneSection({
    required this.lane,
    required this.tasks,
    required this.collapsed,
    required this.canManage,
    required this.canDeleteLane,
    required this.canCreate,
    required this.busy,
    required this.actor,
    required this.board,
    required this.toggle,
    required this.createTask,
    required this.editLane,
    required this.moveLaneUp,
    required this.moveLaneDown,
    required this.deleteLane,
    required this.viewTask,
    required this.editTask,
    required this.moveTask,
    required this.toggleTask,
    required this.deleteTask,
  });

  final TrackerLane lane;
  final List<TrackerTask> tasks;
  final bool collapsed;
  final bool canManage;
  final bool canDeleteLane;
  final bool canCreate;
  final bool busy;
  final EntityRef? actor;
  final TrackerBoard board;
  final VoidCallback toggle;
  final VoidCallback createTask;
  final VoidCallback editLane;
  final VoidCallback? moveLaneUp;
  final VoidCallback? moveLaneDown;
  final VoidCallback deleteLane;
  final ValueChanged<TrackerTask> viewTask;
  final ValueChanged<TrackerTask> editTask;
  final ValueChanged<TrackerTask> moveTask;
  final ValueChanged<TrackerTask> toggleTask;
  final ValueChanged<TrackerTask> deleteTask;

  @override
  Widget build(BuildContext context) {
    final color = Color(0xFF000000 | lane.color);
    return Semantics(
      container: true,
      label:
          '${lane.name}, ${tasks.length} tasks, ${collapsed ? 'collapsed' : 'expanded'}',
      child: Padding(
        padding: EdgeInsets.only(bottom: 14),
        child: Column(
          children: [
            Material(
              color: context.kaede.panel,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(KaedeRadius.medium),
                side: BorderSide(color: context.kaede.border),
              ),
              clipBehavior: Clip.antiAlias,
              child: InkWell(
                onTap: toggle,
                child: IntrinsicHeight(
                  child: Row(
                    children: [
                      SizedBox(width: 4, child: ColoredBox(color: color)),
                      SizedBox(width: 8),
                      SizedBox.square(
                        dimension: 44,
                        child: Icon(
                          collapsed
                              ? Icons.chevron_right_rounded
                              : Icons.expand_more_rounded,
                        ),
                      ),
                      Icon(_laneIcon(lane), color: color, size: 21),
                      SizedBox(width: 9),
                      Expanded(
                        child: Text(
                          lane.name,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: Theme.of(context)
                              .textTheme
                              .titleLarge
                              ?.copyWith(fontWeight: FontWeight.w800),
                        ),
                      ),
                      Container(
                        padding:
                            EdgeInsets.symmetric(horizontal: 9, vertical: 4),
                        decoration: BoxDecoration(
                          color: context.kaede.raised,
                          borderRadius:
                              BorderRadius.circular(KaedeRadius.small),
                        ),
                        child: Text('${tasks.length}'),
                      ),
                      if (canManage)
                        PopupMenuButton<String>(
                          tooltip: 'Manage ${lane.name}',
                          enabled: !busy,
                          onSelected: (action) => switch (action) {
                            'edit' => editLane(),
                            'up' => moveLaneUp?.call(),
                            'down' => moveLaneDown?.call(),
                            'delete' => deleteLane(),
                            _ => null,
                          },
                          itemBuilder: (_) => [
                            PopupMenuItem(
                                value: 'edit', child: Text('Edit lane')),
                            PopupMenuItem(
                              value: 'up',
                              enabled: moveLaneUp != null,
                              child: Text('Move up'),
                            ),
                            PopupMenuItem(
                              value: 'down',
                              enabled: moveLaneDown != null,
                              child: Text('Move down'),
                            ),
                            PopupMenuDivider(),
                            PopupMenuItem(
                              value: 'delete',
                              enabled: canDeleteLane,
                              child: Text('Delete lane',
                                  style:
                                      TextStyle(color: context.kaede.danger)),
                            ),
                          ],
                        )
                      else
                        SizedBox(width: 10),
                    ],
                  ),
                ),
              ),
            ),
            if (!collapsed) ...[
              if (tasks.isEmpty)
                Padding(
                  padding: EdgeInsets.fromLTRB(12, 14, 12, 6),
                  child: Row(
                    children: [
                      Icon(Icons.inbox_outlined,
                          size: 18, color: context.kaede.muted),
                      SizedBox(width: 8),
                      Expanded(
                        child: Text('No tasks in this lane.',
                            style: TextStyle(color: context.kaede.muted)),
                      ),
                      if (canCreate)
                        TextButton.icon(
                          onPressed: busy ? null : createTask,
                          icon: Icon(Icons.add_rounded),
                          label: Text('Add task'),
                        ),
                    ],
                  ),
                )
              else
                for (final task in tasks)
                  _TrackerTaskRow(
                    task: task,
                    lane: lane,
                    canEdit: trackerTaskCanEdit(board, task, actor),
                    canAssign: trackerTaskCanAssign(board, task, actor),
                    busy: busy,
                    open: () => viewTask(task),
                    edit: () => editTask(task),
                    move: () => moveTask(task),
                    toggle: () => toggleTask(task),
                    delete: () => deleteTask(task),
                  ),
              if (tasks.isNotEmpty && canCreate)
                Align(
                  alignment: Alignment.centerLeft,
                  child: TextButton.icon(
                    onPressed: busy ? null : createTask,
                    icon: Icon(Icons.add_rounded),
                    label: Text('Add task to ${lane.name}'),
                  ),
                ),
            ],
          ],
        ),
      ),
    );
  }
}

final class _TrackerTaskRow extends StatelessWidget {
  const _TrackerTaskRow({
    required this.task,
    required this.lane,
    required this.canEdit,
    required this.canAssign,
    required this.busy,
    required this.open,
    required this.edit,
    required this.move,
    required this.toggle,
    required this.delete,
  });

  final TrackerTask task;
  final TrackerLane lane;
  final bool canEdit;
  final bool canAssign;
  final bool busy;
  final VoidCallback open;
  final VoidCallback edit;
  final VoidCallback move;
  final VoidCallback toggle;
  final VoidCallback delete;

  @override
  Widget build(BuildContext context) {
    final due = task.dueAt;
    final overdue =
        !task.completed && due != null && due.isBefore(DateTime.now());
    final priorityColor = _priorityColor(context, task.priority);
    final dueLabel = due == null
        ? 'no due date'
        : 'due ${DateFormat.yMMMd().add_jm().format(due.toLocal())}'
            '${overdue ? ', overdue' : ''}';
    return Semantics(
      button: true,
      label:
          '${task.key}, ${task.title}, ${trackerPriorityLabel(task.priority)}'
          ', status ${lane.name}, $dueLabel'
          '${task.assignee == null ? ', unassigned' : ', assigned to ${task.assignee!.name}'}'
          ', open task details',
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          onTap: !busy ? open : null,
          borderRadius: BorderRadius.circular(KaedeRadius.small),
          child: Padding(
            padding: EdgeInsets.fromLTRB(10, 8, 2, 8),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                IconButton(
                  tooltip: task.completed ? 'Reopen task' : 'Complete task',
                  onPressed: canEdit && !busy ? toggle : null,
                  icon: Icon(
                    task.completed
                        ? Icons.check_circle_rounded
                        : _laneIcon(lane),
                    color: task.completed
                        ? context.kaede.mint
                        : Color(0xFF000000 | lane.color),
                    size: 20,
                  ),
                ),
                SizedBox(
                  width: 62,
                  child: Padding(
                    padding: EdgeInsets.only(top: 14),
                    child: Text(
                      task.key,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: Theme.of(context).textTheme.labelSmall,
                    ),
                  ),
                ),
                Expanded(
                  child: Padding(
                    padding: EdgeInsets.only(top: 10),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          task.title,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            decoration: task.completed
                                ? TextDecoration.lineThrough
                                : null,
                            color: task.completed
                                ? context.kaede.muted
                                : context.kaede.text,
                          ),
                        ),
                        if (task.priority != TrackerPriority.none ||
                            due != null) ...[
                          SizedBox(height: 5),
                          Wrap(
                            spacing: 7,
                            runSpacing: 5,
                            children: [
                              if (task.priority != TrackerPriority.none)
                                _MetaChip(
                                  label: trackerPriorityLabel(task.priority),
                                  color: priorityColor,
                                ),
                              if (due != null)
                                _MetaChip(
                                  icon: Icons.event_outlined,
                                  label:
                                      DateFormat.MMMd().format(due.toLocal()),
                                  color: overdue
                                      ? context.kaede.danger
                                      : context.kaede.muted,
                                ),
                            ],
                          ),
                        ],
                      ],
                    ),
                  ),
                ),
                SizedBox(width: 6),
                _AssigneeAvatar(user: task.assignee),
                if (canEdit || canAssign)
                  PopupMenuButton<String>(
                    tooltip: 'Actions for ${task.key}',
                    enabled: !busy,
                    onSelected: (action) => switch (action) {
                      'edit' => edit(),
                      'move' => move(),
                      'toggle' => toggle(),
                      'delete' => delete(),
                      _ => null,
                    },
                    itemBuilder: (_) => [
                      PopupMenuItem(
                        value: 'edit',
                        child: Text(canEdit ? 'Edit task' : 'Assign task'),
                      ),
                      if (canEdit) ...[
                        PopupMenuItem(
                            value: 'move', child: Text('Move or reorder')),
                        PopupMenuItem(
                          value: 'toggle',
                          child: Text(
                              task.completed ? 'Reopen task' : 'Mark complete'),
                        ),
                        PopupMenuDivider(),
                        PopupMenuItem(
                          value: 'delete',
                          child: Text('Delete task',
                              style: TextStyle(color: context.kaede.danger)),
                        ),
                      ],
                    ],
                  )
                else
                  SizedBox(width: 12),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

final class _MetaChip extends StatelessWidget {
  const _MetaChip({required this.label, required this.color, this.icon});

  final String label;
  final Color color;
  final IconData? icon;

  @override
  Widget build(BuildContext context) => Container(
        padding: EdgeInsets.symmetric(horizontal: 7, vertical: 3),
        decoration: BoxDecoration(
          color: color.withValues(alpha: .14),
          borderRadius: BorderRadius.circular(KaedeRadius.pill),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (icon != null) ...[
              Icon(icon, size: 12, color: color),
              SizedBox(width: 4),
            ],
            Text(
              label,
              style: TextStyle(
                  color: color, fontSize: 11, fontWeight: FontWeight.w700),
            ),
          ],
        ),
      );
}

final class _AssigneeAvatar extends StatelessWidget {
  const _AssigneeAvatar({required this.user});

  final KaedeUser? user;

  @override
  Widget build(BuildContext context) {
    final name = user?.name.trim() ?? '';
    return Tooltip(
      message: user == null ? 'Unassigned' : 'Assigned to $name',
      child: user == null
          ? CircleAvatar(
              radius: 15,
              backgroundColor: context.kaede.raised,
              foregroundColor: context.kaede.muted,
              child: Icon(Icons.person_outline_rounded, size: 16),
            )
          : UserAvatar(user: user!, radius: 15),
    );
  }
}

final class TrackerTaskDraft {
  TrackerTaskDraft({
    required this.lane,
    required this.title,
    required this.priority,
    required this.clientNonce,
    this.description,
    this.dueAt,
    this.assignee,
  });

  final EntityRef lane;
  final String title;
  final String? description;
  final TrackerPriority priority;
  final String clientNonce;
  final DateTime? dueAt;
  final EntityRef? assignee;
}

final class TrackerTaskMoveDraft {
  TrackerTaskMoveDraft({required this.lane, required this.position});

  final EntityRef lane;
  final int position;
}

final class TrackerTaskDetailsSheet extends StatelessWidget {
  const TrackerTaskDetailsSheet({
    super.key,
    required this.task,
    required this.lane,
    required this.canEdit,
    this.assignmentOnly = false,
  });

  final TrackerTask task;
  final TrackerLane lane;
  final bool canEdit;
  final bool assignmentOnly;

  @override
  Widget build(BuildContext context) {
    final description = task.description?.trim();
    final due = task.dueAt;
    return SafeArea(
      child: ConstrainedBox(
        constraints: BoxConstraints(
          maxHeight: MediaQuery.sizeOf(context).height * .9,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Padding(
              padding: EdgeInsets.fromLTRB(20, 0, 8, 12),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(task.key,
                            style: Theme.of(context).textTheme.labelLarge),
                        SizedBox(height: 4),
                        Text(task.title,
                            style: Theme.of(context).textTheme.headlineSmall),
                      ],
                    ),
                  ),
                  IconButton(
                    tooltip: 'Close task details',
                    onPressed: () => Navigator.pop(context, false),
                    icon: Icon(Icons.close_rounded),
                  ),
                ],
              ),
            ),
            Divider(height: 1),
            Flexible(
              child: SingleChildScrollView(
                padding: EdgeInsets.fromLTRB(20, 18, 20, 12),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Text('Description',
                        style: Theme.of(context).textTheme.titleSmall),
                    SizedBox(height: 7),
                    SelectableText(
                      description?.isNotEmpty == true
                          ? description!
                          : 'No description provided.',
                      style: TextStyle(
                        color: description?.isNotEmpty == true
                            ? context.kaede.text
                            : context.kaede.muted,
                      ),
                    ),
                    SizedBox(height: 20),
                    _TaskDetailRow(
                      icon: _laneIcon(lane),
                      label: 'Lane',
                      value: lane.name,
                      color: Color(0xFF000000 | lane.color),
                    ),
                    _TaskDetailRow(
                      icon: Icons.flag_outlined,
                      label: 'Priority',
                      value: trackerPriorityLabel(task.priority),
                      color: _priorityColor(context, task.priority),
                    ),
                    _TaskDetailRow(
                      icon: Icons.event_outlined,
                      label: 'Due',
                      value: due == null
                          ? 'No due date'
                          : DateFormat.yMMMd().add_jm().format(due.toLocal()),
                    ),
                    _TaskDetailRow(
                      icon: Icons.person_outline_rounded,
                      label: 'Assignee',
                      value: task.assignee?.name ?? 'Unassigned',
                    ),
                    _TaskDetailRow(
                      icon: Icons.edit_outlined,
                      label: 'Created by',
                      value: task.creator.name,
                    ),
                  ],
                ),
              ),
            ),
            if (canEdit)
              Padding(
                padding: EdgeInsets.fromLTRB(20, 10, 20, 18),
                child: FilledButton.icon(
                  key: ValueKey('tracker-task-details-edit'),
                  onPressed: () => Navigator.pop(context, true),
                  icon: Icon(Icons.edit_outlined),
                  label: Text(assignmentOnly ? 'Assign task' : 'Edit task'),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

final class _TaskDetailRow extends StatelessWidget {
  const _TaskDetailRow({
    required this.icon,
    required this.label,
    required this.value,
    this.color,
  });

  final IconData icon;
  final String label;
  final String value;
  final Color? color;

  @override
  Widget build(BuildContext context) => Padding(
        padding: EdgeInsets.only(bottom: 12),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(icon, size: 20, color: color ?? context.kaede.muted),
            SizedBox(width: 12),
            SizedBox(
              width: 82,
              child: Text(label, style: TextStyle(color: context.kaede.muted)),
            ),
            Expanded(child: Text(value)),
          ],
        ),
      );
}

final class TrackerTaskMoveSheet extends StatefulWidget {
  const TrackerTaskMoveSheet({
    super.key,
    required this.task,
    required this.lanes,
    required this.tasks,
  });

  final TrackerTask task;
  final List<TrackerLane> lanes;
  final List<TrackerTask> tasks;

  @override
  State<TrackerTaskMoveSheet> createState() => _TrackerTaskMoveSheetState();
}

final class _TrackerTaskMoveSheetState extends State<TrackerTaskMoveSheet> {
  late EntityRef _lane;
  late int _position;

  @override
  void initState() {
    super.initState();
    _lane = widget.task.laneRef;
    final maximum = _tasksFor(_lane).length;
    _position = widget.task.position.clamp(0, maximum);
  }

  List<TrackerTask> _tasksFor(EntityRef lane) => widget.tasks
      .where((task) => task.laneRef == lane && task.ref != widget.task.ref)
      .toList()
    ..sort((left, right) => left.position.compareTo(right.position));

  String _positionLabel(int position, List<TrackerTask> tasks) {
    if (position == 0) return tasks.isEmpty ? 'Only task' : 'At the top';
    if (position == tasks.length) return 'At the bottom';
    return 'After ${tasks[position - 1].key}';
  }

  @override
  Widget build(BuildContext context) {
    final destinationTasks = _tasksFor(_lane);
    final unchanged =
        _lane == widget.task.laneRef && _position == widget.task.position;
    return SafeArea(
      child: Padding(
        padding: EdgeInsets.fromLTRB(
          20,
          0,
          20,
          18 + MediaQuery.viewInsetsOf(context).bottom,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text('Move ${widget.task.key}',
                      style: Theme.of(context).textTheme.headlineSmall),
                ),
                IconButton(
                  tooltip: 'Close',
                  onPressed: () => Navigator.pop(context),
                  icon: Icon(Icons.close_rounded),
                ),
              ],
            ),
            SizedBox(height: 12),
            DropdownButtonFormField<EntityRef>(
              key: ValueKey('tracker-task-move-lane'),
              initialValue: _lane,
              decoration: InputDecoration(
                labelText: 'Lane',
                prefixIcon: Icon(Icons.view_week_outlined),
              ),
              items: [
                for (final lane in widget.lanes)
                  DropdownMenuItem(value: lane.ref, child: Text(lane.name)),
              ],
              onChanged: (lane) {
                if (lane == null) return;
                setState(() {
                  _lane = lane;
                  _position = _tasksFor(lane).length;
                });
              },
            ),
            SizedBox(height: 14),
            DropdownButtonFormField<int>(
              key: ValueKey('tracker-task-move-position-${_lane.wire}'),
              initialValue: _position,
              decoration: InputDecoration(
                labelText: 'Position',
                prefixIcon: Icon(Icons.vertical_align_center_rounded),
              ),
              items: [
                for (var position = 0;
                    position <= destinationTasks.length;
                    position += 1)
                  DropdownMenuItem(
                    value: position,
                    child: Text(_positionLabel(position, destinationTasks)),
                  ),
              ],
              onChanged: (position) {
                if (position != null) setState(() => _position = position);
              },
            ),
            SizedBox(height: 18),
            FilledButton.icon(
              key: ValueKey('tracker-task-move-submit'),
              onPressed: unchanged
                  ? null
                  : () => Navigator.pop(
                        context,
                        TrackerTaskMoveDraft(
                          lane: _lane,
                          position: _position,
                        ),
                      ),
              icon: Icon(Icons.drive_file_move_outline),
              label: Text('Move task'),
            ),
          ],
        ),
      ),
    );
  }
}

final class TrackerTaskEditorSheet extends StatefulWidget {
  const TrackerTaskEditorSheet({
    super.key,
    required this.lanes,
    required this.initialLane,
    required this.members,
    required this.canAssignOthers,
    this.canEditDetails = true,
    this.task,
    this.actor,
  });

  final List<TrackerLane> lanes;
  final TrackerLane initialLane;
  final TrackerTask? task;
  final KaedeUser? actor;
  final List<GuildMember> members;
  final bool canAssignOthers;
  final bool canEditDetails;

  @override
  State<TrackerTaskEditorSheet> createState() => _TrackerTaskEditorSheetState();
}

final class _TrackerTaskEditorSheetState extends State<TrackerTaskEditorSheet> {
  final _formKey = GlobalKey<FormState>();
  late final TextEditingController _title;
  late final TextEditingController _description;
  late EntityRef _lane;
  late TrackerPriority _priority;
  late DateTime? _dueAt;
  late EntityRef? _assignee;
  late final String _clientNonce;

  @override
  void initState() {
    super.initState();
    _title = TextEditingController(text: widget.task?.title ?? '');
    _description = TextEditingController(text: widget.task?.description ?? '');
    _lane = widget.task?.laneRef ?? widget.initialLane.ref;
    _priority = widget.task?.priority ?? TrackerPriority.none;
    _dueAt = widget.task?.dueAt;
    _assignee = widget.task?.assignee?.ref;
    // Keep one idempotency key for the whole editor lifecycle. If a caller
    // retries after a lost response, the server returns the original task.
    _clientNonce = Uuid().v4();
  }

  @override
  void dispose() {
    _title.dispose();
    _description.dispose();
    super.dispose();
  }

  List<KaedeUser> get _assignableUsers {
    final users = <EntityRef, KaedeUser>{};
    if (widget.actor case final actor?) users[actor.ref] = actor;
    if (widget.task?.assignee case final assignee?) {
      users[assignee.ref] = assignee;
    }
    if (widget.canAssignOthers) {
      for (final member in widget.members) {
        users[member.user.ref] = member.user;
      }
    }
    return users.values.toList()
      ..sort((left, right) =>
          left.name.toLowerCase().compareTo(right.name.toLowerCase()));
  }

  Future<void> _pickDate() async {
    final now = DateTime.now();
    final picked = await showDatePicker(
      context: context,
      initialDate: (_dueAt ?? now).toLocal(),
      firstDate: DateTime(now.year - 1),
      lastDate: DateTime(now.year + 10),
      helpText: 'Task due date',
    );
    if (picked != null && mounted) {
      setState(
          () => _dueAt = DateTime(picked.year, picked.month, picked.day, 17));
    }
  }

  void _save() {
    if (_formKey.currentState?.validate() != true) return;
    final description = _description.text.trim();
    Navigator.pop(
      context,
      TrackerTaskDraft(
        lane: _lane,
        title: _title.text.trim(),
        description: description.isEmpty ? null : description,
        priority: _priority,
        clientNonce: _clientNonce,
        dueAt: _dueAt,
        assignee: _assignee,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final media = MediaQuery.of(context);
    final users = _assignableUsers;
    final assigneeEditable = widget.canAssignOthers ||
        widget.task == null ||
        widget.task?.assignee == null ||
        widget.task?.assignee?.ref == widget.actor?.ref;
    return AnimatedPadding(
      duration: Duration(milliseconds: 180),
      padding: EdgeInsets.only(bottom: media.viewInsets.bottom),
      child: ConstrainedBox(
        constraints: BoxConstraints(maxHeight: media.size.height * .9),
        child: Form(
          key: _formKey,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Padding(
                padding: EdgeInsets.fromLTRB(20, 0, 12, 12),
                child: Row(
                  children: [
                    Expanded(
                      child: Text(
                        widget.task == null
                            ? 'Create task'
                            : !widget.canEditDetails
                                ? 'Assign ${widget.task!.key}'
                                : 'Edit ${widget.task!.key}',
                        style: Theme.of(context).textTheme.headlineSmall,
                      ),
                    ),
                    IconButton(
                      tooltip: 'Close',
                      onPressed: () => Navigator.pop(context),
                      icon: Icon(Icons.close_rounded),
                    ),
                  ],
                ),
              ),
              Divider(height: 1),
              Flexible(
                child: SingleChildScrollView(
                  keyboardDismissBehavior:
                      ScrollViewKeyboardDismissBehavior.onDrag,
                  padding: EdgeInsets.fromLTRB(20, 18, 20, 14),
                  child: Column(
                    children: [
                      TextFormField(
                        key: ValueKey('tracker-task-title'),
                        controller: _title,
                        enabled: widget.canEditDetails,
                        autofocus: widget.task == null,
                        maxLength: 200,
                        textCapitalization: TextCapitalization.sentences,
                        decoration: InputDecoration(
                          labelText: 'Title',
                          prefixIcon: Icon(Icons.task_alt_rounded),
                        ),
                        validator: (value) => value?.trim().isEmpty == true
                            ? 'Enter a task title'
                            : null,
                      ),
                      SizedBox(height: 10),
                      TextFormField(
                        controller: _description,
                        enabled: widget.canEditDetails,
                        minLines: 3,
                        maxLines: 7,
                        maxLength: 10000,
                        textCapitalization: TextCapitalization.sentences,
                        decoration: InputDecoration(
                          labelText: 'Description (optional)',
                          alignLabelWithHint: true,
                        ),
                      ),
                      SizedBox(height: 10),
                      DropdownButtonFormField<EntityRef>(
                        key: ValueKey('tracker-task-lane'),
                        initialValue: _lane,
                        decoration: InputDecoration(
                          labelText: 'Lane',
                          prefixIcon: Icon(Icons.view_week_outlined),
                        ),
                        items: [
                          for (final lane in widget.lanes)
                            DropdownMenuItem(
                                value: lane.ref, child: Text(lane.name)),
                        ],
                        onChanged: !widget.canEditDetails
                            ? null
                            : (value) => setState(() => _lane = value ?? _lane),
                      ),
                      SizedBox(height: 14),
                      DropdownButtonFormField<TrackerPriority>(
                        initialValue: _priority,
                        decoration: InputDecoration(
                          labelText: 'Priority',
                          prefixIcon: Icon(Icons.flag_outlined),
                        ),
                        items: [
                          for (final priority in TrackerPriority.values)
                            DropdownMenuItem(
                              value: priority,
                              child: Text(trackerPriorityLabel(priority)),
                            ),
                        ],
                        onChanged: !widget.canEditDetails
                            ? null
                            : (value) =>
                                setState(() => _priority = value ?? _priority),
                      ),
                      SizedBox(height: 14),
                      DropdownButtonFormField<String>(
                        key: ValueKey('tracker-task-assignee'),
                        initialValue: _assignee?.wire ?? '',
                        decoration: InputDecoration(
                          labelText: 'Assignee',
                          helperText: assigneeEditable
                              ? 'One member can own a task.'
                              : 'You do not have permission to reassign this task.',
                          prefixIcon: Icon(Icons.person_outline_rounded),
                        ),
                        items: [
                          DropdownMenuItem(
                              value: '', child: Text('Unassigned')),
                          for (final user in users)
                            DropdownMenuItem(
                                value: user.ref.wire, child: Text(user.name)),
                        ],
                        onChanged: !assigneeEditable
                            ? null
                            : (value) => setState(() {
                                  _assignee = value?.isNotEmpty == true
                                      ? EntityRef.parse(value!)
                                      : null;
                                }),
                      ),
                      SizedBox(height: 14),
                      ListTile(
                        contentPadding: EdgeInsets.zero,
                        leading: Icon(Icons.event_outlined),
                        title: Text(_dueAt == null
                            ? 'No due date'
                            : DateFormat.yMMMd().format(_dueAt!.toLocal())),
                        subtitle: Text('Due at 5:00 PM local time'),
                        trailing: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            if (_dueAt != null)
                              IconButton(
                                tooltip: 'Clear due date',
                                onPressed: !widget.canEditDetails
                                    ? null
                                    : () => setState(() => _dueAt = null),
                                icon: Icon(Icons.clear_rounded),
                              ),
                            IconButton(
                              tooltip: _dueAt == null
                                  ? 'Set due date'
                                  : 'Change due date',
                              onPressed:
                                  widget.canEditDetails ? _pickDate : null,
                              icon: Icon(Icons.edit_calendar_outlined),
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
              ),
              Padding(
                padding: EdgeInsets.fromLTRB(20, 10, 20, 18),
                child: SizedBox(
                  width: double.infinity,
                  child: FilledButton.icon(
                    key: ValueKey('tracker-task-save'),
                    onPressed: _save,
                    icon: Icon(widget.task == null
                        ? Icons.add_rounded
                        : !widget.canEditDetails
                            ? Icons.person_add_alt_1_rounded
                            : Icons.save_outlined),
                    label: Text(widget.task == null
                        ? 'Create task'
                        : !widget.canEditDetails
                            ? 'Save assignment'
                            : 'Save task'),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

final class TrackerLaneDraft {
  TrackerLaneDraft({
    required this.name,
    required this.color,
    required this.kind,
    required this.completed,
  });

  final String name;
  final int color;
  final TrackerLaneKind kind;
  final bool completed;
}

final class TrackerLaneEditorSheet extends StatefulWidget {
  const TrackerLaneEditorSheet({super.key, this.lane});

  final TrackerLane? lane;

  @override
  State<TrackerLaneEditorSheet> createState() => _TrackerLaneEditorSheetState();
}

final class _TrackerLaneEditorSheetState extends State<TrackerLaneEditorSheet> {
  static const _colors = <int>[
    0xA8E063,
    0xF5B700,
    0x4D8EFF,
    0xB49BE4,
    0x68B69B,
    0xEE765E,
    0xFF8175,
    0xAAA096,
  ];
  final _formKey = GlobalKey<FormState>();
  late final TextEditingController _name;
  late int _color;
  late TrackerLaneKind _kind;
  late bool _completed;

  @override
  void initState() {
    super.initState();
    _name = TextEditingController(text: widget.lane?.name ?? '');
    _color = widget.lane?.color ?? _colors.first;
    _kind = widget.lane?.kind ?? TrackerLaneKind.custom;
    _completed = widget.lane?.completed ?? false;
  }

  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }

  void _save() {
    if (_formKey.currentState?.validate() != true) return;
    Navigator.pop(
      context,
      TrackerLaneDraft(
        name: _name.text.trim(),
        color: _color,
        kind: _kind,
        completed: _completed,
      ),
    );
  }

  @override
  Widget build(BuildContext context) => Padding(
        padding:
            EdgeInsets.only(bottom: MediaQuery.viewInsetsOf(context).bottom),
        child: Form(
          key: _formKey,
          child: SingleChildScrollView(
            padding: EdgeInsets.fromLTRB(20, 0, 20, 18),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  widget.lane == null ? 'Create lane' : 'Edit lane',
                  style: Theme.of(context).textTheme.headlineSmall,
                ),
                SizedBox(height: 18),
                TextFormField(
                  key: ValueKey('tracker-lane-name'),
                  controller: _name,
                  autofocus: widget.lane == null,
                  maxLength: 100,
                  decoration: InputDecoration(
                    labelText: 'Lane name',
                    prefixIcon: Icon(Icons.view_week_outlined),
                  ),
                  validator: (value) => value?.trim().isEmpty == true
                      ? 'Enter a lane name'
                      : null,
                ),
                SizedBox(height: 10),
                DropdownButtonFormField<TrackerLaneKind>(
                  initialValue: _kind,
                  decoration: InputDecoration(labelText: 'Lane type'),
                  items: [
                    for (final kind in TrackerLaneKind.values)
                      DropdownMenuItem(
                          value: kind, child: Text(_laneKindLabel(kind))),
                  ],
                  onChanged: (value) => setState(() => _kind = value ?? _kind),
                ),
                SizedBox(height: 14),
                Text('Color', style: Theme.of(context).textTheme.titleSmall),
                SizedBox(height: 8),
                Wrap(
                  spacing: 10,
                  runSpacing: 10,
                  children: [
                    for (final color in _colors)
                      Semantics(
                        button: true,
                        selected: _color == color,
                        label:
                            'Lane color ${color.toRadixString(16).padLeft(6, '0')}',
                        child: InkWell(
                          onTap: () => setState(() => _color = color),
                          borderRadius: BorderRadius.circular(30),
                          child: Container(
                            width: 42,
                            height: 42,
                            decoration: BoxDecoration(
                              shape: BoxShape.circle,
                              color: Color(0xFF000000 | color),
                              border: Border.all(
                                color: _color == color
                                    ? readableForeground(
                                        Color(0xFF000000 | color),
                                      )
                                    : Colors.transparent,
                                width: 3,
                              ),
                            ),
                            child: _color == color
                                ? Icon(
                                    Icons.check_rounded,
                                    color: readableForeground(
                                      Color(0xFF000000 | color),
                                    ),
                                  )
                                : null,
                          ),
                        ),
                      ),
                  ],
                ),
                SizedBox(height: 12),
                SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  value: _completed,
                  onChanged: (value) => setState(() => _completed = value),
                  title: Text('Completed lane'),
                  subtitle: Text(
                    'Tasks moved here are marked complete automatically.',
                  ),
                ),
                SizedBox(height: 10),
                FilledButton.icon(
                  key: ValueKey('tracker-lane-save'),
                  onPressed: _save,
                  icon: Icon(widget.lane == null
                      ? Icons.add_rounded
                      : Icons.save_outlined),
                  label:
                      Text(widget.lane == null ? 'Create lane' : 'Save lane'),
                ),
              ],
            ),
          ),
        ),
      );
}

final class _TrackerEmpty extends StatelessWidget {
  const _TrackerEmpty({
    required this.icon,
    required this.title,
    required this.message,
    this.action,
    this.actionLabel,
  });

  final IconData icon;
  final String title;
  final String message;
  final VoidCallback? action;
  final String? actionLabel;

  @override
  Widget build(BuildContext context) => Padding(
        padding: EdgeInsets.fromLTRB(28, 100, 28, 32),
        child: Column(
          children: [
            Icon(icon, size: 48, color: context.kaede.muted),
            SizedBox(height: 14),
            Text(title, style: Theme.of(context).textTheme.headlineSmall),
            SizedBox(height: 6),
            Text(message,
                textAlign: TextAlign.center,
                style: TextStyle(color: context.kaede.muted)),
            if (action != null) ...[
              SizedBox(height: 18),
              FilledButton.icon(
                onPressed: action,
                icon: Icon(Icons.add_rounded),
                label: Text(actionLabel ?? 'Create'),
              ),
            ],
          ],
        ),
      );
}

final class _TrackerFailure extends StatelessWidget {
  const _TrackerFailure({required this.message, required this.retry});

  final String message;
  final Future<void> Function({bool background}) retry;

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: EdgeInsets.all(28),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.sync_problem_rounded,
                  size: 46, color: context.kaede.danger),
              SizedBox(height: 12),
              Text(message, textAlign: TextAlign.center),
              SizedBox(height: 16),
              FilledButton.icon(
                onPressed: () => retry(),
                icon: Icon(Icons.refresh_rounded),
                label: Text('Try again'),
              ),
            ],
          ),
        ),
      );
}

IconData _laneIcon(TrackerLane lane) => switch (lane.kind) {
      TrackerLaneKind.backlog => Icons.inventory_2_outlined,
      TrackerLaneKind.planned => Icons.rocket_launch_outlined,
      TrackerLaneKind.inProgress => Icons.schedule_rounded,
      TrackerLaneKind.completed => Icons.check_circle_outline_rounded,
      TrackerLaneKind.custom => lane.completed
          ? Icons.check_circle_outline_rounded
          : Icons.view_week_outlined,
    };

String _laneKindLabel(TrackerLaneKind kind) => switch (kind) {
      TrackerLaneKind.backlog => 'Backlog',
      TrackerLaneKind.planned => 'Planned',
      TrackerLaneKind.inProgress => 'In progress',
      TrackerLaneKind.completed => 'Completed',
      TrackerLaneKind.custom => 'Custom',
    };

Color _priorityColor(BuildContext context, TrackerPriority priority) =>
    switch (priority) {
      TrackerPriority.none => context.kaede.muted,
      TrackerPriority.low => context.kaede.mint,
      TrackerPriority.medium => context.kaede.warning,
      TrackerPriority.high => context.kaede.coralText,
      TrackerPriority.urgent => context.kaede.danger,
    };
