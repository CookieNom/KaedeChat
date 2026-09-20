import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/e2ee/disclosures.dart';

class DmEncryptionConsent extends ConsumerStatefulWidget {
  const DmEncryptionConsent({super.key, required this.channel});
  final KaedeChannel channel;

  @override
  ConsumerState<DmEncryptionConsent> createState() =>
      _DmEncryptionConsentState();
}

class _DmEncryptionConsentState extends ConsumerState<DmEncryptionConsent> {
  Timer? _timer;
  Map<String, Object?>? _request;
  bool _busy = false;
  bool _loading = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
    _timer = Timer.periodic(const Duration(seconds: 5), (_) => _load());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    if (_busy || _loading) return;
    _loading = true;
    try {
      final result = await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .dmEncryptionConsent(widget.channel.ref, 'status');
      if (mounted && !_busy) {
        setState(() =>
            _request = (result['request'] as Map?)?.cast<String, Object?>());
      }
    } on Object {
      // Retain the current request during a temporary connection failure.
    } finally {
      _loading = false;
    }
  }

  Future<void> _respond(String action) async {
    if (_busy ||
        _request == null ||
        (action != 'disagree' &&
            !ref.read(mobileControllerProvider).e2eeActivationEnabled)) {
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final controller = ref.read(mobileControllerProvider.notifier);
      final account = ref.read(mobileControllerProvider).user?.ref.wire;
      if (action != 'enable') {
        final result = await controller.repository.dmEncryptionConsent(
          widget.channel.ref,
          action,
          requestId: _request!['request_id'] as String,
        );
        if (!mounted) return;
        setState(() =>
            _request = (result['request'] as Map).cast<String, Object?>());
      }
      if (action != 'disagree') {
        final client = await controller.e2eeClient();
        final updated = await client.enableRoom(widget.channel);
        if (account != null) {
          await acknowledgeEncryptedRoom(account, updated.ref.wire);
        }
        await controller.refreshNavigation();
      }
    } on Object catch (caught) {
      if (mounted) {
        setState(() => _error = userFacingError(caught,
            summary:
                'Could not update this encryption request. Please try again.'));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final request = _request;
    if (request == null) return const SizedBox.shrink();
    final state = ref.watch(mobileControllerProvider);
    final requester = request['requester'] as Map;
    final mine =
        '${requester['id']}@${requester['domain']}' == state.user?.ref.wire;
    final status = request['status'];
    final disabled = _busy || !state.e2eeActivationEnabled;
    return ConstrainedBox(
      constraints:
          BoxConstraints(maxHeight: MediaQuery.sizeOf(context).height * .4),
      child: SingleChildScrollView(
        child: Card(
          margin: const EdgeInsets.all(8),
          child: Padding(
            padding: const EdgeInsets.all(16),
            child:
                Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(
                  mine
                      ? 'You requested end-to-end encryption'
                      : '${request['requester_name']} requested end-to-end encryption',
                  style: const TextStyle(fontWeight: FontWeight.bold)),
              if (status == 'pending' || status == 'approved') ...[
                const SizedBox(height: 8),
                const Text(
                    'Only you and the other participant can read future encrypted messages. Earlier messages stay unencrypted. Once enabled, encryption cannot be turned off in this conversation.'),
                const ExpansionTile(
                  tilePadding: EdgeInsets.zero,
                  title: Text('Read the warnings before agreeing'),
                  children: [
                    Padding(
                      padding: EdgeInsets.only(bottom: 12),
                      child: Text(
                          'Server search, automatic link previews, and integrations without encrypted access stop working. Notifications may become generic.\n\nEncrypted files are not scanned by the server. Unsupported clients cannot use this conversation.\n\nLosing your key vault, trusted devices, and recovery backup means losing encrypted history.\n\nServers can still see who communicates and when. Recipients can save or share messages. Compare your safety number through another trusted channel to verify identities.'),
                    )
                  ],
                ),
                if (status == 'approved') ...[
                  const Text(
                      'You both agreed. Encryption still needs to finish turning on.'),
                  FilledButton(
                      onPressed: disabled ? null : () => _respond('enable'),
                      child: const Text('Finish enabling encryption')),
                ] else if (mine)
                  const Text(
                      'Waiting for the other participant to agree. Messages are still unencrypted.')
                else
                  Wrap(spacing: 8, runSpacing: 8, children: [
                    FilledButton(
                        onPressed: disabled ? null : () => _respond('agree'),
                        child: Text(_busy
                            ? 'Please wait…'
                            : 'Agree and enable encryption')),
                    OutlinedButton(
                        onPressed: _busy ? null : () => _respond('disagree'),
                        child: const Text('Disagree')),
                  ]),
                if (!state.e2eeActivationEnabled)
                  const Text(
                      'New encryption requests are currently disabled by the server.'),
              ] else ...[
                const SizedBox(height: 8),
                Text(
                    '${status == 'declined' ? 'The request was declined.' : 'This request expired.'} This conversation is still unencrypted.'),
              ],
              if (_error != null)
                Text(_error!,
                    style:
                        TextStyle(color: Theme.of(context).colorScheme.error)),
            ]),
          ),
        ),
      ),
    );
  }
}
