import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:kaede_mobile/src/domain/voice_messages.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:path_provider/path_provider.dart';
import 'package:record/record.dart';

const voiceMessageMaximumDuration = Duration(minutes: 20);
const _voiceMessageMinimumDuration = Duration(milliseconds: 300);

final class VoiceRecording {
  VoiceRecording({
    required this.file,
    required this.durationSecs,
    required this.waveform,
  });

  final File file;
  final double durationSecs;
  final String waveform;

  String get filename =>
      'voice-message-${DateTime.now().millisecondsSinceEpoch}.m4a';
  String get contentType => 'audio/mp4';

  Future<void> delete() async {
    try {
      if (await file.exists()) await file.delete();
    } on FileSystemException {
      // The OS may already have reclaimed a temporary recording.
    }
  }
}

/// Discord-style mobile voice-message affordance: hold to record, drag left
/// to cancel, swipe up to lock, then explicitly send or discard.
final class VoiceMessageRecorder extends StatefulWidget {
  const VoiceMessageRecorder({
    super.key,
    required this.enabled,
    required this.busy,
    required this.onRecorded,
    required this.onError,
  });

  final bool enabled;
  final bool busy;
  final Future<void> Function(VoiceRecording recording) onRecorded;
  final ValueChanged<String> onError;

  @override
  State<VoiceMessageRecorder> createState() => _VoiceMessageRecorderState();
}

final class _VoiceMessageRecorderState extends State<VoiceMessageRecorder> {
  final AudioRecorder _recorder = AudioRecorder();
  final Stopwatch _elapsed = Stopwatch();
  final List<double> _amplitudes = <double>[];
  StreamSubscription<Amplitude>? _amplitudeSubscription;
  Timer? _ticker;
  Timer? _maximumTimer;
  Offset? _gestureOrigin;
  int? _pointer;
  bool _starting = false;
  bool _recording = false;
  bool _locked = false;
  bool _finishing = false;
  bool _finishAfterStart = false;
  bool _cancelAfterStart = false;

  bool get _available => widget.enabled && !widget.busy && !_finishing;

  @override
  void dispose() {
    _ticker?.cancel();
    _maximumTimer?.cancel();
    unawaited(_amplitudeSubscription?.cancel());
    if (_starting || _recording) unawaited(_recorder.cancel());
    unawaited(_recorder.dispose());
    super.dispose();
  }

  Future<void> _start(PointerDownEvent event) async {
    if (!_available || _starting || _recording) return;
    _pointer = event.pointer;
    _gestureOrigin = event.position;
    _finishAfterStart = false;
    _cancelAfterStart = false;
    setState(() => _starting = true);
    unawaited(HapticFeedback.selectionClick());
    try {
      if (!await _recorder.hasPermission()) {
        throw StateError(
          'Microphone access is required to record a voice message.',
        );
      }
      if (!await _recorder.isEncoderSupported(AudioEncoder.aacLc)) {
        throw StateError(
          'This device cannot record the supported voice-message format.',
        );
      }
      final directory = await getTemporaryDirectory();
      final path = '${directory.path}/kaede-voice-'
          '${DateTime.now().microsecondsSinceEpoch}.m4a';
      _amplitudes.clear();
      await _recorder.start(
        RecordConfig(
          encoder: AudioEncoder.aacLc,
          bitRate: 64000,
          sampleRate: 48000,
          numChannels: 1,
          autoGain: true,
          echoCancel: true,
          noiseSuppress: true,
        ),
        path: path,
      );
      await _amplitudeSubscription?.cancel();
      _amplitudeSubscription = _recorder
          .onAmplitudeChanged(Duration(milliseconds: 100))
          .listen((sample) => _amplitudes.add(sample.current));
      _elapsed
        ..reset()
        ..start();
      _ticker = Timer.periodic(Duration(seconds: 1), (_) {
        if (mounted) setState(() {});
      });
      _maximumTimer = Timer(voiceMessageMaximumDuration, () {
        if (_recording) unawaited(_finish(send: true));
      });
      if (!mounted) {
        await _recorder.cancel();
        return;
      }
      setState(() {
        _starting = false;
        _recording = true;
      });
      if (_cancelAfterStart) {
        await _finish(send: false);
      } else if (_finishAfterStart && !_locked) {
        await _finish(send: true);
      }
    } on Object catch (error) {
      await _resetRecorder(cancel: true);
      widget.onError(_recordingError(error));
    }
  }

  void _move(PointerMoveEvent event) {
    if (event.pointer != _pointer || (!_starting && !_recording) || _locked) {
      return;
    }
    final origin = _gestureOrigin;
    if (origin == null) return;
    final delta = event.position - origin;
    if (delta.dx <= -80) {
      _cancelAfterStart = true;
      unawaited(_finish(send: false));
    } else if (delta.dy <= -64 && _recording) {
      setState(() => _locked = true);
      unawaited(HapticFeedback.mediumImpact());
    }
  }

  void _release(PointerEvent event) {
    if (event.pointer != _pointer || _locked) return;
    if (_starting) {
      _finishAfterStart = true;
      return;
    }
    if (_recording) unawaited(_finish(send: true));
  }

  Future<void> _finish({required bool send}) async {
    if (_finishing) return;
    if (_starting && !_recording) {
      if (send) {
        _finishAfterStart = true;
      } else {
        _cancelAfterStart = true;
      }
      return;
    }
    if (!_recording) return;
    setState(() => _finishing = true);
    final duration = _elapsed.elapsed;
    _elapsed.stop();
    _ticker?.cancel();
    _maximumTimer?.cancel();
    await _amplitudeSubscription?.cancel();
    _amplitudeSubscription = null;
    try {
      if (!send || duration < _voiceMessageMinimumDuration) {
        await _recorder.cancel();
        if (send && duration < _voiceMessageMinimumDuration) {
          widget.onError('Hold the microphone a little longer to record.');
        }
      } else {
        final path = await _recorder.stop();
        if (path == null || path.isEmpty) {
          throw StateError('The recorder did not return an audio file.');
        }
        final recording = VoiceRecording(
          file: File(path),
          durationSecs: duration.inMilliseconds / 1000,
          waveform: encodeVoiceWaveform(_amplitudes),
        );
        await widget.onRecorded(recording);
      }
    } on Object catch (error) {
      widget.onError(_recordingError(error));
      try {
        await _recorder.cancel();
      } on Object {
        // Preserve the original recording or upload error.
      }
    } finally {
      _resetUi();
    }
  }

  Future<void> _resetRecorder({required bool cancel}) async {
    if (cancel) {
      try {
        await _recorder.cancel();
      } on Object {
        // The recorder may not have started yet.
      }
    }
    _resetUi();
  }

  void _resetUi() {
    _ticker?.cancel();
    _maximumTimer?.cancel();
    _elapsed
      ..stop()
      ..reset();
    _pointer = null;
    _gestureOrigin = null;
    _starting = false;
    _recording = false;
    _locked = false;
    _finishing = false;
    _finishAfterStart = false;
    _cancelAfterStart = false;
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    if (_locked || _finishing) {
      return SizedBox(
        height: 46,
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            IconButton(
              tooltip: 'Discard voice message',
              onPressed: _finishing ? null : () => _finish(send: false),
              icon: Icon(Icons.delete_outline_rounded),
            ),
            Icon(Icons.fiber_manual_record,
                size: 10, color: context.kaede.danger),
            SizedBox(width: 5),
            Text(
              _durationLabel(_elapsed.elapsed),
              style: TextStyle(
                color: context.kaede.textSoft,
                fontWeight: FontWeight.w700,
              ),
            ),
            IconButton.filled(
              tooltip: 'Send voice message',
              onPressed: _finishing ? null : () => _finish(send: true),
              icon: _finishing
                  ? SizedBox.square(
                      dimension: 15,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : Icon(Icons.arrow_upward_rounded),
            ),
          ],
        ),
      );
    }
    return Semantics(
      button: true,
      enabled: _available,
      label: _starting || _recording
          ? 'Recording voice message. Release to send, drag left to cancel, or swipe up to lock.'
          : 'Hold to record a voice message',
      child: Listener(
        behavior: HitTestBehavior.opaque,
        onPointerDown: _available ? _start : null,
        onPointerMove: _move,
        onPointerUp: _release,
        onPointerCancel: (_) => unawaited(_finish(send: false)),
        child: Tooltip(
          message: _recording
              ? 'Release to send · swipe up to lock · drag left to cancel'
              : 'Hold to record a voice message',
          child: SizedBox.square(
            dimension: 46,
            child: Icon(
              _starting
                  ? Icons.hourglass_top_rounded
                  : _recording
                      ? Icons.mic_rounded
                      : Icons.mic_none_rounded,
              color: _recording ? context.kaede.danger : context.kaede.textSoft,
              size: 23,
            ),
          ),
        ),
      ),
    );
  }
}

String _durationLabel(Duration duration) {
  final total = duration.inSeconds.clamp(0, 1200);
  return '${total ~/ 60}:${(total % 60).toString().padLeft(2, '0')}';
}

String _recordingError(Object error) {
  final text =
      '$error'.replaceFirst(RegExp(r'^(StateError|Exception):\s*'), '');
  return text.isEmpty ? 'Could not record this voice message.' : text;
}
