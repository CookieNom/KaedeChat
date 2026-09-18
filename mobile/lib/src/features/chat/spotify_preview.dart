import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:kaede_mobile/src/features/shared/action_feedback.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:webview_flutter/webview_flutter.dart';

Uri? spotifyEmbedUri(String value) {
  final uri = Uri.tryParse(value);
  if (uri == null ||
      uri.scheme != 'https' ||
      uri.host != 'open.spotify.com' ||
      uri.userInfo.isNotEmpty ||
      uri.port != 443) {
    return null;
  }
  final match = RegExp(
          r'^/(?:intl-[a-zA-Z-]+/)?(track|album|playlist|artist|episode|show)/([a-zA-Z0-9]{22})/?$')
      .firstMatch(uri.path);
  return match == null
      ? null
      : Uri.https('open.spotify.com', '/embed/${match[1]}/${match[2]}');
}

/// Keeps the card's size stable while releasing offscreen native players.
class SpotifyPreview extends StatefulWidget {
  const SpotifyPreview({required this.uri, super.key});
  final Uri uri;

  @override
  State<SpotifyPreview> createState() => _SpotifyPreviewState();
}

class _SpotifyPreviewState extends State<SpotifyPreview>
    with WidgetsBindingObserver {
  final _positions = <ScrollPosition>[];
  bool _visible = false;
  bool _scheduled = false;
  bool _foreground = true;
  bool _enabled = true;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _foreground = WidgetsBinding.instance.lifecycleState == null ||
        WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed;
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    for (final position in _positions) {
      position.removeListener(_scheduleVisibility);
    }
    _positions.clear();
    var scrollable = Scrollable.maybeOf(context);
    while (scrollable != null) {
      _positions.add(scrollable.position);
      scrollable.position.addListener(_scheduleVisibility);
      scrollable = Scrollable.maybeOf(scrollable.context);
    }
    _enabled = TickerMode.valuesOf(context).enabled &&
        (ModalRoute.isCurrentOf(context) ?? true);
    _scheduleVisibility();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _foreground = state == AppLifecycleState.resumed;
    _scheduleVisibility();
  }

  @override
  void didChangeMetrics() => _scheduleVisibility();

  void _scheduleVisibility() {
    if (_scheduled) return;
    _scheduled = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _scheduled = false;
      if (!mounted) return;
      final box = context.findRenderObject();
      var visible = false;
      if (_enabled && _foreground && box is RenderBox && box.hasSize) {
        var bounds = MatrixUtils.transformRect(
            box.getTransformTo(null), box.paintBounds);
        RenderObject? ancestor = box.parent;
        while (ancestor != null) {
          if (ancestor is RenderAbstractViewport) {
            bounds = bounds.intersect(MatrixUtils.transformRect(
                ancestor.getTransformTo(null), ancestor.paintBounds));
          }
          ancestor = ancestor.parent;
        }
        visible = !bounds.isEmpty;
      }
      if (_visible != visible) setState(() => _visible = visible);
    });
    WidgetsBinding.instance.ensureVisualUpdate();
  }

  @override
  void dispose() {
    for (final position in _positions) {
      position.removeListener(_scheduleVisibility);
    }
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) =>
      LayoutBuilder(builder: (context, constraints) {
        _scheduleVisibility();
        return Padding(
          padding: const EdgeInsets.only(top: 8),
          child: Align(
            alignment: AlignmentDirectional.centerStart,
            child: SizedBox(
              width: 520,
              height: 152,
              child: ClipRRect(
                borderRadius: BorderRadius.circular(12),
                child: _visible && _enabled && _foreground
                    ? _SpotifyPlayer(key: ValueKey(widget.uri), uri: widget.uri)
                    : ColoredBox(color: context.kaede.raised),
              ),
            ),
          ),
        );
      });
}

class _SpotifyPlayer extends StatefulWidget {
  const _SpotifyPlayer({required this.uri, super.key});
  final Uri uri;

  @override
  State<_SpotifyPlayer> createState() => _SpotifyPlayerState();
}

class _SpotifyPlayerState extends State<_SpotifyPlayer>
    with WidgetsBindingObserver {
  late final WebViewController _controller;
  bool _loading = true;
  bool _failed = false;
  bool _active = true;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _controller = WebViewController();
    unawaited(_load());
  }

  Future<void> _load() async {
    try {
      await _controller.setJavaScriptMode(JavaScriptMode.unrestricted);
      await _controller.setBackgroundColor(Colors.transparent);
      await _controller.setNavigationDelegate(NavigationDelegate(
        onNavigationRequest: (request) {
          if (request.url == 'about:blank') return NavigationDecision.navigate;
          if (!mounted || !_active) return NavigationDecision.prevent;
          if (!request.isMainFrame || request.url == widget.uri.toString()) {
            return NavigationDecision.navigate;
          }
          final uri = Uri.tryParse(request.url);
          if (uri != null && uri.scheme == 'https') {
            unawaited(launchUrl(uri, mode: LaunchMode.externalApplication));
          }
          return NavigationDecision.prevent;
        },
        onPageFinished: (_) {
          if (mounted) setState(() => _loading = false);
        },
        onWebResourceError: (error) {
          if (mounted && error.isForMainFrame == true) {
            setState(() => _failed = true);
          }
        },
      ));
      if (mounted && _active) await _controller.loadRequest(widget.uri);
    } on Object {
      if (mounted) setState(() => _failed = true);
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _active = state == AppLifecycleState.resumed;
    // Paused apps may not render another frame to dispose the widget immediately.
    if (state != AppLifecycleState.resumed) {
      _clearDocument();
    } else {
      setState(() => _loading = true);
      unawaited(_load());
    }
  }

  void _clearDocument() {
    unawaited(_controller
        .loadRequest(Uri.parse('about:blank'))
        .catchError((Object _) {}));
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    // Clear the document (and its audio) even if the platform retains the controller.
    _clearDocument();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => ColoredBox(
        color: context.kaede.raised,
        child: _failed
            ? Center(
                child: ActionButton(
                kind: ActionButtonKind.text,
                onPressed: () => launchUrl(
                    widget.uri.replace(
                        path: widget.uri.path.replaceFirst('/embed', '')),
                    mode: LaunchMode.externalApplication),
                icon: const Icon(Icons.open_in_new, size: 18),
                label: Text('${L10n.of(context).ui_open_538b10e9} Spotify'),
              ))
            : Stack(fit: StackFit.expand, children: [
                // No eager/drag recognizers: vertical drags belong to the chat list.
                WebViewWidget(controller: _controller),
                if (_loading)
                  const Positioned(
                      left: 0,
                      right: 0,
                      top: 0,
                      child: LinearProgressIndicator(minHeight: 2)),
              ]),
      );
}
