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
  final OverlayPortalController _overlay = OverlayPortalController();
  final LayerLink _anchor = LayerLink();
  VoiceRecording? _draft;
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
  bool _disposeAfterStart = false;

  bool get _available =>
      widget.enabled &&
      !widget.busy &&
      !_finishing &&
      !_starting &&
      !_recording &&
      _draft == null;

  @override
  void dispose() {
    _ticker?.cancel();
    _maximumTimer?.cancel();
    unawaited(_amplitudeSubscription?.cancel());
    if (_starting || _recording) unawaited(_recorder.cancel());
    unawaited(_draft?.delete());
    _disposeAfterStart = _starting;
    if (!_disposeAfterStart) unawaited(_recorder.dispose());
    super.dispose();
  }

  Future<void> _start([PointerDownEvent? event]) async {
    if (!_available) return;
    _pointer = event?.pointer;
    _gestureOrigin = event?.position;
    _locked = event == null;
    _finishAfterStart = false;
    _cancelAfterStart = false;
    setState(() => _starting = true);
    _overlay.show();
    unawaited(HapticFeedback.selectionClick());
    try {
      if (!await _recorder.hasPermission()) {
        throw StateError(
          'Microphone access was denied. Open your phone’s Settings, find Kaede, '
          'and allow microphone access to record voice messages.',
        );
      }
      if (!await _recorder.isEncoderSupported(AudioEncoder.aacLc)) {
        throw StateError(
          'This device cannot record the supported voice-message format.',
        );
      }
      if (!mounted || _cancelAfterStart || (_finishAfterStart && !_locked)) {
        await _resetRecorder(cancel: true);
        return;
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
      if (!mounted) {
        await _recorder.cancel();
        return;
      }
      await _amplitudeSubscription?.cancel();
      _amplitudeSubscription = _recorder
          .onAmplitudeChanged(Duration(milliseconds: 100))
          .listen((sample) {
        if (mounted && sample.current.isFinite) {
          setState(() => _amplitudes.add(sample.current));
        }
      }, onError: (Object error) {
        if (mounted) widget.onError(_recordingError(error));
        unawaited(_finish(send: false));
      });
      _elapsed
        ..reset()
        ..start();
      _ticker = Timer.periodic(Duration(seconds: 1), (_) {
        if (mounted) setState(() {});
      });
      _maximumTimer = Timer(voiceMessageMaximumDuration, () {
        if (_recording) unawaited(_finish(send: false, keep: true));
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
      if (mounted) widget.onError(_recordingError(error));
    } finally {
      if (_disposeAfterStart) await _recorder.dispose();
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
    } else if (delta.dy <= -64) {
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

  Future<void> _finish({required bool send, bool keep = false}) async {
    if (_finishing) return;
    if (_starting && !_recording) {
      if (send) {
        _finishAfterStart = true;
      } else {
        _cancelAfterStart = true;
      }
      return;
    }
    if (_draft case final draft?) {
      _draft = null;
      setState(() => _finishing = true);
      try {
        if (send) await widget.onRecorded(draft);
      } on Object catch (error) {
        if (mounted) widget.onError(_recordingError(error));
      } finally {
        await draft.delete();
        _resetUi();
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
      if ((!send && !keep) || duration < _voiceMessageMinimumDuration) {
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
        _recording = false;
        if (!mounted) {
          await recording.delete();
        } else if (keep) {
          _draft = recording;
          _locked = true;
        } else {
          try {
            await widget.onRecorded(recording);
          } finally {
            await recording.delete();
          }
        }
      }
    } on Object catch (error) {
      if (mounted) widget.onError(_recordingError(error));
      try {
        await _recorder.cancel();
      } on Object {
        // Preserve the original recording or upload error.
      }
    } finally {
      if (_draft != null && mounted) {
        setState(() => _finishing = false);
      } else {
        _resetUi();
      }
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
    unawaited(_amplitudeSubscription?.cancel());
    _amplitudeSubscription = null;
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
    if (mounted) {
      _overlay.hide();
      setState(() {});
    }
  }

  @override
  Widget build(BuildContext context) {
    // Keep this pointer listener mounted while the overlay changes: the held
    // finger must still deliver moves/releases after recording starts or locks.
    return CompositedTransformTarget(
      link: _anchor,
      child: OverlayPortal(
        controller: _overlay,
        overlayChildBuilder: (context) => Positioned(
          width: MediaQuery.sizeOf(context).width - 16,
          child: CompositedTransformFollower(
            link: _anchor,
            showWhenUnlinked: false,
            targetAnchor: Alignment.bottomRight,
            followerAnchor: Alignment.bottomRight,
            child: _recordingPanel(context),
          ),
        ),
        child: Semantics(
          button: true,
          enabled: _available,
          label: 'Hold to record a voice message',
          onTap: _available ? () => unawaited(_start()) : null,
          child: Listener(
            behavior: HitTestBehavior.opaque,
            onPointerDown: _available ? _start : null,
            onPointerMove: _move,
            onPointerUp: _release,
            onPointerCancel: (event) {
              if (event.pointer == _pointer && !_locked) {
                unawaited(_finish(send: false));
              }
            },
            child: Tooltip(
              triggerMode: TooltipTriggerMode.manual,
              message: 'Hold to record a voice message',
              child: SizedBox.square(
                dimension: 46,
                child: Icon(Icons.mic_none_rounded,
                    color: context.kaede.textSoft, size: 23),
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _recordingPanel(BuildContext context) {
    final ready = _draft != null;
    return Material(
      color: Colors.transparent,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          if (!_locked && !_finishing)
            Container(
              margin: EdgeInsets.only(bottom: 8, right: 2),
              padding: EdgeInsets.symmetric(horizontal: 12, vertical: 10),
              decoration: BoxDecoration(
                color: context.kaede.raised,
                borderRadius: BorderRadius.circular(24),
              ),
              child: Column(children: [
                Icon(Icons.lock_open_rounded, color: context.kaede.textSoft),
                Icon(Icons.keyboard_arrow_up_rounded,
                    color: context.kaede.textSoft),
              ]),
            ),
          Center(
            child: Container(
              padding: EdgeInsets.symmetric(horizontal: 12, vertical: 6),
              decoration: BoxDecoration(
                color: context.kaede.raised,
                borderRadius: BorderRadius.circular(16),
              ),
              child: Text(
                _starting
                    ? 'Starting microphone…'
                    : ready
                        ? 'Ready to send'
                        : _locked
                            ? 'Recording locked'
                            : 'Release to send · Swipe up to lock',
                style: TextStyle(color: context.kaede.textSoft, fontSize: 12),
              ),
            ),
          ),
          SizedBox(height: 6),
          Container(
            height: 54,
            decoration: BoxDecoration(
              color: context.kaede.raised,
              borderRadius: BorderRadius.circular(24),
              border: Border.all(color: context.kaede.border),
            ),
            child: Row(children: [
              IconButton(
                tooltip: 'Discard voice message',
                onPressed: _finishing ? null : () => _finish(send: false),
                icon: Icon(Icons.delete_outline_rounded),
              ),
              Icon(ready ? Icons.lock_rounded : Icons.fiber_manual_record,
                  size: 10, color: context.kaede.danger),
              SizedBox(width: 5),
              Text(_durationLabel(_elapsed.elapsed),
                  style: TextStyle(
                      color: context.kaede.textSoft,
                      fontWeight: FontWeight.w700)),
              SizedBox(width: 12),
              Expanded(
                child: CustomPaint(
                  size: Size(double.infinity, 30),
                  painter: _LiveWaveform(
                    _amplitudes.length > 48
                        ? _amplitudes.sublist(_amplitudes.length - 48)
                        : List<double>.of(_amplitudes),
                    context.kaede.textSoft,
                  ),
                ),
              ),
              if (_locked && !ready && !_starting)
                IconButton(
                  tooltip: 'Stop recording',
                  onPressed: _finishing
                      ? null
                      : () => _finish(send: false, keep: true),
                  icon: Icon(Icons.stop_rounded),
                ),
              IconButton.filled(
                tooltip: 'Send voice message',
                onPressed:
                    _finishing || _starting ? null : () => _finish(send: true),
                icon: _finishing || _starting
                    ? SizedBox.square(
                        dimension: 16,
                        child: CircularProgressIndicator(strokeWidth: 2))
                    : Icon(Icons.send_rounded),
              ),
              SizedBox(width: 4),
            ]),
          ),
        ],
      ),
    );
  }
}

String _durationLabel(Duration duration) {
  final total = duration.inSeconds.clamp(0, 1200);
  return '${total ~/ 60}:${(total % 60).toString().padLeft(2, '0')}';
}

String _recordingError(Object error) {
  if (error is StateError) return error.message;
  final text =
      '$error'.replaceFirst(RegExp(r'^(StateError|Exception):\s*'), '');
  return text.isEmpty ? 'Could not record this voice message.' : text;
}

final class _LiveWaveform extends CustomPainter {
  const _LiveWaveform(this.samples, this.color);
  final List<double> samples;
  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = color
      ..strokeWidth = 3
      ..strokeCap = StrokeCap.round;
    final count = (size.width / 5).floor();
    for (var i = 0; i < count; i++) {
      final index = samples.length - count + i;
      final level =
          index < 0 ? 0.0 : ((samples[index] + 60) / 60).clamp(0.0, 1.0);
      final height = 2 + level * (size.height - 2);
      final x = i * 5.0 + 2;
      canvas.drawLine(Offset(x, (size.height - height) / 2),
          Offset(x, (size.height + height) / 2), paint);
    }
  }

  @override
  bool shouldRepaint(_LiveWaveform oldDelegate) => true;
}
