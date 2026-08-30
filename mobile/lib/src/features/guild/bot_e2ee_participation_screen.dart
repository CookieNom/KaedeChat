import 'dart:async';

import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/bot_e2ee_participation.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

/// Discord-located per-channel participant consent under an installed app's
/// Server Settings > Integrations detail.
final class BotE2eeParticipationScreen extends StatefulWidget {
  const BotE2eeParticipationScreen({
    super.key,
    required this.guild,
    required this.application,
    required this.applicationName,
    required this.repository,
    required this.canManage,
  });

  final KaedeGuild guild;
  final EntityRef application;
  final String applicationName;
  final KaedeRepository repository;
  final bool canManage;

  @override
  State<BotE2eeParticipationScreen> createState() =>
      _BotE2eeParticipationScreenState();
}

final class _BotE2eeParticipationScreenState
    extends State<BotE2eeParticipationScreen> {
  late final List<KaedeChannel> _channels = widget.guild.channels
      .where((channel) => channel.encryptionMode == 'e2ee')
      .toList(growable: false);
  KaedeChannel? _channel;
  BotE2eeParticipation? _participation;
  var _loading = false;
  var _busy = false;
  String? _error;
  String? _notice;

  @override
  void initState() {
    super.initState();
    _channel = _channels.firstOrNull;
    if (_channel != null) unawaited(_load());
  }

  Future<void> _load() async {
    final channel = _channel;
    if (channel == null) return;
    setState(() {
      _loading = true;
      _error = null;
      _notice = null;
    });
    try {
      final participation = await widget.repository.botE2eeParticipation(
        guild: widget.guild.ref,
        channel: channel.ref,
        application: widget.application,
      );
      if (mounted && _channel?.ref == channel.ref) {
        setState(() {
          _participation = participation;
          _loading = false;
        });
      }
    } on KaedeException catch (error) {
      if (!mounted || _channel?.ref != channel.ref) return;
      if (error.code == 'BOT_E2EE_PARTICIPATION_NOT_FOUND') {
        setState(() {
          _participation = null;
          _loading = false;
        });
      } else {
        setState(() {
          _error = userFacingError(
            error,
            summary: 'Could not load encrypted app access',
          );
          _loading = false;
        });
      }
    } on Object catch (error) {
      if (!mounted || _channel?.ref != channel.ref) return;
      setState(() {
        _error = userFacingError(
          error,
          summary: 'Could not load encrypted app access',
        );
        _loading = false;
      });
    }
  }

  Future<void> _changeChannel(KaedeChannel? channel) async {
    if (channel == null || channel.ref == _channel?.ref) return;
    setState(() {
      _channel = channel;
      _participation = null;
    });
    await _load();
  }

  Future<void> _grant() async {
    final channel = _channel;
    if (channel == null || _busy) return;
    final reason = await _confirmWithReason(
      title:
          'Allow ${widget.applicationName} in #${channel.name ?? 'channel'}?',
      warning:
          'Verified app devices will join this MLS room after a rekey and can decrypt future messages after each displayed history floor. Revocation stops future access but cannot erase content the app already received.',
      action: 'Allow access',
    );
    if (reason == null || !mounted) return;
    setState(() {
      _busy = true;
      _error = null;
      _notice = null;
    });
    try {
      final participation = await widget.repository.grantBotE2eeParticipation(
        guild: widget.guild.ref,
        channel: channel.ref,
        application: widget.application,
        reason: reason,
      );
      if (mounted) {
        setState(() {
          _participation = participation;
          _notice =
              'Access is staged. Pending devices activate after the room rekeys.';
        });
      }
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(
              error,
              summary: 'Could not grant encrypted channel access',
            ));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _revoke() async {
    final channel = _channel;
    if (channel == null || _busy) return;
    final reason = await _confirmWithReason(
      title:
          'Revoke ${widget.applicationName} from #${channel.name ?? 'channel'}?',
      warning:
          'Kaede will rekey the room. The app loses future access, but messages its devices already decrypted cannot be recalled.',
      action: 'Revoke access',
      destructive: true,
    );
    if (reason == null || !mounted) return;
    setState(() {
      _busy = true;
      _error = null;
      _notice = null;
    });
    try {
      await widget.repository.revokeBotE2eeParticipation(
        guild: widget.guild.ref,
        channel: channel.ref,
        application: widget.application,
        reason: reason,
      );
      if (mounted) {
        setState(() {
          _participation = null;
          _notice = 'Access was revoked and the room rekey was staged.';
        });
      }
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(
              error,
              summary: 'Could not revoke encrypted channel access',
            ));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<String?> _confirmWithReason({
    required String title,
    required String warning,
    required String action,
    bool destructive = false,
  }) async {
    final reason = TextEditingController();
    try {
      return await showDialog<String>(
        context: context,
        builder: (dialogContext) => AlertDialog(
          title: Text(title),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(warning),
              const SizedBox(height: 14),
              TextField(
                controller: reason,
                maxLength: 512,
                decoration:
                    const InputDecoration(labelText: 'Audit reason (optional)'),
              ),
            ],
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext),
              child: const Text('Cancel'),
            ),
            destructive
                ? FilledButton(
                    style: FilledButton.styleFrom(
                      backgroundColor: Theme.of(context).colorScheme.error,
                      foregroundColor: Theme.of(context).colorScheme.onError,
                    ),
                    onPressed: () => Navigator.pop(dialogContext, reason.text),
                    child: Text(action),
                  )
                : FilledButton(
                    onPressed: () => Navigator.pop(dialogContext, reason.text),
                    child: Text(action),
                  ),
          ],
        ),
      );
    } finally {
      reason.dispose();
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: const Text('Encrypted channel access')),
        body: SafeArea(
          child: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              Text(
                widget.applicationName,
                style: Theme.of(context).textTheme.headlineSmall,
              ),
              const SizedBox(height: 6),
              const Text(
                'Participant mode lets verified app devices join a channel’s MLS room. Consent is per channel and always triggers a rekey.',
              ),
              const SizedBox(height: 16),
              if (_channels.isEmpty)
                const Card(
                  child: ListTile(
                    leading: Icon(Icons.lock_outline_rounded),
                    title: Text('No encrypted channels'),
                    subtitle: Text(
                      'Create or enable an end-to-end encrypted channel before granting participant access.',
                    ),
                  ),
                )
              else ...[
                DropdownButtonFormField<KaedeChannel>(
                  initialValue: _channel,
                  decoration: const InputDecoration(labelText: 'Channel'),
                  items: [
                    for (final channel in _channels)
                      DropdownMenuItem(
                        value: channel,
                        child: Text('#${channel.name ?? 'encrypted-channel'}'),
                      ),
                  ],
                  onChanged: _loading || _busy ? null : _changeChannel,
                ),
                const SizedBox(height: 14),
                if (_loading)
                  const Center(child: CircularProgressIndicator())
                else if (_participation?.devices.isNotEmpty == true)
                  for (final device in _participation!.devices)
                    Card(
                      child: ListTile(
                        leading: Icon(
                          device.status == 'active'
                              ? Icons.verified_user_outlined
                              : Icons.sync_lock_outlined,
                          color: device.status == 'active'
                              ? context.kaede.mint
                              : context.kaede.warning,
                        ),
                        title: Text(device.status),
                        subtitle: Text(
                          '${device.deviceId}\n${device.historyNotice} · consent generation ${device.consentGeneration} · joined epoch ${device.joinedEpoch}',
                        ),
                        isThreeLine: true,
                      ),
                    )
                else
                  const Card(
                    child: ListTile(
                      leading: Icon(Icons.no_encryption_outlined),
                      title: Text('Not allowed in this channel'),
                    ),
                  ),
                if (_error case final error?) ...[
                  const SizedBox(height: 10),
                  Text(error, style: TextStyle(color: context.kaede.danger)),
                ],
                if (_notice case final notice?) ...[
                  const SizedBox(height: 10),
                  Text(notice, style: TextStyle(color: context.kaede.mint)),
                ],
                if (widget.canManage) ...[
                  const SizedBox(height: 14),
                  if (_participation?.active == true)
                    OutlinedButton.icon(
                      onPressed: _busy ? null : _revoke,
                      icon: const Icon(Icons.link_off_rounded),
                      label: Text(_busy ? 'Revoking…' : 'Revoke access'),
                    )
                  else
                    FilledButton.icon(
                      onPressed: _busy || _loading ? null : _grant,
                      icon: const Icon(Icons.enhanced_encryption_outlined),
                      label: Text(_busy ? 'Granting…' : 'Allow in channel'),
                    ),
                ],
                const SizedBox(height: 14),
                Text(
                  'The app receives plaintext only on verified participant devices. Revocation prevents new decryptions; it cannot recall content already delivered or bypass the displayed history floor.',
                  style: TextStyle(color: context.kaede.muted, fontSize: 12.5),
                ),
              ],
            ],
          ),
        ),
      );
}
