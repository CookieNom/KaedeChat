import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/debug_log.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/e2ee/disclosures.dart';
import 'package:kaede_mobile/src/e2ee/setup_retry.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';

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
  bool _enabling = false;
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
      _enabling = action != 'disagree';
      _error = null;
    });
    var step = 'consent';
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
        // Starting the client and enabling the room both resume safely, so
        // retry here instead of asking the user to press the button again.
        step = 'open_encryption';
        final client = await retryE2eeSetup(
          controller.e2eeClient,
          waitForBusyVault: true,
          keepTrying: () => mounted,
        );
        step = 'enable_room';
        final updated = await client.enableRoom(
          widget.channel,
          waitForBusyVault: true,
          keepTrying: () => mounted,
        );
        step = 'refresh';
        if (account != null) {
          await acknowledgeEncryptedRoom(account, updated.ref.wire);
        }
        await controller.refreshNavigation();
      }
    } on Object catch (caught) {
      DebugLog.instance.record(DebugEvent.encryptionSetupFailed,
          step: step,
          error: caught,
          status: caught is KaedeException ? caught.status : null);
      if (mounted) {
        setState(() => _error = userFacingError(caught,
            summary:
                'Could not update this encryption request. Please try again.'));
      }
    } finally {
      if (mounted) {
        setState(() {
          _busy = false;
          _enabling = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final request = _request;
    if (request == null) return const SizedBox.shrink();
    if (_enabling) return const _EnablingEncryptionCard();
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
                    'Only you and the other participant can read future encrypted messages. Earlier messages stay unencrypted. Messages sent with encryption stay encrypted.'),
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

/// Stands in for the full consent card while encryption is being turned on,
/// so the wait takes one row instead of covering the conversation.
class _EnablingEncryptionCard extends StatelessWidget {
  const _EnablingEncryptionCard();

  @override
  Widget build(BuildContext context) {
    return Card(
      margin: const EdgeInsets.all(8),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        child: Semantics(
          container: true,
          liveRegion: true,
          child: Row(children: [
            const SizedBox.square(
              dimension: 20,
              child: CircularProgressIndicator(strokeWidth: 2),
            ),
            const SizedBox(width: 16),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Text('Turning on end-to-end encryption…',
                      style: TextStyle(fontWeight: FontWeight.bold)),
                  const SizedBox(height: 2),
                  Text('This can take several minutes. Keep Kaede open.',
                      style: Theme.of(context).textTheme.bodySmall),
                ],
              ),
            ),
          ]),
        ),
      ),
    );
  }
}

class DmContinuationConsent extends ConsumerStatefulWidget {
  const DmContinuationConsent(
      {required this.channel,
      required this.callActive,
      this.settings = false,
      super.key});
  final bool callActive;
  final bool settings;
  final KaedeChannel channel;
  @override
  ConsumerState<DmContinuationConsent> createState() =>
      _DmContinuationConsentState();
}

class _DmContinuationConsentState extends ConsumerState<DmContinuationConsent> {
  Timer? _timer;
  int _revision = 0;
  bool _declined = false;
  bool _busy = false, _reading = false, _available = false;
  Map<String, Object?>? _proposal;
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

  @override
  void didUpdateWidget(covariant DmContinuationConsent oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.channel.continuationRequester !=
        widget.channel.continuationRequester) {
      unawaited(_load());
    }
  }

  Future<void> _apply(Map<String, Object?> result) async {
    if (!mounted) return;
    if (result['channel_ref'] != null) {
      await ref.read(mobileControllerProvider.notifier).refreshNavigation();
      return;
    }
    setState(() {
      final oldStatus = _proposal?['status'];
      final newStatus = (result['request'] as Map?)?['status'];
      if (newStatus != 'declined') {
        _declined = false;
      } else if (oldStatus == 'pending' || oldStatus == 'approved') {
        _declined = true;
      }
      _proposal = (result['request'] as Map?)?.cast<String, Object?>();
      _available = result['available'] != false;
    });
  }

  Future<void> _load() async {
    if (_busy || _reading) return;
    _reading = true;
    final revision = _revision;
    try {
      final result = await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .dmContinuation(widget.channel.ref, 'status');
      if (mounted && revision == _revision) await _apply(result);
    } on Object {
      // Retry the status check on the next tick.
    } finally {
      _reading = false;
    }
  }

  Future<void> _act(String action) async {
    if (_busy ||
        !_available ||
        (action == 'commit' && widget.callActive) ||
        (action != 'request' && _proposal == null)) {
      return;
    }
    final token = _proposal?['request_id'] as String?;
    final repo = ref.read(mobileControllerProvider.notifier).repository;
    setState(() {
      _busy = true;
      _error = null;
      _revision++;
    });
    try {
      if (action == 'request' || action == 'agree') {
        final confirmed = await showDialog<bool>(
            context: context,
            builder: (context) => AlertDialog(
                    title: Text(L10n.of(context).dm_privacy_question),
                    content: Text(L10n.of(context).dm_privacy_explain),
                    actions: [
                      TextButton(
                          onPressed: () => Navigator.pop(context, false),
                          child: Text(L10n.of(context).dm_privacy_cancel)),
                      FilledButton(
                          onPressed: () => Navigator.pop(context, true),
                          child: Text(action == 'request'
                              ? L10n.of(context).dm_privacy_send
                              : L10n.of(context).dm_privacy_agree))
                    ]));
        if (!mounted || confirmed != true) return;
      }
      final result = await repo.dmContinuation(widget.channel.ref, action,
          requestId: token);
      if (!mounted) return;
      await _apply(result);
      if (mounted &&
          !widget.settings &&
          !widget.callActive &&
          _proposal?['status'] == 'approved' &&
          action != 'commit') {
        await _apply(await repo.dmContinuation(widget.channel.ref, 'commit',
            requestId: _proposal!['request_id'] as String));
      }
    } on Object catch (e) {
      if (mounted) setState(() => _error = userFacingError(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    if (!_available) return const SizedBox.shrink();
    final status = _proposal?['status'];
    final pending = status == 'pending' || status == 'approved';
    if (widget.settings) {
      return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        if (!pending)
          OutlinedButton(
              onPressed: _busy ? null : () => _act('request'),
              child: Text(L10n.of(context).dm_privacy_request))
        else
          Text(L10n.of(context).dm_privacy_asked(
              '${_proposal?["requester_name"] ?? _proposal?["requester"] ?? ""}')),
        if (_error != null)
          Text(_error!,
              style: TextStyle(color: Theme.of(context).colorScheme.error)),
      ]);
    }
    if (!pending && !_declined) return const SizedBox.shrink();
    if (!pending) {
      return ListTile(
          dense: true,
          title: Text(L10n.of(context).dm_privacy_declined),
          trailing: IconButton(
              tooltip: L10n.of(context).ui_close_cd86acc3,
              icon: const Icon(Icons.close),
              onPressed: () => setState(() => _declined = false)));
    }
    final votes = _proposal?['approved_by'] as List? ?? const [];
    final mine =
        votes.contains(ref.watch(mobileControllerProvider).user?.ref.wire);
    final roster =
        (_proposal?['context'] as Map?)?['participants'] as List? ?? const [];
    return ConstrainedBox(
        constraints:
            BoxConstraints(maxHeight: MediaQuery.sizeOf(context).height * .32),
        child: SingleChildScrollView(
            child: ExpansionTile(
                key: ValueKey(status),
                initiallyExpanded: true,
                title: Text(L10n.of(context).dm_privacy_asked(
                    '${_proposal?["requester_name"] ?? _proposal?["requester"] ?? ""}')),
                childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
                children: [
              Text(L10n.of(context)
                  .dm_privacy_votes(votes.length, roster.length)),
              if (status == 'approved' && widget.callActive)
                Text(L10n.of(context).dm_privacy_end_call),
              Wrap(spacing: 8, children: [
                if (status == 'approved')
                  FilledButton(
                      onPressed: _busy || widget.callActive
                          ? null
                          : () => _act('commit'),
                      child: Text(L10n.of(context).dm_privacy_finish))
                else if (!mine)
                  FilledButton(
                      onPressed: _busy ? null : () => _act('agree'),
                      child: Text(L10n.of(context).dm_privacy_agree)),
                TextButton(
                    onPressed: _busy ? null : () => _act('disagree'),
                    child: Text(mine
                        ? L10n.of(context).dm_privacy_withdraw
                        : L10n.of(context).dm_privacy_decline)),
              ]),
              if (_error != null)
                Text(_error!,
                    style:
                        TextStyle(color: Theme.of(context).colorScheme.error)),
            ])));
  }
}
