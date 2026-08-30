import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/domain/models.dart';

Map<String, Object?> _user({String id = '2'}) => <String, Object?>{
      'id': id,
      'origin_domain': 'two.example',
      'username': 'alice',
      'display_name': 'Alice',
      'avatar_hash': null,
      'bot': false,
    };

Map<String, Object?> _metadata({
  String type = 'command',
  String commandType = 'chat_input',
}) =>
    <String, Object?>{
      'id': '1',
      'origin_domain': 'one.example',
      'interaction_ref': '1@one.example',
      'type': type,
      'user': _user(),
      'user_ref': '2@two.example',
      'application_ref': '3@three.example',
      'integration_type': 'guild_install',
      'authorizing_integration_owners': <String, Object?>{
        'guild_install': '4@one.example',
      },
      if (type == 'command') ...<String, Object?>{
        'command_name': 'ship',
        'command_type': commandType,
      },
    };

void main() {
  test('parses durable command attribution and uses Discord slash syntax', () {
    final metadata = KaedeInteractionMetadata.tryFromJson(_metadata());
    expect(metadata, isNotNull);
    expect(
      interactionAttributionText(metadata, deleted: false),
      'Alice used /ship',
    );

    final context = _metadata(commandType: 'message')
      ..addAll(<String, Object?>{
        'target_message_id': '8',
        'target_message_domain': 'two.example',
        'target_message_ref': '8@two.example',
      });
    expect(
      interactionAttributionText(
        KaedeInteractionMetadata.tryFromJson(context),
        deleted: false,
      ),
      'Alice used ship',
    );
  });

  test('rejects inconsistent federated target and actor identities', () {
    final badTarget = _metadata(commandType: 'message')
      ..addAll(<String, Object?>{
        'target_message_id': '8',
        'target_message_domain': 'two.example',
        'target_message_ref': '9@two.example',
      });
    expect(KaedeInteractionMetadata.tryFromJson(badTarget), isNull);

    final badActor = _metadata()..['user_ref'] = '9@two.example';
    expect(KaedeInteractionMetadata.tryFromJson(badActor), isNull);
  });

  test('parses bounded modal lineage and hides deleted attribution', () {
    final modal = _metadata(type: 'modal_submit')
      ..['triggering_interaction_metadata'] = _metadata();
    final metadata = KaedeInteractionMetadata.tryFromJson(modal);
    expect(metadata?.triggeringInteractionMetadata, isNotNull);
    expect(
      interactionAttributionText(metadata, deleted: false),
      'Alice submitted a form',
    );
    expect(interactionAttributionText(metadata, deleted: true), isNull);

    final tooDeep = _metadata(type: 'modal_submit')
      ..['triggering_interaction_metadata'] = modal;
    expect(KaedeInteractionMetadata.tryFromJson(tooDeep), isNull);
  });
}
