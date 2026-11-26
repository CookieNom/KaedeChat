import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/presence_control.dart';
import 'package:kaede_mobile/src/features/shared/remote_media.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:timezone/data/latest.dart' as tzdata;
import 'package:timezone/timezone.dart' as tz;

/// Shows a Discord-style duration picker for Idle, Do Not Disturb, or Invisible.
/// Returns `null` if dismissed, or a record `(minutes: int?)` when confirmed.
Future<({int? minutes})?> showPresenceDurationSheet(
  BuildContext context, {
  required PresenceStatus status,
}) {
  return showModalBottomSheet<({int? minutes})>(
    context: context,
    showDragHandle: true,
    builder: (sheetContext) => SafeArea(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 0, 20, 10),
            child: Row(
              children: [
                Icon(
                  presenceIcon(status),
                  size: 18,
                  color: presenceColor(sheetContext, status),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        presenceLabel(status),
                        style: const TextStyle(
                          fontWeight: FontWeight.w700,
                          fontSize: 16,
                        ),
                      ),
                      Text(
                        'How long should this status stay active?',
                        style: TextStyle(
                          color: sheetContext.kaede.muted,
                          fontSize: 12,
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
          const Divider(height: 1),
          for (final option in kPresenceDurationOptions)
            ListTile(
              contentPadding: const EdgeInsets.symmetric(horizontal: 20),
              title: Text(
                option.label,
                style: const TextStyle(
                  fontSize: 15,
                  fontWeight: FontWeight.w500,
                ),
              ),
              onTap: () => Navigator.pop(
                sheetContext,
                (minutes: option.minutes),
              ),
            ),
          const SizedBox(height: 8),
        ],
      ),
    ),
  );
}

Future<void> showPresenceSchedulesSheet(BuildContext context, WidgetRef ref) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    showDragHandle: true,
    builder: (sheetContext) => DraggableScrollableSheet(
      expand: false,
      initialChildSize: 0.82,
      minChildSize: 0.5,
      maxChildSize: 0.94,
      builder: (_, scrollController) => _PresenceSchedulesSheetBody(
        scrollController: scrollController,
      ),
    ),
  );
}

class _PresenceSchedulesSheetBody extends ConsumerStatefulWidget {
  const _PresenceSchedulesSheetBody({required this.scrollController});

  final ScrollController scrollController;

  @override
  ConsumerState<_PresenceSchedulesSheetBody> createState() =>
      _PresenceSchedulesSheetBodyState();
}

class _PresenceSchedulesSheetBodyState
    extends ConsumerState<_PresenceSchedulesSheetBody> {
  int? _selectedDay;
  late final Timer _clock;

  @override
  void initState() {
    super.initState();
    tzdata.initializeTimeZones();
    _clock = Timer.periodic(const Duration(minutes: 1), (_) => setState(() {}));
  }

  @override
  void dispose() {
    _clock.cancel();
    super.dispose();
  }

  bool _busy = false;
  String? _error;

  Future<void> _save(Future<void> Function(MobileController) update) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await update(ref.read(mobileControllerProvider.notifier));
    } on Object catch (caught) {
      if (mounted) {
        setState(() => _error = caught.toString());
      }
    } finally {
      if (mounted) {
        setState(() => _busy = false);
      }
    }
  }

  Future<void> _openRuleEditor(
    List<PresenceScheduleRule> schedules, {
    PresenceScheduleRule? existing,
    PresenceScheduleRule? preset,
  }) async {
    final result = await showModalBottomSheet<PresenceScheduleRule>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (editorContext) => _ScheduleEditorSheet(
        initial: existing ?? preset,
        isEditing: existing != null,
      ),
    );
    if (result == null || !mounted) return;
    final nextSchedules = existing != null
        ? schedules
            .map((rule) => rule.id == existing.id ? result : rule)
            .toList()
        : <PresenceScheduleRule>[...schedules, result];
    await _save((controller) =>
        controller.updatePresenceSchedules(schedules, nextSchedules));
  }

  Future<void> _moveRule(
      List<PresenceScheduleRule> schedules, int index, int delta) async {
    final target = index + delta;
    if (target < 0 || target >= schedules.length) return;
    final next = List<PresenceScheduleRule>.of(schedules);
    final moved = next.removeAt(index);
    next.insert(target, moved);
    await _save(
        (controller) => controller.updatePresenceSchedules(schedules, next));
  }

  @override
  Widget build(BuildContext context) {
    final mobile = ref.watch(mobileControllerProvider);
    final presenceState = mobile.presenceState;
    final schedules =
        presenceState?.schedules ?? const <PresenceScheduleRule>[];
    final savedTimezone = presenceState?.timezone ?? 'UTC';
    final timezoneChoices =
        <String>{...kCommonTimezones, savedTimezone}.toList();
    final now = tz.TZDateTime.now(
        tz.timeZoneDatabase.locations[savedTimezone] ?? tz.UTC);
    final todayIndex = now.weekday % 7;
    final previewDay = _selectedDay ?? todayIndex;
    final nowFraction = (now.hour * 60 + now.minute) / 1440.0;
    final baseStatus = presenceState?.baseStatus ?? mobile.presencePreference;

    final slices = <PresenceStatus>[];
    var hasOverlap = false;
    for (var slot = 0; slot < 96; slot++) {
      final minute = slot * 15;
      final matches = schedules
          .where((rule) => rule.matchesAt(previewDay, minute))
          .toList();
      if (matches.length > 1) hasOverlap = true;
      slices.add(
        baseStatus == PresenceStatus.invisible
            ? PresenceStatus.invisible
            : (matches.isNotEmpty ? matches.first.status : baseStatus),
      );
    }

    final stamp = DateTime.now().millisecondsSinceEpoch;
    final presets = <PresenceScheduleRule>[
      PresenceScheduleRule(
        id: 'sched_${stamp}_sleep',
        name: 'Sleep',
        status: PresenceStatus.dnd,
        days: const <int>[0, 1, 2, 3, 4, 5, 6],
        startMinute: 23 * 60,
        endMinute: 7 * 60,
      ),
      PresenceScheduleRule(
        id: 'sched_${stamp}_work',
        name: 'Work Focus',
        status: PresenceStatus.idle,
        days: const <int>[1, 2, 3, 4, 5],
        startMinute: 9 * 60,
        endMinute: 17 * 60,
      ),
      PresenceScheduleRule(
        id: 'sched_${stamp}_wknd',
        name: 'Weekend Quiet',
        status: PresenceStatus.dnd,
        days: const <int>[0, 6],
        startMinute: 22 * 60,
        endMinute: 9 * 60,
      ),
    ];

    return SafeArea(
      child: ListView(
        controller: widget.scrollController,
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 28),
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text(
                      'Presence schedules',
                      style: TextStyle(
                        fontSize: 18,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    const SizedBox(height: 3),
                    Text(
                      'Automatically switch status while you sleep or focus.',
                      style: TextStyle(
                        fontSize: 13,
                        color: context.kaede.muted,
                      ),
                    ),
                  ],
                ),
              ),
              IconButton(
                icon: const Icon(Icons.close_rounded),
                tooltip: 'Close',
                onPressed: () => Navigator.pop(context),
              ),
            ],
          ),
          const SizedBox(height: 12),
          if (presenceState != null &&
              (presenceState.source == 'schedule' ||
                  presenceState.source == 'override' ||
                  presenceState.source == 'timed' ||
                  (presenceState.effectiveStatus == PresenceStatus.invisible &&
                      presenceState.suppressNotifications))) ...[
            Container(
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: context.kaede.panel,
                borderRadius: BorderRadius.circular(12),
                border: Border.all(color: context.kaede.border),
              ),
              child: Row(
                children: [
                  Icon(
                    presenceIcon(presenceState.effectiveStatus),
                    size: 18,
                    color:
                        presenceColor(context, presenceState.effectiveStatus),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      summarizePresenceSubtext(presenceState).isNotEmpty
                          ? '${presenceLabel(presenceState.effectiveStatus)} · ${summarizePresenceSubtext(presenceState)}'
                          : presenceLabel(presenceState.effectiveStatus),
                      style: const TextStyle(
                        fontSize: 13,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                  if (presenceState.source == 'schedule')
                    TextButton(
                      onPressed: _busy
                          ? null
                          : () => _save((controller) =>
                              controller.skipActivePresenceSchedule()),
                      child: const Text('Skip now'),
                    )
                  else if (presenceState.source == 'override' ||
                      presenceState.source == 'timed')
                    TextButton(
                      onPressed: _busy
                          ? null
                          : () => _save((controller) =>
                              controller.resetPresenceOverride()),
                      child: const Text('Resume'),
                    ),
                ],
              ),
            ),
            const SizedBox(height: 12),
          ],
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: context.kaede.panel,
              borderRadius: BorderRadius.circular(12),
              border: Border.all(color: context.kaede.border),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const Text(
                  '24-hour preview',
                  style: TextStyle(
                    fontSize: 13,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(height: 8),
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    for (var day = 0; day < 7; day++)
                      InkWell(
                        borderRadius: BorderRadius.circular(16),
                        onTap: () => setState(() => _selectedDay = day),
                        child: Container(
                          width: 32,
                          height: 32,
                          alignment: Alignment.center,
                          decoration: BoxDecoration(
                            shape: BoxShape.circle,
                            color: previewDay == day
                                ? Theme.of(context).colorScheme.primary
                                : Colors.transparent,
                            border: Border.all(
                              color: previewDay == day
                                  ? Theme.of(context).colorScheme.primary
                                  : context.kaede.border,
                            ),
                          ),
                          child: Text(
                            kPresenceDayShort[day],
                            style: TextStyle(
                              fontSize: 12,
                              fontWeight: FontWeight.w700,
                              color: previewDay == day
                                  ? Theme.of(context).colorScheme.onPrimary
                                  : context.kaede.muted,
                            ),
                          ),
                        ),
                      ),
                  ],
                ),
                const SizedBox(height: 10),
                LayoutBuilder(
                  builder: (barContext, constraints) {
                    return Stack(
                      clipBehavior: Clip.none,
                      children: [
                        ClipRRect(
                          borderRadius: BorderRadius.circular(7),
                          child: SizedBox(
                            height: 18,
                            child: Row(
                              children: [
                                for (final status in slices)
                                  Expanded(
                                    child: ColoredBox(
                                      color: presenceColor(barContext, status)
                                          .withValues(
                                        alpha: status == PresenceStatus.online
                                            ? 0.35
                                            : 0.88,
                                      ),
                                    ),
                                  ),
                              ],
                            ),
                          ),
                        ),
                        if (previewDay == todayIndex)
                          Positioned(
                            left: (constraints.maxWidth * nowFraction)
                                .clamp(0, constraints.maxWidth - 2.5),
                            top: -2,
                            bottom: -2,
                            child: Container(
                              width: 2.5,
                              color: Theme.of(barContext).colorScheme.onSurface,
                            ),
                          ),
                      ],
                    );
                  },
                ),
                const SizedBox(height: 6),
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    for (final tick in const <String>[
                      '12a',
                      '6a',
                      '12p',
                      '6p',
                      '12a',
                    ])
                      Text(
                        tick,
                        style: TextStyle(
                          fontSize: 11.5,
                          color: context.kaede.muted,
                        ),
                      ),
                  ],
                ),
                const SizedBox(height: 8),
                Wrap(
                  spacing: 12,
                  runSpacing: 4,
                  children: [
                    for (final legendStatus in const <PresenceStatus>[
                      PresenceStatus.online,
                      PresenceStatus.idle,
                      PresenceStatus.dnd,
                      PresenceStatus.invisible,
                    ])
                      Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Container(
                            width: 9,
                            height: 9,
                            decoration: BoxDecoration(
                              borderRadius: BorderRadius.circular(2.5),
                              color: presenceColor(context, legendStatus)
                                  .withValues(
                                alpha: legendStatus == PresenceStatus.online
                                    ? 0.45
                                    : 0.88,
                              ),
                            ),
                          ),
                          const SizedBox(width: 5),
                          Text(
                            legendStatus == PresenceStatus.dnd
                                ? 'DND'
                                : presenceLabel(legendStatus),
                            style: TextStyle(
                              fontSize: 11.5,
                              color: context.kaede.muted,
                            ),
                          ),
                        ],
                      ),
                  ],
                ),
                if (hasOverlap) ...[
                  const SizedBox(height: 6),
                  Text(
                    'Overlapping times use the top schedule first.',
                    style: TextStyle(
                      fontSize: 11.5,
                      color: context.kaede.muted,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ],
              ],
            ),
          ),
          const SizedBox(height: 12),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              for (final preset in presets)
                ActionChip(
                  label: Text(preset.name),
                  avatar: Icon(
                    presenceIcon(preset.status),
                    size: 14,
                    color: presenceColor(context, preset.status),
                  ),
                  onPressed: _busy || schedules.length >= kMaxPresenceSchedules
                      ? null
                      : () => _openRuleEditor(schedules, preset: preset),
                ),
              FilledButton.icon(
                onPressed: _busy || schedules.length >= kMaxPresenceSchedules
                    ? null
                    : () => _openRuleEditor(schedules),
                icon: const Icon(Icons.add_rounded, size: 16),
                label: const Text('Custom'),
              ),
            ],
          ),
          const SizedBox(height: 12),
          if (schedules.isEmpty)
            Container(
              padding: const EdgeInsets.all(16),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(12),
                border: Border.all(color: context.kaede.border),
              ),
              child: Text(
                'No schedules yet. Tap Sleep or Work Focus above, or create a custom schedule.',
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 13, color: context.kaede.muted),
              ),
            )
          else
            for (var index = 0; index < schedules.length; index++)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: _ScheduleCard(
                  rule: schedules[index],
                  canMoveUp: index > 0,
                  canMoveDown: index < schedules.length - 1,
                  showReorder: schedules.length > 1,
                  busy: _busy,
                  onToggle: (enabled) {
                    final next = schedules
                        .map(
                          (item) => item.id == schedules[index].id
                              ? item.copyWith(enabled: enabled)
                              : item,
                        )
                        .toList();
                    _save((controller) =>
                        controller.updatePresenceSchedules(schedules, next));
                  },
                  onEdit: () =>
                      _openRuleEditor(schedules, existing: schedules[index]),
                  onDelete: () {
                    final next = schedules
                        .where((item) => item.id != schedules[index].id)
                        .toList();
                    _save((controller) =>
                        controller.updatePresenceSchedules(schedules, next));
                  },
                  onMoveUp: () => _moveRule(schedules, index, -1),
                  onMoveDown: () => _moveRule(schedules, index, 1),
                ),
              ),
          const SizedBox(height: 8),
          Row(
            children: [
              Text(
                'Timezone',
                style: TextStyle(
                  fontSize: 13,
                  color: context.kaede.muted,
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: DropdownButtonFormField<String>(
                  key: ValueKey('presence-tz-$savedTimezone'),
                  initialValue: savedTimezone,
                  isExpanded: true,
                  decoration: const InputDecoration(
                    isDense: true,
                    contentPadding: EdgeInsets.symmetric(
                      horizontal: 10,
                      vertical: 8,
                    ),
                  ),
                  items: [
                    for (final zone in timezoneChoices)
                      DropdownMenuItem<String>(
                        value: zone,
                        child: Text(zone, overflow: TextOverflow.ellipsis),
                      ),
                  ],
                  onChanged: _busy
                      ? null
                      : (zone) {
                          if (zone != null && zone != savedTimezone) {
                            _save((controller) =>
                                controller.updatePresenceTimezone(zone));
                          }
                        },
                ),
              ),
            ],
          ),
          if (_error != null) ...[
            const SizedBox(height: 10),
            Text(
              _error!,
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
          ],
        ],
      ),
    );
  }
}

class _ScheduleCard extends StatelessWidget {
  const _ScheduleCard({
    required this.rule,
    required this.canMoveUp,
    required this.canMoveDown,
    required this.showReorder,
    required this.busy,
    required this.onToggle,
    required this.onEdit,
    required this.onDelete,
    required this.onMoveUp,
    required this.onMoveDown,
  });

  final PresenceScheduleRule rule;
  final bool canMoveUp;
  final bool canMoveDown;
  final bool showReorder;
  final bool busy;
  final ValueChanged<bool> onToggle;
  final VoidCallback onEdit;
  final VoidCallback onDelete;
  final VoidCallback onMoveUp;
  final VoidCallback onMoveDown;

  @override
  Widget build(BuildContext context) {
    final summary =
        '${formatPresenceDaysSummary(rule.days)} · ${formatPresenceMinute(rule.startMinute)} – ${formatPresenceMinute(rule.endMinute)}${rule.isOvernight ? ' (next day)' : ''}';
    return Opacity(
      opacity: rule.enabled ? 1.0 : 0.65,
      child: Container(
        padding: const EdgeInsets.fromLTRB(12, 10, 8, 10),
        decoration: BoxDecoration(
          color: context.kaede.panel,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: context.kaede.border),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Icon(
                  presenceIcon(rule.status),
                  size: 16,
                  color: presenceColor(context, rule.status),
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        '${rule.name} · ${presenceLabel(rule.status)}',
                        style: const TextStyle(
                          fontWeight: FontWeight.w700,
                          fontSize: 13,
                        ),
                      ),
                      const SizedBox(height: 2),
                      Text(
                        summary,
                        style: TextStyle(
                          fontSize: 12,
                          color: context.kaede.muted,
                        ),
                      ),
                    ],
                  ),
                ),
                Switch(
                  value: rule.enabled,
                  onChanged: busy ? null : onToggle,
                ),
              ],
            ),
            const Divider(height: 14),
            Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                if (showReorder) ...[
                  IconButton(
                    visualDensity: VisualDensity.compact,
                    icon: const Icon(Icons.arrow_upward_rounded, size: 18),
                    tooltip: 'Higher priority',
                    onPressed: busy || !canMoveUp ? null : onMoveUp,
                  ),
                  IconButton(
                    visualDensity: VisualDensity.compact,
                    icon: const Icon(Icons.arrow_downward_rounded, size: 18),
                    tooltip: 'Lower priority',
                    onPressed: busy || !canMoveDown ? null : onMoveDown,
                  ),
                ],
                TextButton(
                  style: TextButton.styleFrom(
                    visualDensity: VisualDensity.compact,
                  ),
                  onPressed: busy ? null : onEdit,
                  child: const Text('Edit'),
                ),
                IconButton(
                  visualDensity: VisualDensity.compact,
                  icon: const Icon(Icons.delete_outline_rounded, size: 18),
                  tooltip: 'Delete schedule',
                  onPressed: busy ? null : onDelete,
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _ScheduleEditorSheet extends StatefulWidget {
  const _ScheduleEditorSheet({
    required this.initial,
    required this.isEditing,
  });

  final PresenceScheduleRule? initial;
  final bool isEditing;

  @override
  State<_ScheduleEditorSheet> createState() => _ScheduleEditorSheetState();
}

class _ScheduleEditorSheetState extends State<_ScheduleEditorSheet> {
  late final TextEditingController _nameController;
  late PresenceStatus _status;
  late Set<int> _days;
  late int _startMinute;
  late int _endMinute;

  @override
  void initState() {
    super.initState();
    final initial = widget.initial;
    _nameController = TextEditingController(
      text: initial?.name ?? 'Quiet Hours',
    );
    _status = initial?.status ?? PresenceStatus.dnd;
    _days = Set<int>.of(initial?.days ?? const <int>[0, 1, 2, 3, 4, 5, 6]);
    _startMinute = initial?.startMinute ?? 22 * 60;
    _endMinute = initial?.endMinute ?? 7 * 60;
  }

  @override
  void dispose() {
    _nameController.dispose();
    super.dispose();
  }

  Future<void> _pickTime({required bool isStart}) async {
    final current = isStart ? _startMinute : _endMinute;
    final picked = await showTimePicker(
      context: context,
      initialTime: TimeOfDay(hour: current ~/ 60, minute: current % 60),
    );
    if (picked == null) return;
    setState(() {
      final total = picked.hour * 60 + picked.minute;
      if (isStart) {
        _startMinute = total;
      } else {
        _endMinute = total;
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final trimmedName = _nameController.text.trim();
    final sameTime = _startMinute == _endMinute;
    final overnight = _endMinute < _startMinute;
    final valid = trimmedName.isNotEmpty && _days.isNotEmpty && !sameTime;

    return SafeArea(
      child: Padding(
        padding: EdgeInsets.fromLTRB(
          16,
          0,
          16,
          MediaQuery.of(context).viewInsets.bottom + 20,
        ),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                widget.isEditing ? 'Edit schedule' : 'New schedule',
                style: const TextStyle(
                  fontSize: 17,
                  fontWeight: FontWeight.w700,
                ),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _nameController,
                maxLength: 32,
                decoration: const InputDecoration(
                  labelText: 'Schedule name',
                  hintText: 'e.g. Sleep, Work Focus',
                ),
                onChanged: (_) => setState(() {}),
              ),
              const SizedBox(height: 8),
              const Text(
                'Status',
                style: TextStyle(fontSize: 12, fontWeight: FontWeight.w600),
              ),
              const SizedBox(height: 6),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  for (final status in const <PresenceStatus>[
                    PresenceStatus.online,
                    PresenceStatus.idle,
                    PresenceStatus.dnd,
                    PresenceStatus.invisible,
                  ])
                    ChoiceChip(
                      selected: _status == status,
                      avatar: Icon(
                        presenceIcon(status),
                        size: 14,
                        color: presenceColor(context, status),
                      ),
                      label: Text(presenceLabel(status)),
                      onSelected: (_) => setState(() => _status = status),
                    ),
                ],
              ),
              const SizedBox(height: 12),
              Row(
                children: [
                  Expanded(
                    child: OutlinedButton(
                      style: OutlinedButton.styleFrom(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 12,
                          vertical: 10,
                        ),
                      ),
                      onPressed: () => _pickTime(isStart: true),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            'Starts',
                            style: TextStyle(
                              fontSize: 11,
                              color: context.kaede.muted,
                            ),
                          ),
                          const SizedBox(height: 2),
                          Text(
                            formatPresenceMinute(_startMinute),
                            style: const TextStyle(
                              fontSize: 15,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: OutlinedButton(
                      style: OutlinedButton.styleFrom(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 12,
                          vertical: 10,
                        ),
                      ),
                      onPressed: () => _pickTime(isStart: false),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            'Ends',
                            style: TextStyle(
                              fontSize: 11,
                              color: context.kaede.muted,
                            ),
                          ),
                          const SizedBox(height: 2),
                          Text(
                            formatPresenceMinute(_endMinute),
                            style: const TextStyle(
                              fontSize: 15,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                ],
              ),
              if (sameTime) ...[
                const SizedBox(height: 8),
                Text(
                  'Start and end time must be different.',
                  style: TextStyle(
                    fontSize: 12.5,
                    color: Theme.of(context).colorScheme.error,
                  ),
                ),
              ] else if (overnight) ...[
                const SizedBox(height: 8),
                Text(
                  'Overnight schedule: ends at ${formatPresenceMinute(_endMinute)} the next morning.',
                  style: TextStyle(
                    fontSize: 12.5,
                    color: context.kaede.muted,
                  ),
                ),
              ],
              const SizedBox(height: 14),
              const Text(
                'Repeat on',
                style: TextStyle(fontSize: 13, fontWeight: FontWeight.w600),
              ),
              const SizedBox(height: 6),
              Wrap(
                spacing: 6,
                runSpacing: 6,
                children: [
                  OutlinedButton(
                    style: OutlinedButton.styleFrom(
                      visualDensity: VisualDensity.compact,
                      padding: const EdgeInsets.symmetric(horizontal: 10),
                    ),
                    onPressed: () =>
                        setState(() => _days = <int>{0, 1, 2, 3, 4, 5, 6}),
                    child: const Text('Every day'),
                  ),
                  OutlinedButton(
                    style: OutlinedButton.styleFrom(
                      visualDensity: VisualDensity.compact,
                      padding: const EdgeInsets.symmetric(horizontal: 10),
                    ),
                    onPressed: () =>
                        setState(() => _days = <int>{1, 2, 3, 4, 5}),
                    child: const Text('Weekdays'),
                  ),
                  OutlinedButton(
                    style: OutlinedButton.styleFrom(
                      visualDensity: VisualDensity.compact,
                      padding: const EdgeInsets.symmetric(horizontal: 10),
                    ),
                    onPressed: () => setState(() => _days = <int>{0, 6}),
                    child: const Text('Weekends'),
                  ),
                ],
              ),
              const SizedBox(height: 10),
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  for (var day = 0; day < 7; day++)
                    InkWell(
                      borderRadius: BorderRadius.circular(20),
                      onTap: () {
                        setState(() {
                          if (_days.contains(day)) {
                            _days.remove(day);
                          } else {
                            _days.add(day);
                          }
                        });
                      },
                      child: Container(
                        width: 38,
                        height: 38,
                        alignment: Alignment.center,
                        decoration: BoxDecoration(
                          shape: BoxShape.circle,
                          color: _days.contains(day)
                              ? Theme.of(context).colorScheme.primary
                              : Colors.transparent,
                          border: Border.all(
                            color: _days.contains(day)
                                ? Theme.of(context).colorScheme.primary
                                : context.kaede.border,
                          ),
                        ),
                        child: Text(
                          kPresenceDayShort[day],
                          style: TextStyle(
                            fontSize: 13,
                            fontWeight: FontWeight.w700,
                            color: _days.contains(day)
                                ? Theme.of(context).colorScheme.onPrimary
                                : context.kaede.muted,
                          ),
                        ),
                      ),
                    ),
                ],
              ),
              const SizedBox(height: 16),
              FilledButton(
                onPressed: !valid
                    ? null
                    : () {
                        final sortedDays = _days.toList()..sort();
                        final id = widget.initial?.id ??
                            'sched_${DateTime.now().millisecondsSinceEpoch}';
                        Navigator.pop(
                          context,
                          PresenceScheduleRule(
                            id: id,
                            name: trimmedName,
                            status: _status,
                            days: sortedDays,
                            startMinute: _startMinute,
                            endMinute: _endMinute,
                            enabled: widget.initial?.enabled ?? true,
                          ),
                        );
                      },
                child: Text(widget.isEditing ? 'Save changes' : 'Add schedule'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
