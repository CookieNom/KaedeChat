import 'package:kaede_mobile/src/core/network_json.dart';
import 'package:kaede_mobile/src/core/refs.dart';

final class BotE2eeParticipationDevice {
  const BotE2eeParticipationDevice({
    required this.deviceId,
    required this.status,
    required this.consentGeneration,
    required this.joinedEpoch,
    required this.historyFloorMessageRef,
  });

  factory BotE2eeParticipationDevice.fromJson(Map<String, Object?> json) =>
      BotE2eeParticipationDevice(
        deviceId: '${json['device_id']}',
        status: '${json['status']}',
        consentGeneration: '${json['consent_generation']}',
        joinedEpoch: '${json['joined_epoch']}',
        historyFloorMessageRef: json['history_floor_message_ref'] == null
            ? null
            : EntityRef.fromJson(json['history_floor_message_ref']),
      );

  final String deviceId;
  final String status;
  final String consentGeneration;
  final String joinedEpoch;
  final EntityRef? historyFloorMessageRef;

  String get historyNotice => historyFloorMessageRef == null
      ? 'No access to messages sent before consent'
      : 'No access before message ${historyFloorMessageRef!.wire}';
}

final class BotE2eeParticipation {
  const BotE2eeParticipation({
    required this.applicationRef,
    required this.channelRef,
    required this.mode,
    required this.devices,
  });

  factory BotE2eeParticipation.fromJson(Map<String, Object?> json) =>
      BotE2eeParticipation(
        applicationRef: EntityRef.fromJson(json['application_ref']),
        channelRef: EntityRef.fromJson(json['channel_ref']),
        mode: '${json['e2ee_mode']}',
        devices: strictNetworkObjectList(
          json['devices'],
          label: 'Bot E2EE devices',
        )
            .map((item) => BotE2eeParticipationDevice.fromJson(
                  item,
                ))
            .toList(growable: false),
      );

  final EntityRef applicationRef;
  final EntityRef channelRef;
  final String mode;
  final List<BotE2eeParticipationDevice> devices;

  bool get active => devices.any((device) => device.status != 'revoked');
}

final class DmBotE2eeParticipantConsent {
  const DmBotE2eeParticipantConsent({
    required this.userRef,
    required this.consented,
  });

  factory DmBotE2eeParticipantConsent.fromJson(Map<String, Object?> json) =>
      DmBotE2eeParticipantConsent(
        userRef: EntityRef.fromJson(json['user_ref']),
        consented: json['consented'] == true,
      );

  final EntityRef userRef;
  final bool consented;
}

final class DmBotE2eeDevice {
  const DmBotE2eeDevice({
    required this.deviceId,
    required this.status,
    required this.joinedEpoch,
  });

  factory DmBotE2eeDevice.fromJson(Map<String, Object?> json) =>
      DmBotE2eeDevice(
        deviceId: '${json['device_id']}',
        status: '${json['status']}',
        joinedEpoch: '${json['joined_epoch']}',
      );

  final String deviceId;
  final String status;
  final String joinedEpoch;
}

final class DmBotE2eeParticipation {
  const DmBotE2eeParticipation({
    required this.applicationRef,
    required this.channelRef,
    required this.consentState,
    required this.consentGeneration,
    required this.historyFloorMessageRef,
    required this.participants,
    required this.devices,
    required this.encryptionPolicy,
  });

  factory DmBotE2eeParticipation.fromJson(Map<String, Object?> json) =>
      DmBotE2eeParticipation(
        applicationRef: EntityRef.fromJson(json['application_ref']),
        channelRef: EntityRef.fromJson(json['channel_ref']),
        consentState: '${json['consent_state']}',
        consentGeneration: '${json['consent_generation']}',
        historyFloorMessageRef: json['history_floor_message_ref'] == null
            ? null
            : EntityRef.fromJson(json['history_floor_message_ref']),
        participants: strictNetworkObjectList(
          json['participants'],
          label: 'DM bot E2EE participants',
        )
            .map((item) => DmBotE2eeParticipantConsent.fromJson(
                  item,
                ))
            .toList(growable: false),
        devices: strictNetworkObjectList(
          json['devices'],
          label: 'DM bot E2EE devices',
        )
            .map((item) => DmBotE2eeDevice.fromJson(
                  item,
                ))
            .toList(growable: false),
        encryptionPolicy: json['encryption_policy'] is Map
            ? Map<String, Object?>.from(
                json['encryption_policy']! as Map<Object?, Object?>,
              )
            : const <String, Object?>{},
      );

  final EntityRef applicationRef;
  final EntityRef channelRef;
  final String consentState;
  final String consentGeneration;
  final EntityRef? historyFloorMessageRef;
  final List<DmBotE2eeParticipantConsent> participants;
  final List<DmBotE2eeDevice> devices;
  final Map<String, Object?> encryptionPolicy;

  bool get active => consentState == 'active';
  bool get revoked => consentState == 'revoked';
  bool get everyoneConsented =>
      participants.isNotEmpty && participants.every((item) => item.consented);
}
