import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/e2ee/client.dart';

void main() {
  final channel = KaedeChannel(
    ref: EntityRef.parse('20@guild.example'),
    guildRef: EntityRef.parse('10@guild.example'),
    type: ChannelType.text,
    position: 0,
    permissions: BigInt.zero,
    encryptionMode: 'e2ee',
    encryptionState: 'active',
    encryptionPolicyGeneration: 3,
    encryptionProtocol: mlsProtocol,
    encryptionSuite: mlsSuite,
    encryptionGroupId: 'Z2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2c',
    encryptionEpoch: 7,
  );

  test('mobile encrypted interactions use recursively sorted compact JSON', () {
    final bytes = mobileCanonicalInteractionJson(<String, Object?>{
      'z': <Object?>[
        <String, Object?>{'b': 2, 'a': 1},
      ],
      'a': <String, Object?>{'d': 4, 'c': 3},
    });

    expect(
      utf8.decode(bytes),
      '{"a":{"c":3,"d":4},"z":[{"a":1,"b":2}]}',
    );
  });

  test('mobile interaction context binds effective user-install DM authority',
      () {
    final context = mobileInteractionAuthenticatedContext(
      channel,
      invoker: EntityRef.parse('40@users.example'),
      senderDeviceId: 'ked_${List.filled(43, 'a').join()}',
      application: EntityRef.parse('30@apps.example'),
      integrationType: 'user_install',
      interactionContext: 'bot_dm',
      interactionType: 'command',
      commandId: '91',
      commandName: 'secure',
      commandType: 'chat_input',
      attachmentIds: const <String>['100', '9'],
    );

    expect(context['attachment_ids'], const <String>['9', '100']);
    expect(context['application_ref'], '30@apps.example');
    expect(context['channel_ref'], '20@guild.example');
    expect(context['integration_type'], 'user_install');
    expect(context['context'], 'bot_dm');
    expect(context['invoker_ref'], '40@users.example');
    expect(context['command_id'], '91');
    expect(context['epoch'], '7');
    expect(context['policy_generation'], '3');
  });

  test('mobile encrypted commands require a stable command identity', () {
    expect(
      () => mobileInteractionAuthenticatedContext(
        channel,
        invoker: EntityRef.parse('40@users.example'),
        senderDeviceId: 'ked_${List.filled(43, 'a').join()}',
        application: EntityRef.parse('30@apps.example'),
        integrationType: 'guild_install',
        interactionContext: 'guild',
        interactionType: 'command',
      ),
      throwsFormatException,
    );
  });

  test('mobile encrypted bot-DM commands bind the capability integration', () {
    final context = mobileInteractionAuthenticatedContext(
      channel,
      invoker: EntityRef.parse('40@users.example'),
      senderDeviceId: 'ked_${List.filled(43, 'a').join()}',
      application: EntityRef.parse('30@apps.example'),
      integrationType: 'dm_capability',
      interactionContext: 'bot_dm',
      interactionType: 'command',
      commandId: '91',
      commandName: 'secure',
      commandType: 'chat_input',
    );

    expect(context['integration_type'], 'dm_capability');
    expect(context['context'], 'bot_dm');
  });

  test('mobile matches the shared interaction AAD vectors', () async {
    final fixture = Map<String, Object?>.from(
      jsonDecode(
        await File('../frontend/static/protocol/interaction-aad-v1.json')
            .readAsString(),
      ) as Map,
    );
    final rawChannel = Map<String, Object?>.from(fixture['channel']! as Map);
    final fixtureChannel = KaedeChannel(
      ref: EntityRef.parse(
        '${rawChannel['id']}@${rawChannel['origin_domain']}',
      ),
      type: ChannelType.text,
      position: 0,
      permissions: BigInt.zero,
      encryptionMode: 'e2ee',
      encryptionState: 'active',
      encryptionPolicyGeneration:
          int.parse('${rawChannel['encryption_policy_generation']}'),
      encryptionProtocol: mlsProtocol,
      encryptionSuite: mlsSuite,
      encryptionGroupId: '${rawChannel['encryption_group_id']}',
      encryptionEpoch: int.parse('${rawChannel['encryption_epoch']}'),
    );
    final invoker = EntityRef.parse('${fixture['invoker_ref']}');
    final senderDeviceId = '${fixture['sender_device_id']}';

    for (final rawVector in fixture['vectors']! as List) {
      final vector = Map<String, Object?>.from(rawVector as Map);
      final input = Map<String, Object?>.from(vector['input']! as Map);
      EntityRef? optionalRef(String key) =>
          input[key] == null ? null : EntityRef.parse(input[key]! as String);
      final context = mobileInteractionAuthenticatedContext(
        fixtureChannel,
        invoker: invoker,
        senderDeviceId: senderDeviceId,
        application: EntityRef.parse(input['applicationRef']! as String),
        integrationType: input['integrationType']! as String,
        interactionContext: input['interactionContext']! as String,
        interactionType: input['interactionType']! as String,
        commandId: input['commandId'] as String?,
        commandName: input['commandName'] as String?,
        commandType: input['commandType'] as String?,
        componentType: input['componentType'],
        customId: input['customId'] as String?,
        message: optionalRef('messageRef'),
        responseId: input['responseId'],
        target: optionalRef('targetRef'),
        viewVersion: input['viewVersion'],
        autocompleteGeneration: input['autocompleteGeneration'],
        focusedOption: input['focusedOption'] as String?,
      );
      final expectedContext = Map<String, Object?>.from(
        vector['context']! as Map,
      );
      expect(context, expectedContext, reason: '${vector['name']} context');
      final aad = mobileCanonicalInteractionJson(<String, Object?>{
        'context': context,
        'purpose': 'kaede.interaction.v1',
      });
      expect(
        base64Url.encode(aad).replaceAll('=', ''),
        vector['aad_base64url'],
        reason: '${vector['name']} AAD',
      );
    }
  });

  test('repository wire strips every plaintext interaction collection', () {
    final wire = interactionRequestData(
      <String, Object?>{
        'application_ref': '30@apps.example',
        'command_name': 'secure',
        'options': <String, Object?>{'query': 'private'},
      },
      encryptedPayload: const <String, Object?>{'ciphertext': 'opaque'},
      attachmentIds: const <String>['9223372036854775807'],
    );

    expect(wire['options'], isEmpty);
    expect(wire['values'], isEmpty);
    expect(wire['components'], isEmpty);
    expect(wire['attachment_ids'], const <String>['9223372036854775807']);
    expect(wire['encrypted_payload'], const {'ciphertext': 'opaque'});
  });

  test('mobile matches the shared interaction response AAD vectors', () async {
    final fixture = Map<String, Object?>.from(
      jsonDecode(
        await File(
                '../frontend/static/protocol/interaction-response-aad-v1.json')
            .readAsString(),
      ) as Map,
    );
    final rawChannel = Map<String, Object?>.from(fixture['channel']! as Map);
    final fixtureChannel = KaedeChannel(
      ref:
          EntityRef.parse('${rawChannel['id']}@${rawChannel['origin_domain']}'),
      type: ChannelType.text,
      position: 0,
      permissions: BigInt.zero,
      encryptionMode: 'e2ee',
      encryptionState: 'active',
      encryptionPolicyGeneration:
          int.parse('${rawChannel['encryption_policy_generation']}'),
      encryptionProtocol: mlsProtocol,
      encryptionSuite: mlsSuite,
      encryptionGroupId: '${rawChannel['encryption_group_id']}',
      encryptionEpoch: int.parse('${rawChannel['encryption_epoch']}'),
    );

    for (final rawVector in fixture['vectors']! as List) {
      final vector = Map<String, Object?>.from(rawVector as Map);
      final input = Map<String, Object?>.from(vector['input']! as Map);
      final context = mobileInteractionResponseAuthenticatedContext(
        fixtureChannel,
        authorityDomain: input['authorityDomain']! as String,
        interactionRef: input['interactionRef']! as String,
        responseRef: input['responseRef']! as String,
        invokerRef: input['invokerRef']! as String,
        channelRef: input['channelRef']! as String,
        applicationRef: input['applicationRef']! as String,
        sequence: input['sequence']! as int,
        revision: input['revision']! as String,
        callbackType: input['callbackType']! as int,
        operation: input['operation']! as String,
        attachmentRefs: (input['attachmentRefs']! as List).cast<String>(),
        interactionContractDigest:
            input['interactionContractDigest'] as String?,
        senderDeviceId: input['senderDeviceId']! as String,
      );
      expect(
        context,
        Map<String, Object?>.from(vector['context']! as Map),
        reason: '${vector['name']} context',
      );
      final aad = mobileCanonicalInteractionJson(<String, Object?>{
        'context': context,
        'purpose': 'kaede.interaction.response.v1',
      });
      expect(
        base64Url.encode(aad).replaceAll('=', ''),
        vector['aad_base64url'],
        reason: '${vector['name']} AAD',
      );
      final contract = await mobileInteractionRoutingContract(
        Map<String, Object?>.from(vector['data']! as Map),
        input['callbackType']! as int,
      );
      expect(
        contract,
        vector['interaction_contract'],
        reason: '${vector['name']} routing contract',
      );
      expect(
        contract == null
            ? null
            : await mobileInteractionRoutingContractDigest(contract),
        input['interactionContractDigest'],
        reason: '${vector['name']} routing digest',
      );
    }
  });

  test('mobile matches shared private routing contracts and rejects mutations',
      () async {
    final fixture = Map<String, Object?>.from(
      jsonDecode(
        await File(
          '../frontend/static/protocol/interaction-routing-contract-v1.json',
        ).readAsString(),
      ) as Map,
    );
    final vectors = fixture['vectors']! as List;
    for (final rawVector in vectors) {
      final vector = Map<String, Object?>.from(rawVector as Map);
      final contract = await mobileInteractionRoutingContract(
        Map<String, Object?>.from(vector['input']! as Map),
        vector['callback_type']! as int,
      );
      expect(contract, vector['contract'], reason: '${vector['name']}');
      expect(
        await mobileInteractionRoutingContractDigest(contract!),
        vector['digest'],
        reason: '${vector['name']} digest',
      );
      expect(
        validateMobileInteractionRoutingContract(
          vector['contract'],
          vector['callback_type']! as int,
        ),
        vector['contract'],
        reason: '${vector['name']} validation',
      );
    }
    expect(
      Map<String, Object?>.from((vectors[0] as Map)['contract'] as Map),
      Map<String, Object?>.from((vectors[1] as Map)['contract'] as Map),
    );
    for (final rawInvalid in fixture['invalid_contracts']! as List) {
      final invalid = Map<String, Object?>.from(rawInvalid as Map);
      expect(
        () => validateMobileInteractionRoutingContract(
          invalid['contract'],
          invalid['callback_type']! as int,
        ),
        throwsFormatException,
        reason: '${invalid['name']}',
      );
    }
    final changed = Map<String, Object?>.from(
      jsonDecode(jsonEncode((vectors[0] as Map)['input'])) as Map,
    );
    final layout = Map<String, Object?>.from(
      (changed['components']! as List).first as Map,
    );
    final control = Map<String, Object?>.from(
      (layout['components']! as List).first as Map,
    );
    final options = (control['options']! as List)
        .map((value) => Map<String, Object?>.from(value as Map))
        .toList();
    options.first['value'] = 'different';
    control['options'] = options;
    layout['components'] = <Object?>[control];
    changed['components'] = <Object?>[layout];
    final changedContract = await mobileInteractionRoutingContract(changed, 4);
    expect(
      await mobileInteractionRoutingContractDigest(changedContract!),
      isNot((vectors[0] as Map)['digest']),
    );
  });

  test('mobile matches shared human and bot rich-message AAD vectors',
      () async {
    final fixture = Map<String, Object?>.from(
      jsonDecode(
        await File('../frontend/static/protocol/message-rich-aad-v1.json')
            .readAsString(),
      ) as Map,
    );
    for (final rawVector in fixture['vectors']! as List) {
      final vector = Map<String, Object?>.from(rawVector as Map);
      final richData = Map<String, Object?>.from(vector['rich_data']! as Map);
      final context = validateMobileRichMessageAuthenticatedContext(
        vector['context'],
      );
      expect(
        context,
        Map<String, Object?>.from(vector['context']! as Map),
        reason: '${vector['name']} context',
      );
      expect(
        await mobileRichMessagePayloadDigest(richData),
        context['rich_payload_digest'],
        reason: '${vector['name']} rich digest',
      );
      expect(
        await mobileRichMessageForwardProjectionDigest(
          richData,
          (context['message_mention_refs']! as List).cast<String>(),
        ),
        context['forward_projection_digest'],
        reason: '${vector['name']} forward projection',
      );
      expect(
        mobileRichMessageCustomEmojiRefs(richData),
        context['message_custom_emoji_refs'],
        reason: '${vector['name']} custom emoji refs',
      );
      final mentionIntent = mobileRichMessageMentionIntent(richData);
      expect(
        mentionIntent.userRefs,
        context['message_mention_user_refs'],
        reason: '${vector['name']} user mention intent',
      );
      expect(
        mentionIntent.roleRefs,
        context['message_mention_role_refs'],
        reason: '${vector['name']} role mention intent',
      );
      expect(
        mentionIntent.everyone,
        context['message_mention_everyone'],
        reason: '${vector['name']} everyone mention intent',
      );
      expect(
        mobileRichMessageStickerRefs(richData),
        context['message_sticker_refs'],
        reason: '${vector['name']} sticker refs',
      );
      expect(
        context.keys.toSet(),
        (fixture['context_fields']! as List).cast<String>().toSet(),
        reason: '${vector['name']} exact context fields',
      );
      final aad = mobileRichMessageAuthenticatedData(context);
      expect(
        base64Url.encode((await Sha256().hash(aad)).bytes).replaceAll('=', ''),
        vector['aad_sha256'],
        reason: '${vector['name']} AAD',
      );
      final contract = await mobileInteractionRoutingContract(richData, null);
      expect(
        contract,
        vector['interaction_contract'],
        reason: '${vector['name']} routing contract',
      );
      expect(
        contract == null
            ? null
            : await mobileInteractionRoutingContractDigest(contract),
        context['interaction_contract_digest'],
        reason: '${vector['name']} routing digest',
      );
    }
  });

  test('mobile rich mention routing rejects ambiguous plaintext policy', () {
    expect(
      () => mobileRichMessageMentionIntent(<String, Object?>{
        'content': 'hi <@42>',
        'components': const <Object?>[],
        'allowed_mentions': <String, Object?>{
          'parse': const <String>['users'],
          'users': const <String>[],
          'roles': const <String>[],
          'replied_user': false,
        },
      }),
      throwsFormatException,
    );
    expect(
      () => validateMobileEncryptedAllowedMentions(<String, Object?>{
        'parse': const <String>['users'],
        'users': const <String>['42@example.test'],
        'roles': const <String>[],
        'replied_user': false,
      }),
      throwsFormatException,
    );
  });

  test('mobile accepts only exact authenticated voice manifests', () {
    final manifest = <String, Object?>{
      'version': 1,
      'protocol': 'kaede-file-v1',
      'file_id': List.filled(22, 'A').join(),
      'key': List.filled(43, 'A').join(),
      'filename': 'voice.m4a',
      'content_type': 'audio/mp4',
      'plaintext_size': 1,
      'plaintext_sha256': List.filled(43, 'A').join(),
      'ciphertext_size': 62,
      'ciphertext_sha256': List.filled(43, 'A').join(),
      'chunk_size': 65536,
      'attachment_id': '1',
      'attachment_domain': 'example.test',
      'duration_millis': 1250,
      'waveform': 'AQ==',
    };
    expect(
      validateMobileEncryptedRichMessageAttachments(
        <Object?>[manifest],
        voiceMessage: true,
      ),
      <Object?>[manifest],
    );
    expect(
      () => validateMobileEncryptedRichMessageAttachments(
        <Object?>[manifest],
        voiceMessage: false,
      ),
      throwsFormatException,
    );
    expect(
      () => validateMobileEncryptedRichMessageAttachments(
        <Object?>[
          <String, Object?>{...manifest, 'waveform': 'AQ'}
        ],
        voiceMessage: true,
      ),
      throwsFormatException,
    );
    expect(
      () => validateMobileEncryptedRichMessageAttachments(
        <Object?>[manifest, 1],
        voiceMessage: true,
      ),
      throwsFormatException,
    );
  });

  test('mobile consumes the shared plaintext-committed file manifest',
      () async {
    final fixture = Map<String, Object?>.from(
      jsonDecode(
        await File('../frontend/static/protocol/kaede-file-v1.json')
            .readAsString(),
      ) as Map,
    );
    final manifest = <String, Object?>{
      ...Map<String, Object?>.from(fixture['manifest']! as Map),
      'attachment_id': '1',
      'attachment_domain': 'files.example',
    };
    expect(
      validateMobileEncryptedRichMessageAttachments(
        <Object?>[manifest],
        voiceMessage: false,
      ),
      <Object?>[manifest],
    );
    expect(
      () => validateMobileEncryptedRichMessageAttachments(
        <Object?>[
          <String, Object?>{...manifest}..remove('plaintext_sha256'),
        ],
        voiceMessage: false,
      ),
      throwsFormatException,
    );
  });

  test('mobile binds public bot credentials to exact app and device projection',
      () {
    final deviceId = 'kbe_${List.filled(43, 'A').join()}';
    final credential = Uint8List.fromList(
      utf8.encode(
        jsonEncode(<String, Object?>{
          'account': 'bot:10@apps.example:worker:7',
          'application_ref': '10@apps.example',
          'credential_type': 'kaede-bot-device-v2',
          'device_id': deviceId,
          'worker_id': '7',
        }),
      ),
    );
    KaedeMessage message(EntityRef? application) => KaedeMessage(
          ref: EntityRef.parse('60@guild.example'),
          channelRef: EntityRef.parse('30@guild.example'),
          authorRef: EntityRef.parse('50@bots.example'),
          createdAt: DateTime.utc(2026, 8, 28),
          applicationRef: application,
        );
    expect(
      () => validateMobileEncryptedMessageSenderCredential(
        credential,
        message(EntityRef.parse('10@apps.example')),
        deviceId,
      ),
      returnsNormally,
    );
    expect(
      () => validateMobileEncryptedMessageSenderCredential(
        credential,
        message(EntityRef.parse('11@apps.example')),
        deviceId,
      ),
      throwsFormatException,
    );
    expect(
      () => validateMobileEncryptedMessageSenderCredential(
        credential,
        message(null),
        deviceId,
      ),
      throwsFormatException,
    );
  });

  test('mobile binds public webhook credentials to exact webhook and device',
      () {
    final deviceId = 'kwe_${List.filled(43, 'W').join()}';
    final credential = Uint8List.fromList(
      utf8.encode(
        jsonEncode(<String, Object?>{
          'account': 'webhook:70@hooks.example',
          'credential_type': 'kaede-webhook-device-v1',
          'device_id': deviceId,
          'webhook_ref': '70@hooks.example',
        }),
      ),
    );
    KaedeMessage message({EntityRef? webhook, EntityRef? application}) =>
        KaedeMessage(
          ref: EntityRef.parse('60@guild.example'),
          channelRef: EntityRef.parse('30@guild.example'),
          authorRef: EntityRef.parse('50@hooks.example'),
          createdAt: DateTime.utc(2026, 8, 28),
          webhookRef: webhook,
          applicationRef: application,
        );
    final roundTrip = KaedeMessage.fromTrustedCacheJson(
      message(webhook: EntityRef.parse('70@hooks.example')).toJson(),
    );
    expect(roundTrip.webhookRef?.wire, '70@hooks.example');
    expect(
      () => validateMobileEncryptedMessageSenderCredential(
        credential,
        message(webhook: EntityRef.parse('70@hooks.example')),
        deviceId,
      ),
      returnsNormally,
    );
    expect(
      () => validateMobileEncryptedMessageSenderCredential(
        credential,
        message(webhook: EntityRef.parse('71@hooks.example')),
        deviceId,
      ),
      throwsFormatException,
    );
    expect(
      () => validateMobileEncryptedMessageSenderCredential(
        credential,
        message(
          webhook: EntityRef.parse('70@hooks.example'),
          application: EntityRef.parse('10@apps.example'),
        ),
        deviceId,
      ),
      throwsFormatException,
    );
  });

  test('mobile derives webhook MLS device from exact ref and identity key',
      () async {
    final identityKey = List<int>.generate(32, (index) => index);
    final expected = base64Url
        .encode((await Sha256().hash(<int>[
          ...utf8.encode(
            'kaede-webhook-e2ee-device-v1\u000070@hooks.example\u0000',
          ),
          ...identityKey,
        ]))
            .bytes)
        .replaceAll('=', '');
    expect(
      await mobileWebhookE2eeDeviceId(
        EntityRef.parse('70@hooks.example'),
        identityKey,
      ),
      'kwe_$expected',
    );
  });
}
