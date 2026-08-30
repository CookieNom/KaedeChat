int? voiceStartTimeSeconds(Object? value) =>
    value is int && value > 0 ? value : null;

/// Render the compact elapsed time Discord uses beside an active voice room.
String? voiceElapsedLabel(
  int? startedAt, {
  DateTime? now,
}) {
  if (startedAt == null || startedAt <= 0) return null;
  final current =
      (now ?? DateTime.now()).toUtc().millisecondsSinceEpoch ~/ 1000;
  final elapsed = (current - startedAt).clamp(0, 1 << 62);
  final hours = elapsed ~/ 3600;
  final minutes = (elapsed % 3600) ~/ 60;
  final seconds = elapsed % 60;
  final minuteText = minutes.toString().padLeft(2, '0');
  final secondText = seconds.toString().padLeft(2, '0');
  return hours > 0 ? '$hours:$minuteText:$secondText' : '$minutes:$secondText';
}
