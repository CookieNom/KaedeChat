import 'dart:async';

import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/domain/application_installations.dart';
import 'package:kaede_mobile/src/domain/bot_e2ee_participation.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

/// Participant-app consent for an encrypted DM or group conversation.
///
/// Every human participant consents independently. This screen never treats
/// account authorization as room access and displays the server-authoritative
/// consent/device state after each mutation.
final class DmBotE2eeParticipationScreen extends StatefulWidget {
  const DmBotE2eeParticipationScreen({
    super.key,
    required this.channel,
    required this.repository,
  });

  final KaedeChannel channel;
  final KaedeRepository repository;

  @override
  State<DmBotE2eeParticipationScreen> createState() =>
      _DmBotE2eeParticipationScreenState();
}

final class _DmBotE2eeParticipationScreenState
    extends State<DmBotE2eeParticipationScreen> {
  List<UserApplicationInstallation> _installations = const [];
  UserApplicationInstallation? _selected;
  DmBotE2eeParticipation? _participation;
  var _loading = true;
  var _busy = false;
  String? _error;
  String? _notice;

  @override
  void initState() {
    super.initState();
    unawaited(_loadInstallations());
  }

  Future<void> _loadInstallations() async {
    setState(() {
      _loading = true;
      _error = null;
      _notice = null;
    });
    try {
      final installations =
          (await widget.repository.userApplicationInstallations())
              .where((item) => item.supportsEncryptedPrivateConversation)
              .toList(growable: false);
      if (!mounted) return;
      setState(() {
        _installations = installations;
        _selected = installations.firstOrNull;
      });
      await _loadParticipation();
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = userFacingError(
          error,
          summary: 'Could not load participant-capable apps',
        );
      });
    }
  }

  Future<void> _loadParticipation() async {
    final selected = _selected;
    if (selected == null) {
      if (mounted) setState(() => _loading = false);
      return;
    }
    setState(() {
      _loading = true;
      _participation = null;
      _error = null;
      _notice = null;
    });
    try {
      final participation = await widget.repository.dmBotE2eeParticipation(
        channel: widget.channel.ref,
        application: selected.application,
      );
      if (mounted && _selected?.id == selected.id) {
        setState(() {
          _participation = participation;
          _loading = false;
        });
      }
    } on KaedeException catch (error) {
      if (!mounted || _selected?.id != selected.id) return;
      if (error.code == 'BOT_E2EE_PARTICIPATION_NOT_FOUND') {
        setState(() {
          _participation = null;
          _loading = false;
        });
      } else {
        setState(() {
          _loading = false;
          _error = userFacingError(
            error,
            summary: 'Could not load encrypted app consent',
          );
        });
      }
    } on Object catch (error) {
      if (!mounted || _selected?.id != selected.id) return;
      setState(() {
        _loading = false;
        _error = userFacingError(
          error,
          summary: 'Could not load encrypted app consent',
        );
      });
    }
  }

  Future<void> _select(String? installationId) async {
    if (installationId == null || _busy) return;
    final selected =
        _installations.where((item) => item.id == installationId).firstOrNull;
    if (selected == null || selected.id == _selected?.id) return;
    setState(() => _selected = selected);
    await _loadParticipation();
  }

  Future<bool> _confirm({required bool revoke}) async {
    final selected = _selected;
    if (selected == null) return false;
    return await showDialog<bool>(
          context: context,
          builder: (dialogContext) => AlertDialog(
            title: Text(revoke ? 'Remove app?' : 'Consent to add app?'),
            content: Text(
              revoke
                  ? 'Kaede will rekey this conversation. ${selected.applicationName} loses future access, but its operator may retain content already delivered.'
                  : 'Every person in this conversation must separately consent. Once everyone agrees, verified ${selected.applicationName} devices can decrypt future messages after the history floor. The app operator may retain anything it receives.',
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(dialogContext, false),
                child: const Text('Cancel'),
              ),
              FilledButton(
                style: revoke
                    ? FilledButton.styleFrom(
                        backgroundColor: Theme.of(context).colorScheme.error,
                        foregroundColor: Theme.of(context).colorScheme.onError,
                      )
                    : null,
                onPressed: () => Navigator.pop(dialogContext, true),
                child: Text(revoke ? 'Remove app' : 'Consent'),
              ),
            ],
          ),
        ) ??
        false;
  }

  Future<void> _consent() async {
    final selected = _selected;
    if (selected == null || _busy || !await _confirm(revoke: false)) return;
    setState(() {
      _busy = true;
      _error = null;
      _notice = null;
    });
    try {
      final participation =
          await widget.repository.consentToDmBotE2eeParticipation(
        channel: widget.channel.ref,
        application: selected.application,
      );
      if (mounted) {
        setState(() {
          _participation = participation;
          _notice = participation.active
              ? 'Everyone consented. App devices will join after the room rekeys.'
              : 'Your consent was recorded. The app remains blocked until everyone consents.';
        });
      }
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(
              error,
              summary: 'Could not record encrypted app consent',
            ));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _revoke() async {
    final selected = _selected;
    if (selected == null || _busy || !await _confirm(revoke: true)) return;
    setState(() {
      _busy = true;
      _error = null;
      _notice = null;
    });
    try {
      final participation =
          await widget.repository.revokeDmBotE2eeParticipation(
        channel: widget.channel.ref,
        application: selected.application,
      );
      if (mounted) {
        setState(() {
          _participation = participation;
          _notice = 'App access was revoked and a room rekey was staged.';
        });
      }
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(
              error,
              summary: 'Could not revoke encrypted app access',
            ));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final participation = _participation;
    return Scaffold(
      appBar: AppBar(title: const Text('Apps in this conversation')),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Card(
              child: Padding(
                padding: const EdgeInsets.all(14),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Icon(Icons.enhanced_encryption_outlined,
                        color: context.kaede.warning),
                    const SizedBox(width: 10),
                    const Expanded(
                      child: Text(
                        'An app becomes another cryptographic participant. Account authorization alone grants no room access. Revocation rotates keys, but cannot recall data the app already decrypted.',
                      ),
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 14),
            if (_installations.isNotEmpty)
              DropdownButtonFormField<String>(
                initialValue: _selected?.id,
                decoration: const InputDecoration(labelText: 'Authorized app'),
                items: [
                  for (final installation in _installations)
                    DropdownMenuItem(
                      value: installation.id,
                      child: Text(installation.applicationName),
                    ),
                ],
                onChanged: _loading || _busy ? null : _select,
              )
            else if (!_loading)
              const Card(
                child: ListTile(
                  leading: Icon(Icons.apps_outlined),
                  title: Text('No participant-capable app'),
                  subtitle: Text(
                    'Authorize one for private conversations through its reviewed Add App flow, then return here.',
                  ),
                ),
              ),
            const SizedBox(height: 14),
            if (_loading)
              const Center(child: CircularProgressIndicator())
            else if (participation != null) ...[
              Card(
                child: ListTile(
                  leading: Icon(
                    participation.active
                        ? Icons.verified_user_outlined
                        : participation.revoked
                            ? Icons.block_outlined
                            : Icons.hourglass_top_rounded,
                    color: participation.active
                        ? context.kaede.mint
                        : participation.revoked
                            ? context.kaede.danger
                            : context.kaede.warning,
                  ),
                  title: Text('Consent ${participation.consentState}'),
                  subtitle: Text(
                    participation.historyFloorMessageRef == null
                        ? 'No access to messages sent before full consent'
                        : 'No app history before ${participation.historyFloorMessageRef!.wire}',
                  ),
                ),
              ),
              for (final participant in participation.participants)
                ListTile(
                  leading: Icon(
                    participant.consented
                        ? Icons.check_circle_outline_rounded
                        : Icons.schedule_rounded,
                    color: participant.consented
                        ? context.kaede.mint
                        : context.kaede.warning,
                  ),
                  title: Text(participant.userRef.wire),
                  subtitle: Text(participant.consented
                      ? 'Consented'
                      : 'Waiting for consent'),
                ),
              if (participation.devices.isNotEmpty) ...[
                const SizedBox(height: 8),
                Text('VERIFIED APP DEVICES',
                    style: Theme.of(context).textTheme.labelSmall),
                for (final device in participation.devices)
                  ListTile(
                    leading: const Icon(Icons.key_rounded),
                    title: Text(device.deviceId),
                    subtitle:
                        Text('${device.status} · epoch ${device.joinedEpoch}'),
                  ),
              ],
            ],
            if (_error case final error?) ...[
              const SizedBox(height: 10),
              Text(error, style: TextStyle(color: context.kaede.danger)),
            ],
            if (_notice case final notice?) ...[
              const SizedBox(height: 10),
              Text(notice, style: TextStyle(color: context.kaede.mint)),
            ],
            const SizedBox(height: 18),
            if (_selected != null && !_loading)
              participation != null && !participation.revoked
                  ? FilledButton.tonalIcon(
                      onPressed: _busy ? null : _revoke,
                      icon: const Icon(Icons.person_remove_outlined),
                      label: Text(_busy ? 'Removing…' : 'Remove app and rekey'),
                    )
                  : FilledButton.icon(
                      onPressed: _busy ? null : _consent,
                      icon: const Icon(Icons.person_add_alt_1_rounded),
                      label: Text(_busy ? 'Recording…' : 'Consent to add'),
                    ),
          ],
        ),
      ),
    );
  }
}
