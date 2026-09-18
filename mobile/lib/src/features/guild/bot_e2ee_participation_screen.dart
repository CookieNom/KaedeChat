import 'dart:async';
import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/bot_e2ee_participation.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/shared/action_feedback.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';
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
            summary: L10n.of(context)
                .ui_could_not_load_encrypted_app_access_ced90634,
          );
          _loading = false;
        });
      }
    } on Object catch (error) {
      if (!mounted || _channel?.ref != channel.ref) return;
      setState(() {
        _error = userFacingError(
          error,
          summary:
              L10n.of(context).ui_could_not_load_encrypted_app_access_ced90634,
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
      title: L10n.of(context).ui_allow_value0_in_value1_c1b08474(
          (widget.applicationName).toString(),
          (channel.name ?? 'channel').toString()),
      warning:
          'Verified app devices will join this MLS room after a rekey and can decrypt future messages after each displayed history floor. Revocation stops future access but cannot erase content the app already received.',
      action: 'Allow access',
    );
    if (reason == null || !mounted) return;
    setState(() {
      _busy = true;
      _error = null;
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
          showActionFeedback(
              context,
              L10n.of(context)
                  .ui_access_is_staged_pending_devices_activate_aft_9a102147);
        });
      }
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(
              error,
              summary: L10n.of(context)
                  .ui_could_not_grant_encrypted_channel_access_bfd302a4,
            ));
        showActionFeedback(context, _error!, error: true);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _revoke() async {
    final channel = _channel;
    if (channel == null || _busy) return;
    final reason = await _confirmWithReason(
      title: L10n.of(context).ui_revoke_value0_from_value1_0401de4e(
          (widget.applicationName).toString(),
          (channel.name ?? 'channel').toString()),
      warning:
          'Kaede will rekey the room. The app loses future access, but messages its devices already decrypted cannot be recalled.',
      action: 'Revoke access',
      destructive: true,
    );
    if (reason == null || !mounted) return;
    setState(() {
      _busy = true;
      _error = null;
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
          showActionFeedback(
              context,
              L10n.of(context)
                  .ui_access_was_revoked_and_the_room_rekey_was_sta_28d7d718);
        });
      }
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(
              error,
              summary: L10n.of(context)
                  .ui_could_not_revoke_encrypted_channel_access_46694806,
            ));
        showActionFeedback(context, _error!, error: true);
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
                decoration: InputDecoration(
                    labelText:
                        L10n.of(context).ui_audit_reason_optional_2fa64eeb),
              ),
            ],
          ),
          actions: [
            ActionButton(
              kind: ActionButtonKind.text,
              onPressed: () => Navigator.pop(dialogContext),
              child: Text(L10n.of(context).ui_cancel_35afca3b),
            ),
            destructive
                ? ActionButton(
                    style: FilledButton.styleFrom(
                      backgroundColor: Theme.of(context).colorScheme.error,
                      foregroundColor: Theme.of(context).colorScheme.onError,
                    ),
                    onPressed: () => Navigator.pop(dialogContext, reason.text),
                    child: Text(action),
                  )
                : ActionButton(
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
        appBar: AppBar(
            title: Text(L10n.of(context).ui_encrypted_channel_access_45930d32)),
        body: SafeArea(
          child: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              Text(
                widget.applicationName,
                style: Theme.of(context).textTheme.headlineSmall,
              ),
              const SizedBox(height: 6),
              Text(
                L10n.of(context)
                    .ui_participant_mode_lets_verified_app_devices_jo_16dcdfd2,
              ),
              const SizedBox(height: 16),
              if (_channels.isEmpty)
                Card(
                  child: ListTile(
                    leading: Icon(Icons.lock_outline_rounded),
                    title: Text(
                        L10n.of(context).ui_no_encrypted_channels_7dbbddd2),
                    subtitle: Text(
                      L10n.of(context)
                          .ui_create_or_enable_an_end_to_end_encrypted_chan_edb4aad4,
                    ),
                  ),
                )
              else ...[
                DropdownButtonFormField<KaedeChannel>(
                  initialValue: _channel,
                  decoration: InputDecoration(
                      labelText: L10n.of(context).ui_channel_655d8a44),
                  items: [
                    for (final channel in _channels)
                      DropdownMenuItem(
                        value: channel,
                        child: Text(L10n.of(context).ui_value0_ea2f080f(
                            (channel.name ?? 'encrypted-channel').toString())),
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
                          L10n.of(context)
                              .ui_value0_value1_consent_generation_value2_joine_a78bcb23(
                                  (device.deviceId).toString(),
                                  (device.historyNotice).toString(),
                                  (device.consentGeneration).toString(),
                                  (device.joinedEpoch).toString()),
                        ),
                        isThreeLine: true,
                      ),
                    )
                else
                  Card(
                    child: ListTile(
                      leading: Icon(Icons.no_encryption_outlined),
                      title: Text(L10n.of(context)
                          .ui_not_allowed_in_this_channel_dc4a7130),
                    ),
                  ),
                if (_error case final error?) ...[
                  const SizedBox(height: 10),
                  Text(error, style: TextStyle(color: context.kaede.danger)),
                ],
                if (widget.canManage) ...[
                  const SizedBox(height: 14),
                  if (_participation?.active == true)
                    ActionButton(
                      kind: ActionButtonKind.outlined,
                      onPressed: _busy ? null : _revoke,
                      icon: const Icon(Icons.link_off_rounded),
                      label: Text(_busy
                          ? L10n.of(context).ui_revoking_e0fcd0a0
                          : L10n.of(context).ui_revoke_access_0dd14817),
                    )
                  else
                    ActionButton(
                      onPressed: _busy || _loading ? null : _grant,
                      icon: const Icon(Icons.enhanced_encryption_outlined),
                      label: Text(_busy
                          ? L10n.of(context).ui_granting_803784ad
                          : L10n.of(context).ui_allow_in_channel_c1cb6472),
                    ),
                ],
                const SizedBox(height: 14),
                Text(
                  L10n.of(context)
                      .ui_the_app_receives_plaintext_only_on_verified_p_24743e64,
                  style: TextStyle(color: context.kaede.muted, fontSize: 12.5),
                ),
              ],
            ],
          ),
        ),
      );
}
