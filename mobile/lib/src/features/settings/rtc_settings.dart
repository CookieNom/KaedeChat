import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/rtc_routing.dart';
import 'package:kaede_mobile/src/core/errors.dart';

class RtcSettings extends StatefulWidget {
  const RtcSettings({super.key, required this.repository});
  final KaedeRepository repository;
  @override
  State<RtcSettings> createState() => _RtcSettingsState();
}

class _RtcSettingsState extends State<RtcSettings> {
  Map<String, Object?>? _config;
  final _key = TextEditingController();
  final _secret = TextEditingController();
  final _automatic = TextEditingController();
  final _regions = <Map<String, Object?>>[];
  bool _busy = false;
  bool _dirty = false;
  String? _message;
  String? _error;
  final _results = <String, String>{};
  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _key.dispose();
    _secret.dispose();
    _automatic.dispose();
    super.dispose();
  }

  void _apply(Map<String, Object?> config) {
    _config = config;
    _key.text = '${config['api_key'] ?? ''}';
    _secret.clear();
    _automatic.text = '${config['automatic_url'] ?? ''}';
    _regions
      ..clear()
      ..addAll((config['regions'] as List? ?? [])
          .whereType<Map<Object?, Object?>>()
          .map((r) => Map<String, Object?>.from(r)));
    _dirty = false;
  }

  Future<void> _load() async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final config =
          await widget.repository.api.getJson('/api/v1/administration/rtc');
      if (mounted) setState(() => _apply(config));
    } on Object catch (error) {
      if (mounted) setState(() => _error = userFacingError(error));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _save() async {
    if (_busy || _config == null) return;
    setState(() {
      _busy = true;
      _error = null;
      _message = null;
    });
    try {
      final config = await widget.repository.api
          .sendJson('PUT', '/api/v1/administration/rtc', data: {
        'provider': _config!['provider'],
        'api_key': _key.text,
        'api_secret': _secret.text.isEmpty ? null : _secret.text,
        'automatic_url': _automatic.text,
        'regions': _regions,
        'default_region': _config!['default_region'],
        'allow_region_selection': _config!['allow_region_selection'],
      });
      if (mounted) {
        setState(() {
          _apply(config);
          _results.clear();
          _message =
              'Settings saved. Active calls keep their current placement.';
        });
      }
    } on Object catch (error) {
      if (mounted) setState(() => _error = userFacingError(error));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _test(String region, {bool fallback = false}) async {
    if (_busy || _dirty) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      Map<String, Object?> routing = {'region': region};
      if (region == 'automatic' && !fallback) {
        final probes = await widget.repository.api
            .getJson('/api/v1/administration/rtc/probes');
        routing = {
          ...routing,
          'probe_ticket': probes['probe_ticket'],
          'latency': await measureRtcLatency((probes['probes'] as List?) ?? [])
        };
      }
      final result = await widget.repository.api
          .sendJson('POST', '/api/v1/administration/rtc/test', data: routing);
      if (mounted) {
        setState(() => _results[region] =
            '${result['ok'] == true ? 'Passed' : 'Failed'}: ${result['message']}');
      }
    } on Object catch (error) {
      if (mounted) setState(() => _error = userFacingError(error));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _changed() => setState(() {
        _dirty = true;
        _message = null;
      });
  Widget _field(String label, TextEditingController controller,
          {bool secret = false, String? hint}) =>
      Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: TextField(
            controller: controller,
            enabled: !_busy,
            obscureText: secret,
            autocorrect: false,
            enableSuggestions: false,
            decoration: InputDecoration(
                labelText: label,
                hintText: hint,
                border: const OutlineInputBorder()),
            onChanged: (_) => _changed()),
      );
  @override
  Widget build(BuildContext context) {
    final config = _config;
    final canEnable = _key.text.isNotEmpty &&
        _automatic.text.isNotEmpty &&
        (_secret.text.length >= 32 ||
            (config?['secret_configured'] == true &&
                _key.text == config?['api_key']));
    final canTest = !_busy &&
        !_dirty &&
        config?['secret_configured'] == true &&
        _key.text.isNotEmpty &&
        _automatic.text.isNotEmpty;
    final enabled = _regions.where((r) => r['enabled'] == true).toList();
    final hooks = config?['webhooks'] as Map?;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      if (_busy) const LinearProgressIndicator(),
      if (_error != null)
        Padding(
            padding: const EdgeInsets.all(8),
            child: Text(_error!,
                style: TextStyle(color: Theme.of(context).colorScheme.error))),
      if (_message != null)
        Padding(padding: const EdgeInsets.all(8), child: Text(_message!)),
      if (config == null)
        OutlinedButton(
            onPressed: _busy ? null : _load,
            child: const Text('Reload RTC settings')),
      if (config != null) ...[
        DropdownButtonFormField<String>(
            initialValue: '${config['provider']}',
            decoration:
                const InputDecoration(labelText: 'Voice and video provider'),
            items: const [
              DropdownMenuItem(
                  value: 'builtin', child: Text('Built-in LiveKit')),
              DropdownMenuItem(value: 'cinnamon', child: Text('Cinnamon RTC'))
            ],
            onChanged: _busy
                ? null
                : (v) {
                    config['provider'] = v;
                    _changed();
                  }),
        const SizedBox(height: 12),
        const Text(
            'Built-in LiveKit is the default. Use Cinnamon project credentials, not an administration token.'),
        _field('Project API key', _key),
        _field('Project API secret', _secret,
            secret: true,
            hint: config['secret_configured'] == true
                ? '•••••••• — saved; leave blank to keep'
                : 'Enter project secret'),
        _field('Automatic-routing endpoint', _automatic, hint: 'wss://'),
        const Text('Regional endpoints',
            style: TextStyle(fontWeight: FontWeight.bold)),
        if (_regions.isEmpty) const Text('No regions configured.'),
        for (final region in _regions)
          Card(
              key: ObjectKey(region),
              child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(children: [
                    for (final field in const {
                      'id': 'Region ID',
                      'name': 'Display name',
                      'url': 'Endpoint'
                    }.entries)
                      Padding(
                          padding: const EdgeInsets.symmetric(vertical: 6),
                          child: TextFormField(
                              key: ValueKey(field.key),
                              initialValue: '${region[field.key] ?? ''}',
                              enabled: !_busy,
                              decoration: InputDecoration(
                                  labelText: field.value,
                                  border: const OutlineInputBorder()),
                              onChanged: (v) {
                                region[field.key] = v;
                                _changed();
                              })),
                    SwitchListTile(
                        contentPadding: EdgeInsets.zero,
                        title: const Text('Enabled'),
                        value: region['enabled'] == true,
                        onChanged: _busy
                            ? null
                            : (v) {
                                region['enabled'] = v;
                                _changed();
                              }),
                    OutlinedButton(
                        onPressed: _busy
                            ? null
                            : () {
                                _regions.remove(region);
                                _changed();
                              },
                        child: const Text('Remove region')),
                  ]))),
        OutlinedButton(
            onPressed: _busy || _regions.length >= 64
                ? null
                : () {
                    _regions.add(
                        {'id': '', 'name': '', 'url': '', 'enabled': true});
                    _changed();
                  },
            child: const Text('Add region')),
        DropdownButtonFormField<String>(
            key: ValueKey(
                '${config['default_region']}:${enabled.map((r) => r['id']).join(',')}'),
            initialValue:
                enabled.any((r) => r['id'] == config['default_region'])
                    ? '${config['default_region']}'
                    : '',
            decoration: const InputDecoration(labelText: 'Default routing'),
            items: [
              const DropdownMenuItem(value: '', child: Text('Automatic')),
              for (final id in enabled
                  .map((r) => '${r['id']}')
                  .where((s) => s.isNotEmpty)
                  .toSet())
                DropdownMenuItem(
                    value: id,
                    child: Text(
                        '${enabled.firstWhere((r) => r['id'] == id)['name']}'))
            ],
            onChanged: _busy
                ? null
                : (v) {
                    config['default_region'] = v == '' ? null : v;
                    _changed();
                  }),
        SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Allow users to select a region'),
            value: config['allow_region_selection'] == true,
            onChanged: _busy
                ? null
                : (v) {
                    config['allow_region_selection'] = v;
                    _changed();
                  }),
        const Text(
            'Only successful client latency measurements are sent. If every probe fails, Cinnamon uses eligible-node load and capacity. Location is never collected.'),
        const SizedBox(height: 12),
        FilledButton(
            onPressed: _busy ||
                    !_dirty ||
                    (config['provider'] == 'cinnamon' && !canEnable)
                ? null
                : _save,
            child: const Text('Save RTC settings')),
        const SizedBox(height: 16),
        const Text(
            'Connection tests create and remove disposable rooms. Save changes before testing.'),
        OutlinedButton(
            onPressed: canTest ? () => _test('automatic') : null,
            child: const Text('Test Automatic')),
        OutlinedButton(
            onPressed:
                canTest ? () => _test('automatic', fallback: true) : null,
            child: const Text('Test without latency hints')),
        for (final region in enabled)
          OutlinedButton(
              onPressed: canTest ? () => _test('${region['id']}') : null,
              child: Text('Test ${region['name']}')),
        for (final result in _results.entries)
          Text('${result.key}: ${result.value}'),
        const SizedBox(height: 16),
        const Text('Public webhook receiver',
            style: TextStyle(fontWeight: FontWeight.bold)),
        SelectableText('${config['webhook_url']}'),
        Text('Signing API-key identifier: ${config['api_key']}'),
        Text('Enabled region IDs: ${enabled.map((r) => r['id']).join(', ')}'),
        const Text(
            'On Cinnamon, permit automatic and manual routing for these regions, allow routing without hints, and sign webhooks with this project key.'),
        Text(
            'Webhooks: ${hooks?['accepted'] ?? 0} accepted; ${hooks?['pending'] ?? 0} pending. Last processed: ${hooks?['last_processed_at'] ?? 'None'}'),
        OutlinedButton(
            onPressed: _busy || _dirty ? null : _load,
            child: const Text('Refresh webhook status')),
      ],
    ]);
  }
}
