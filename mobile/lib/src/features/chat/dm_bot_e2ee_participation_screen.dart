import 'dart:async';
import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/domain/application_installations.dart';
import 'package:kaede_mobile/src/domain/bot_e2ee_participation.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/shared/action_feedback.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';
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

  @override
  void initState() {
    super.initState();
    unawaited(_loadInstallations());
  }

  Future<void> _loadInstallations() async {
    setState(() {
      _loading = true;
      _error = null;
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
          summary: L10n.of(context)
              .ui_could_not_load_participant_capable_apps_23099607,
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
            summary: L10n.of(context)
                .ui_could_not_load_encrypted_app_consent_a2fb8234,
          );
        });
      }
    } on Object catch (error) {
      if (!mounted || _selected?.id != selected.id) return;
      setState(() {
        _loading = false;
        _error = userFacingError(
          error,
          summary:
              L10n.of(context).ui_could_not_load_encrypted_app_consent_a2fb8234,
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
            title: Text(revoke
                ? L10n.of(context).ui_remove_app_9544ee87
                : L10n.of(context).ui_consent_to_add_app_3fed44cf),
            content: Text(
              revoke
                  ? L10n.of(context)
                      .ui_kaede_will_rekey_this_conversation_value0_los_35a6dafc(
                          (selected.applicationName).toString())
                  : L10n.of(context)
                      .ui_every_person_in_this_conversation_must_separa_2e783b07(
                          (selected.applicationName).toString()),
            ),
            actions: [
              ActionButton(
                kind: ActionButtonKind.text,
                onPressed: () => Navigator.pop(dialogContext, false),
                child: Text(L10n.of(context).ui_cancel_35afca3b),
              ),
              ActionButton(
                style: revoke
                    ? FilledButton.styleFrom(
                        backgroundColor: Theme.of(context).colorScheme.error,
                        foregroundColor: Theme.of(context).colorScheme.onError,
                      )
                    : null,
                onPressed: () => Navigator.pop(dialogContext, true),
                child: Text(revoke
                    ? L10n.of(context).ui_remove_app_fd5c4782
                    : L10n.of(context).ui_consent_fddd385d),
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
          showActionFeedback(
              context,
              participation.active
                  ? L10n.of(context)
                      .ui_everyone_consented_app_devices_will_join_afte_e81be7d8
                  : L10n.of(context)
                      .ui_your_consent_was_recorded_the_app_remains_blo_1736b614);
        });
      }
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(
              error,
              summary: L10n.of(context)
                  .ui_could_not_record_encrypted_app_consent_2ee98a85,
            ));
        showActionFeedback(context, _error!, error: true);
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
          showActionFeedback(
              context,
              L10n.of(context)
                  .ui_app_access_was_revoked_and_a_room_rekey_was_s_8aefb0a7);
        });
      }
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(
              error,
              summary: L10n.of(context)
                  .ui_could_not_revoke_encrypted_app_access_01125b9e,
            ));
        showActionFeedback(context, _error!, error: true);
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final participation = _participation;
    return Scaffold(
      appBar: AppBar(
          title: Text(L10n.of(context).ui_apps_in_this_conversation_9f084fd7)),
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
                    Expanded(
                      child: Text(
                        L10n.of(context)
                            .ui_an_app_becomes_another_cryptographic_particip_c845b147,
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
                decoration: InputDecoration(
                    labelText: L10n.of(context).ui_authorized_app_c2371e67),
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
              Card(
                child: ListTile(
                  leading: Icon(Icons.apps_outlined),
                  title: Text(
                      L10n.of(context).ui_no_participant_capable_app_6281f549),
                  subtitle: Text(
                    L10n.of(context)
                        .ui_authorize_one_for_private_conversations_throu_cce245e7,
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
                  title: Text(L10n.of(context).ui_consent_value0_937d13ae(
                      (participation.consentState).toString())),
                  subtitle: Text(
                    participation.historyFloorMessageRef == null
                        ? L10n.of(context)
                            .ui_no_access_to_messages_sent_before_full_consen_f6c7f545
                        : L10n.of(context)
                            .ui_no_app_history_before_value0_f14a0781(
                                (participation.historyFloorMessageRef!.wire)
                                    .toString()),
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
                      ? L10n.of(context).ui_consented_70f6f7a4
                      : L10n.of(context).ui_waiting_for_consent_d94d101d),
                ),
              if (participation.devices.isNotEmpty) ...[
                const SizedBox(height: 8),
                Text(L10n.of(context).ui_verified_app_devices_6f81ff49,
                    style: Theme.of(context).textTheme.labelSmall),
                for (final device in participation.devices)
                  ListTile(
                    leading: const Icon(Icons.key_rounded),
                    title: Text(device.deviceId),
                    subtitle: Text(L10n.of(context)
                        .ui_value0_epoch_value1_36844dba(
                            (device.status).toString(),
                            (device.joinedEpoch).toString())),
                  ),
              ],
            ],
            if (_error case final error?) ...[
              const SizedBox(height: 10),
              Text(error, style: TextStyle(color: context.kaede.danger)),
            ],
            const SizedBox(height: 18),
            if (_selected != null && !_loading)
              participation != null && !participation.revoked
                  ? ActionButton(
                      kind: ActionButtonKind.tonal,
                      onPressed: _busy ? null : _revoke,
                      icon: const Icon(Icons.person_remove_outlined),
                      label: Text(_busy
                          ? L10n.of(context).ui_removing_be3af276
                          : L10n.of(context).ui_remove_app_and_rekey_a691fe77),
                    )
                  : ActionButton(
                      onPressed: _busy ? null : _consent,
                      icon: const Icon(Icons.person_add_alt_1_rounded),
                      label: Text(_busy
                          ? L10n.of(context).ui_recording_44af53a8
                          : L10n.of(context).ui_consent_to_add_684f79a5),
                    ),
          ],
        ),
      ),
    );
  }
}
