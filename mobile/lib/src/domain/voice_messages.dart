import 'dart:convert';
import 'dart:math';

const messageFlagIsVoiceMessage = 1 << 13;

List<double> decodeVoiceWaveform(String? value) {
  if (value == null ||
      value.isEmpty ||
      value.length > 344 ||
      !RegExp(r'^[A-Za-z0-9+/]+={0,2}$').hasMatch(value)) {
    return const <double>[];
  }
  try {
    final bytes = base64Decode(value);
    if (bytes.isEmpty || bytes.length > 256) return const <double>[];
    return List<double>.unmodifiable(
      bytes.map((sample) => (sample / 255).clamp(.12, 1).toDouble()),
    );
  } on FormatException {
    return const <double>[];
  }
}

String voiceDurationLabel(num? seconds) {
  if (seconds == null || !seconds.isFinite || seconds <= 0) return 'Audio';
  final total = seconds.round().clamp(0, 1200);
  return '${total ~/ 60}:${(total % 60).toString().padLeft(2, '0')}';
}

String encodeVoiceWaveform(Iterable<double> decibels) {
  final samples = decibels.toList(growable: false);
  if (samples.isEmpty) return base64Encode(const <int>[0]);
  const maximum = 256;
  final result = <int>[];
  for (var index = 0; index < min(samples.length, maximum); index += 1) {
    final start = index * samples.length ~/ min(samples.length, maximum);
    final end = (index + 1) * samples.length ~/ min(samples.length, maximum);
    final window = samples.sublist(start, max(start + 1, end));
    final normalized =
        window.map((value) => ((value + 60) / 60).clamp(0, 1)).reduce(max) *
            255;
    result.add(normalized.round());
  }
  return base64Encode(result);
}
