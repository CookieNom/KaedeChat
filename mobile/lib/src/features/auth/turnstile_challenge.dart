import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:uuid/uuid.dart';
import 'package:webview_flutter/webview_flutter.dart';

/// Runs the server-owned Cloudflare challenge in a restricted in-app view.
/// Credentials never enter this view; it can return only the challenge result.
final class TurnstileChallenge extends StatefulWidget {
  const TurnstileChallenge({
    required this.instance,
    required this.action,
    super.key,
  });

  final Domain instance;
  final String action;

  static Future<String?> show(
    BuildContext context, {
    required Domain instance,
    required String action,
  }) =>
      Navigator.of(context).push<String>(
        MaterialPageRoute<String>(
          fullscreenDialog: true,
          builder: (_) => TurnstileChallenge(
            instance: instance,
            action: action,
          ),
        ),
      );

  @override
  State<TurnstileChallenge> createState() => _TurnstileChallengeState();
}

final class _TurnstileChallengeState extends State<TurnstileChallenge> {
  late final String requestId;
  late final Uri challengeUri;
  late final WebViewController controller;
  var loading = true;
  var completed = false;
  String? error;

  @override
  void initState() {
    super.initState();
    requestId = const Uuid().v4().replaceAll('-', '');
    challengeUri = Uri.https(
      widget.instance.value,
      '/api/v1/auth/native-challenge',
      <String, String>{
        'action': widget.action,
        'request_id': requestId,
      },
    );
    controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setBackgroundColor(KaedeColors.canvas)
      ..addJavaScriptChannel(
        'KaedeChallenge',
        onMessageReceived: _receive,
      )
      ..setNavigationDelegate(
        NavigationDelegate(
          onNavigationRequest: (request) {
            // Turnstile renders in a provider-owned iframe. Subframes may load
            // provider resources, but the top-level document must remain the
            // exact server-owned challenge so an external page never gets the
            // native result bridge.
            if (!request.isMainFrame) return NavigationDecision.navigate;
            final uri = Uri.tryParse(request.url);
            if (uri == null || !isExpectedNativeChallenge(uri, challengeUri)) {
              return NavigationDecision.prevent;
            }
            return NavigationDecision.navigate;
          },
          onPageFinished: (url) async {
            final uri = Uri.tryParse(url);
            if (uri == null || !isExpectedNativeChallenge(uri, challengeUri)) {
              return;
            }
            await controller.runJavaScript('''
              window.ipc = {
                postMessage: function(payload) {
                  KaedeChallenge.postMessage(payload);
                }
              };
            ''');
            if (mounted) setState(() => loading = false);
          },
          onWebResourceError: (failure) {
            if (failure.isForMainFrame == true && mounted) {
              setState(() {
                loading = false;
                error = 'The security check could not be loaded.';
              });
            }
          },
        ),
      )
      ..loadRequest(challengeUri);
  }

  void _receive(JavaScriptMessage message) {
    if (!mounted || completed) return;
    try {
      final decoded = jsonDecode(message.message);
      if (decoded is! Map<Object?, Object?> ||
          decoded['request_id'] != requestId) {
        return;
      }
      if (decoded['kind'] == 'complete' && decoded['value'] is String) {
        final value = decoded['value'] as String;
        if (value.isEmpty || value.length > 2048) {
          setState(
            () => error = 'The security check returned an invalid result.',
          );
          return;
        }
        completed = true;
        Navigator.of(context).pop(value);
      } else if (decoded['kind'] == 'error' || decoded['kind'] == 'expired') {
        setState(() => error = 'Verification expired. Please try again.');
      }
    } on FormatException {
      setState(() => error = 'The security check returned an invalid result.');
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: const Text('Security check')),
        body: Stack(
          children: [
            WebViewWidget(controller: controller),
            if (loading) const Center(child: CircularProgressIndicator()),
            if (error case final message?)
              Align(
                alignment: Alignment.bottomCenter,
                child: SafeArea(
                  child: MaterialBanner(
                    content: Text(message),
                    actions: [
                      TextButton(
                        onPressed: () {
                          setState(() {
                            error = null;
                            loading = true;
                          });
                          controller.reload();
                        },
                        child: const Text('Retry'),
                      ),
                    ],
                  ),
                ),
              ),
          ],
        ),
      );
}

bool isExpectedNativeChallenge(Uri candidate, Uri expected) =>
    candidate.scheme == 'https' &&
    candidate.userInfo.isEmpty &&
    candidate.host.toLowerCase() == expected.host.toLowerCase() &&
    candidate.port == expected.port &&
    candidate.path == expected.path &&
    candidate.queryParameters['action'] == expected.queryParameters['action'] &&
    candidate.queryParameters['request_id'] ==
        expected.queryParameters['request_id'] &&
    candidate.queryParameters.length == expected.queryParameters.length &&
    !candidate.hasFragment;
