import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/stage_instances.dart';

extension StageInstancesRepository on KaedeRepository {
  Future<StageInstance> stageInstance(EntityRef channel) async =>
      StageInstance.fromJson(
        await api.getJson('/api/v1/stage-instances/${channel.wire}'),
      );

  Future<StageInstance> createStageInstance(
    EntityRef channel,
    String topic, {
    EntityRef? scheduledEvent,
    bool sendStartNotification = false,
  }) async =>
      StageInstance.fromJson(await api.sendJson(
        'POST',
        '/api/v1/stage-instances',
        data: <String, Object?>{
          'channel_id': channel.wire,
          'topic': topic,
          'privacy_level': 2,
          'send_start_notification': sendStartNotification,
          if (scheduledEvent != null)
            'guild_scheduled_event_id': scheduledEvent.wire,
        },
      ));

  Future<StageInstance> updateStageInstance(
    EntityRef channel,
    String topic,
  ) async =>
      StageInstance.fromJson(await api.sendJson(
        'PATCH',
        '/api/v1/stage-instances/${channel.wire}',
        data: <String, Object?>{'topic': topic},
      ));

  Future<void> deleteStageInstance(EntityRef channel) => api.sendJson(
        'DELETE',
        '/api/v1/stage-instances/${channel.wire}',
      );

  Future<Map<String, Object?>> updateMyStageVoiceState(
    EntityRef guild,
    Map<String, Object?> patch,
  ) =>
      api.sendJson(
        'PATCH',
        '/api/v1/guilds/${guild.wire}/voice-states/@me',
        data: patch,
      );

  Future<Map<String, Object?>> updateStageVoiceState(
    EntityRef guild,
    EntityRef user, {
    required bool suppress,
  }) =>
      api.sendJson(
        'PATCH',
        '/api/v1/guilds/${guild.wire}/voice-states/${user.wire}',
        data: <String, Object?>{'suppress': suppress},
      );
}
