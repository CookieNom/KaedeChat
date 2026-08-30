import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/domain/voice_messages.dart';

void main() {
  test('decodes bounded Discord voice-message waveform samples', () {
    expect(decodeVoiceWaveform('AP+A'), <double>[.12, 1, 128 / 255]);
    expect(decodeVoiceWaveform('not base64'), isEmpty);
    expect(decodeVoiceWaveform('A' * 345), isEmpty);
  });

  test('formats the authoritative duration', () {
    expect(voiceDurationLabel(65.4), '1:05');
    expect(voiceDurationLabel(null), 'Audio');
    expect(voiceDurationLabel(5000), '20:00');
  });

  test('normalizes and downsamples recorder amplitudes to at most 256 bytes',
      () {
    final encoded = encodeVoiceWaveform(
      List<double>.generate(1000, (index) => -60 + (index % 61)),
    );
    expect(decodeVoiceWaveform(encoded), hasLength(256));
  });
}
