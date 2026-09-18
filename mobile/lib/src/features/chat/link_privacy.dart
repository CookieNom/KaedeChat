import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/features/shared/action_feedback.dart';
import 'package:shared_preferences/shared_preferences.dart';

String stripLinkTracking(String content) => content.replaceAllMapped(
      RegExp(r'''https?://[^\s<>"`]+''', caseSensitive: false),
      (match) {
        final matched = match[0]!;
        final suffix = RegExp(r'[.,!;:)\]]+$').firstMatch(matched)?[0] ?? '';
        final raw = matched.substring(0, matched.length - suffix.length);
        final url = Uri.tryParse(raw);
        if (url == null) return matched;
        final host = url.host.toLowerCase();
        if (!(host == 'youtu.be' ||
            host == 'youtube.com' ||
            host.endsWith('.youtube.com') ||
            host == 'open.spotify.com')) {
          return matched;
        }
        final question = raw.indexOf('?');
        final hash = raw.indexOf('#');
        if (question < 0 || (hash >= 0 && hash < question)) return matched;
        final end = hash < 0 ? raw.length : hash;
        final fields = raw.substring(question + 1, end).split('&');
        final kept = fields.where((field) {
          try {
            return Uri.decodeQueryComponent(field.split('=').first) != 'si';
          } on FormatException {
            return true;
          }
        }).toList();
        if (kept.length == fields.length) return matched;
        return raw.substring(0, question) +
            (kept.isEmpty ? '' : '?${kept.join('&')}') +
            raw.substring(end) +
            suffix;
      },
    );

final _choices = <String, bool>{};

Future<String?> preparePrivateLinks(
  BuildContext context,
  String content,
  String account,
) async {
  final stripped = stripLinkTracking(content);
  if (stripped == content) return content;
  final key = 'kaede:strip-link-si:$account';
  final preferences = await SharedPreferences.getInstance();
  var enabled = _choices[key] ?? preferences.getBool(key);
  if (enabled == null) {
    if (!context.mounted) return null;
    enabled = await showDialog<bool>(
      context: context,
      barrierDismissible: false,
      builder: (context) => PopScope(
        canPop: false,
        child: AlertDialog(
          title: const Text('Remove link tracking?'),
          content: const Text(
            'YouTube and Spotify sharing links can contain an si tracking code '
            'that may reveal information about how you shared the link. '
            'Automatically remove only si when sending this and future messages? '
            'Your choice is saved for this account on this device.',
          ),
          actions: [
            ActionButton(
              kind: ActionButtonKind.text,
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Keep tracking'),
            ),
            ActionButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Remove tracking'),
            ),
          ],
        ),
      ),
    );
    if (enabled == null) return null;
    _choices[key] = enabled;
    await preferences.setBool(key, enabled);
  }
  return enabled ? stripped : content;
}
