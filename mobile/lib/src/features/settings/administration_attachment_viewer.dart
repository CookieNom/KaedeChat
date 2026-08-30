import 'dart:async';
import 'dart:io';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/instance_administration_repository.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/domain/instance_administration.dart';
import 'package:path_provider/path_provider.dart';
import 'package:share_plus/share_plus.dart';
import 'package:video_player/video_player.dart';

/// Authenticated, local-file evidence viewer. The API client's redirect fence
/// prevents the bearer token from being forwarded to object storage.
final class AdministrationAttachmentViewer extends StatefulWidget {
  const AdministrationAttachmentViewer({
    super.key,
    required this.repository,
    required this.report,
    required this.attachment,
  });

  final KaedeRepository repository;
  final AdministrationReport report;
  final AdministrationReportAttachment attachment;

  @override
  State<AdministrationAttachmentViewer> createState() =>
      _AdministrationAttachmentViewerState();
}

final class _AdministrationAttachmentViewerState
    extends State<AdministrationAttachmentViewer> {
  File? _file;
  VideoPlayerController? _video;
  AudioPlayer? _audio;
  String? _error;
  var _loading = true;
  var _sharing = false;

  String? get _contentType =>
      widget.report.attachmentContentType(widget.attachment);
  String get _filename =>
      safeEvidenceFilename(widget.report.attachmentFilename(widget.attachment));

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  @override
  void dispose() {
    _video?.dispose();
    unawaited(_audio?.dispose());
    final file = _file;
    if (file != null) unawaited(file.delete().catchError((_) => file));
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final directory = await getTemporaryDirectory();
      final destination = File(
        '${directory.path}/kaede-report-${widget.report.id}-${DateTime.now().microsecondsSinceEpoch}-$_filename',
      );
      final file =
          await widget.repository.downloadAdministrationReportAttachment(
        report: widget.report,
        attachment: widget.attachment,
        destination: destination,
      );
      VideoPlayerController? video;
      AudioPlayer? audio;
      if (_contentType?.startsWith('video/') == true) {
        video = VideoPlayerController.file(file);
        await video.initialize();
        await video.setLooping(false);
      } else if (_contentType?.startsWith('audio/') == true) {
        audio = AudioPlayer();
        await audio
            .setSource(DeviceFileSource(file.path, mimeType: _contentType));
      }
      if (!mounted) {
        await video?.dispose();
        await audio?.dispose();
        await file.delete();
        return;
      }
      setState(() {
        _file = file;
        _video = video;
        _audio = audio;
        _loading = false;
      });
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        _error = userFacingError(
          error,
          summary: 'Could not load this report attachment',
        );
        _loading = false;
      });
    }
  }

  Future<void> _share() async {
    final file = _file;
    if (file == null || _sharing) return;
    setState(() => _sharing = true);
    try {
      final box = context.findRenderObject() as RenderBox?;
      await Share.shareXFiles(
        <XFile>[XFile(file.path, mimeType: _contentType, name: _filename)],
        subject: _filename,
        sharePositionOrigin:
            box == null ? null : box.localToGlobal(Offset.zero) & box.size,
      );
    } on Object catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(userFacingError(
            error,
            summary: 'Could not save or share this attachment',
          )),
        ),
      );
    } finally {
      if (mounted) setState(() => _sharing = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(
          title: Text(_filename),
          actions: [
            IconButton(
              tooltip: 'Save or share original',
              onPressed: _file == null || _sharing ? null : _share,
              icon: _sharing
                  ? const SizedBox.square(
                      dimension: 18,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.ios_share_rounded),
            ),
          ],
        ),
        body: SafeArea(
          child: _loading
              ? const Center(child: CircularProgressIndicator())
              : _error != null
                  ? _EvidenceFailure(message: _error!, retry: _load)
                  : _preview(),
        ),
      );

  Widget _preview() {
    final file = _file!;
    if (_contentType?.startsWith('image/') == true) {
      return Center(
        child: InteractiveViewer(
          minScale: .5,
          maxScale: 5,
          child: Image.file(
            file,
            semanticLabel: 'Preview of $_filename',
            errorBuilder: (_, __, ___) => _GenericEvidence(
                filename: _filename, contentType: _contentType),
          ),
        ),
      );
    }
    final video = _video;
    if (video != null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: AspectRatio(
            aspectRatio:
                video.value.aspectRatio == 0 ? 16 / 9 : video.value.aspectRatio,
            child: Stack(
              alignment: Alignment.center,
              children: [
                VideoPlayer(video),
                ValueListenableBuilder<VideoPlayerValue>(
                  valueListenable: video,
                  builder: (_, value, __) => IconButton.filledTonal(
                    tooltip: value.isPlaying ? 'Pause' : 'Play',
                    onPressed: value.isPlaying ? video.pause : video.play,
                    icon: Icon(value.isPlaying
                        ? Icons.pause_rounded
                        : Icons.play_arrow_rounded),
                  ),
                ),
                Positioned(
                  left: 0,
                  right: 0,
                  bottom: 0,
                  child: VideoProgressIndicator(video, allowScrubbing: true),
                ),
              ],
            ),
          ),
        ),
      );
    }
    final audio = _audio;
    if (audio != null) {
      return Center(
        child: StreamBuilder<PlayerState>(
          stream: audio.onPlayerStateChanged,
          initialData: audio.state,
          builder: (_, snapshot) {
            final playing = snapshot.data == PlayerState.playing;
            return FilledButton.tonalIcon(
              onPressed: playing
                  ? audio.pause
                  : () => audio.play(
                        DeviceFileSource(file.path, mimeType: _contentType),
                      ),
              icon: Icon(
                  playing ? Icons.pause_rounded : Icons.play_arrow_rounded),
              label: Text(playing ? 'Pause audio' : 'Play audio'),
            );
          },
        ),
      );
    }
    return Center(
      child: _GenericEvidence(filename: _filename, contentType: _contentType),
    );
  }
}

final class _GenericEvidence extends StatelessWidget {
  const _GenericEvidence({required this.filename, required this.contentType});

  final String filename;
  final String? contentType;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.insert_drive_file_outlined, size: 56),
            const SizedBox(height: 12),
            Text(filename, textAlign: TextAlign.center),
            if (contentType case final type?) ...[
              const SizedBox(height: 4),
              Text(type, style: Theme.of(context).textTheme.bodySmall),
            ],
            const SizedBox(height: 12),
            const Text(
              'No inline preview is available. Use Save or share original.',
              textAlign: TextAlign.center,
            ),
          ],
        ),
      );
}

final class _EvidenceFailure extends StatelessWidget {
  const _EvidenceFailure({required this.message, required this.retry});

  final String message;
  final VoidCallback retry;

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.error_outline_rounded, size: 42),
              const SizedBox(height: 12),
              Text(message, textAlign: TextAlign.center),
              const SizedBox(height: 16),
              OutlinedButton.icon(
                onPressed: retry,
                icon: const Icon(Icons.refresh_rounded),
                label: const Text('Try again'),
              ),
            ],
          ),
        ),
      );
}

String safeEvidenceFilename(String input) {
  final buffer = StringBuffer();
  const reserved = <int>{47, 92, 58, 42, 63, 34, 60, 62, 124};
  for (final rune in input.trim().runes) {
    buffer.write(rune < 32 || rune == 127 || reserved.contains(rune)
        ? '_'
        : String.fromCharCode(rune));
  }
  final sanitized = buffer.toString().trim();
  if (sanitized.isEmpty || sanitized == '.' || sanitized == '..') {
    return 'reported-evidence';
  }
  return sanitized.length <= 160 ? sanitized : sanitized.substring(0, 160);
}
