import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:url_launcher/url_launcher.dart';

final shoppingLinks = ShoppingLinks();

class ShoppingLinks extends ChangeNotifier {
  EntityRef? _account;
  List<Map<String, dynamic>> _rules = [];
  bool available = false;
  bool enabled = false;
  bool busy = false;
  int _generation = 0;

  void reset(EntityRef? account) {
    _generation++;
    _account = account;
    _savingChoice = false;
    _rules = [];
    available = false;
    enabled = false;
    busy = false;
    notifyListeners();
  }

  Uri destination(Uri uri, EntityRef? account) {
    if (account == null ||
        account != _account ||
        uri.scheme != 'https' ||
        uri.userInfo.isNotEmpty ||
        (uri.hasPort && uri.port != 443)) {
      return uri;
    }
    for (final rule in _rules) {
      if (rule['host'] != uri.host) continue;
      final key = rule['parameter'] as String;
      final fields = uri.query.isEmpty ? <String>[] : uri.query.split('&');
      try {
        final kept = fields
            .where((f) => Uri.decodeQueryComponent(f.split('=').first) != key)
            .toList();
        if (kept.length != fields.length && rule['replace'] != true) return uri;
        kept.add(
            '${Uri.encodeQueryComponent(key)}=${Uri.encodeQueryComponent(rule['value'] as String)}');
        return uri.replace(query: kept.join('&'));
      } on FormatException {
        return uri;
      }
    }
    return uri;
  }

  void _apply(Map<String, Object?> state) {
    available = state['available'] == true;
    enabled = state['enabled'] == true;
    _rules = (state['rules'] as List? ?? [])
        .whereType<Map<String, dynamic>>()
        .toList();
    notifyListeners();
  }

  Future<void> open(
      BuildContext context, KaedeApiClient api, EntityRef account) async {
    if (busy || account != _account) return;
    final generation = _generation;
    busy = true;
    try {
      final state = await api.sendJson(
          'POST', '/api/v1/users/@me/shopping-links/open',
          query: {'platform': 'mobile'});
      if (!context.mounted || generation != _generation) return;
      _apply(state);
      if (state['prompt'] != true) return;
      final choice = await showDialog<bool>(
        context: context,
        builder: (dialog) => AlertDialog(
          scrollable: true,
          title: const Text('Use affiliate links?'),
          content: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              mainAxisSize: MainAxisSize.min,
              children: [
                const Text(
                    'When you open an eligible shopping link, Kaede can add a shared affiliate code. The retailer may pay a commission at no extra cost to you. The code contains no personal identifier.'),
                TextButton(
                    onPressed: () => launchUrl(
                        Uri.https(account.domain.value, '/privacy')
                            .replace(fragment: 'shopping-links'),
                        mode: LaunchMode.externalApplication),
                    child: const Text('Read the privacy policy')),
                FilledButton(
                    style: FilledButton.styleFrom(
                        backgroundColor: const Color(0xff247447),
                        foregroundColor: Colors.white),
                    onPressed: () => Navigator.pop(dialog, true),
                    child: const Text('Yes, use affiliate links')),
                const SizedBox(height: 8),
                FilledButton(
                    style: FilledButton.styleFrom(
                        backgroundColor: const Color(0xff525b65),
                        foregroundColor: Colors.white),
                    onPressed: () => Navigator.pop(dialog, false),
                    child: const Text('No, keep original links')),
              ]),
        ),
      );
      if (choice == true && context.mounted && generation == _generation) {
        await _save(api, true, generation);
      }
    } catch (_) {
      if (generation == _generation) {
        if (!_savingChoice) _apply({});
        if (context.mounted && _savingChoice) {
          ScaffoldMessenger.maybeOf(context)?.showSnackBar(const SnackBar(
              content:
                  Text('Could not save your choice. Try again in Settings.')));
        }
      }
    } finally {
      if (generation == _generation) {
        busy = false;
        _savingChoice = false;
        notifyListeners();
      }
    }
  }

  bool _savingChoice = false;
  Future<void> _save(KaedeApiClient api, bool value, int generation) async {
    _savingChoice = true;
    final state = await api.sendJson('PUT', '/api/v1/users/@me/shopping-links',
        query: {'platform': 'mobile'}, data: {'enabled': value});
    if (generation == _generation) _apply(state);
  }

  Future<void> choose(
      BuildContext context, KaedeApiClient api, bool value) async {
    if (busy) return;
    final generation = _generation;
    busy = true;
    notifyListeners();
    try {
      await _save(api, value, generation);
    } catch (_) {
      if (context.mounted) {
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
            content: Text('Could not save your choice. Please try again.')));
      }
    } finally {
      if (generation == _generation) {
        busy = false;
        _savingChoice = false;
        notifyListeners();
      }
    }
  }
}
