import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/guild_admin_repository.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/scanned_media.dart';
import 'package:kaede_mobile/src/api/scheduled_events_repository.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/guild_admin.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/scheduled_events.dart';
import 'package:kaede_mobile/src/features/guild/guild_admin_advanced.dart';
import 'package:kaede_mobile/src/features/guild/guild_management_screen.dart';
import 'package:kaede_mobile/src/features/guild/scheduled_events_tab.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

void main() {
  final guild = EntityRef.parse('1@chat.example');

  test('AutoMod models preserve every trigger, action, and exemption field',
      () {
    final rule = AutoModRule.fromJson(_ruleJson());

    expect(rule.ref.wire, '90@chat.example');
    expect(rule.triggerMetadata.keywordFilter, ['blocked*']);
    expect(rule.triggerMetadata.regexPatterns, [r'bad\s+word']);
    expect(rule.triggerMetadata.allowList, ['allowed phrase']);
    expect(rule.actions.map((item) => item.type), [
      'block_message',
      'send_alert_message',
      'timeout',
    ]);
    expect(rule.actions[1].channelRef?.wire, '2@chat.example');
    expect(rule.actions[2].durationSeconds, 600);
    expect(rule.exemptRoles.single.wire, '7@chat.example');
    expect(rule.exemptChannels.single.wire, '3@chat.example');

    final request = AutoModRuleDraft.fromRule(rule).toJson();
    expect(request['trigger_metadata'], <String, Object?>{
      'keyword_filter': ['blocked*'],
      'regex_patterns': [r'bad\s+word'],
      'presets': <String>[],
      'allow_list': ['allowed phrase'],
      'mention_total_limit': null,
      'mention_raid_protection_enabled': false,
    });
    expect(request['exempt_roles'], ['7@chat.example']);
    expect(request['exempt_channels'], ['3@chat.example']);
  });

  test('guild administration arrays reject malformed federated children', () {
    final rule = _ruleJson();
    rule['actions'] = <Object?>[
      ...(rule['actions']! as List),
      'not an action',
    ];
    expect(
      () => AutoModRule.fromJson(rule),
      throwsA(isA<FormatException>()),
    );
  });

  test('AutoMod client validation describes invalid Discord-style rules', () {
    const invalid = AutoModRuleDraft(
      name: 'keywords',
      eventType: 'message_send',
      triggerType: 'keyword',
      triggerMetadata: AutoModTriggerMetadata(),
      actions: [AutoModAction(type: 'block_message')],
      enabled: true,
    );
    expect(
      autoModDraftValidationMessage(invalid),
      'Add at least one keyword or regular expression.',
    );

    const valid = AutoModRuleDraft(
      name: 'mention raid',
      eventType: 'message_send',
      triggerType: 'mention_spam',
      triggerMetadata: AutoModTriggerMetadata(
        mentionTotalLimit: 5,
        mentionRaidProtectionEnabled: true,
      ),
      actions: [
        AutoModAction(type: 'block_message'),
        AutoModAction(type: 'timeout', durationSeconds: 600),
      ],
      enabled: true,
    );
    expect(autoModDraftValidationMessage(valid), isNull);
  });

  test('bulk moderation parser canonicalizes and deduplicates user refs', () {
    expect(
      parseCanonicalUserRefs(
        '2@chat.example\n2@chat.example,3',
        Domain('chat.example'),
      ).map((item) => item.wire),
      ['2@chat.example', '3@chat.example'],
    );
    expect(
      () => parseCanonicalUserRefs('not-a-ref', Domain('chat.example')),
      throwsFormatException,
    );
  });

  test('advanced voice settings parse and serialize without owning regions',
      () {
    final channel = KaedeChannel.fromJson(<String, Object?>{
      'id': '2',
      'origin_domain': 'chat.example',
      'guild_id': '1',
      'guild_domain': 'chat.example',
      'type': 2,
      'position': 0,
      'permissions': '0',
      'name': 'Lounge',
      'bitrate': 128000,
      'user_limit': 24,
      'rtc_region': 'future-region',
      'video_quality_mode': 2,
    });
    expect(channel.bitrate, 128000);
    expect(channel.userLimit, 24);
    expect(channel.rtcRegion, 'future-region');
    expect(channel.videoQualityMode, 2);

    const draft = GuildChannelDraft(
      name: 'Lounge',
      topic: '',
      type: ChannelType.voice,
      slowModeSeconds: 0,
      bitrate: 96000,
      userLimit: 12,
      rtcRegion: 'future-region',
      videoQualityMode: 2,
    );
    expect(draft.json, containsPair('bitrate', 96000));
    expect(draft.json, containsPair('user_limit', 12));
    expect(draft.json, containsPair('rtc_region', 'future-region'));
    expect(draft.json, containsPair('video_quality_mode', 2));
  });

  test('soundboard parsing preserves playback integrity metadata', () {
    final sound = SoundboardSound.fromJson(_soundJson());
    expect(sound.ref.wire, '70@chat.example');
    expect(sound.guildRef, guild);
    expect(sound.mediaHash, 'a' * 64);
    expect(sound.durationMilliseconds, 2100);
    expect(sound.creatorRef?.wire, '4@chat.example');
    expect(sound.displayEmoji, '🎉');
    expect(soundboardContentType('clip.MP3'), 'audio/mpeg');
    expect(soundboardContentType('clip.oga'), 'audio/ogg');
    expect(soundboardContentType('clip.wav'), isNull);
    expect(soundboardGatewayDispatch, 'VOICE_CHANNEL_EFFECT_SEND');
  });

  group('creator-owned guild expressions', () {
    final currentUser = EntityRef.parse('4@chat.example');
    final otherUser = EntityRef.parse('5@chat.example');

    test('accepts both creator field spellings with exact domains', () {
      final emoji = <String, Object?>{
        'creator_id': '4',
        'creator_domain': 'chat.example',
      };
      final sound = <String, Object?>{
        'created_by_id': '4',
        'created_by_domain': 'chat.example',
      };

      expect(guildExpressionCreatorRef(emoji), currentUser);
      expect(guildExpressionCreatorRef(sound), currentUser);
      expect(guildExpressionOwnedBy(emoji, currentUser), isTrue);
      expect(guildExpressionOwnedBy(sound, currentUser), isTrue);
      expect(
        guildExpressionOwnedBy(<String, Object?>{
          ...emoji,
          'creator_domain': 'remote.example',
        }, currentUser),
        isFalse,
      );
      expect(
        guildExpressionOwnedBy(
          const <String, Object?>{'creator_id': '4'},
          currentUser,
        ),
        isFalse,
      );
    });

    test('create maintains only own expressions while manage maintains all',
        () {
      expect(
        canModifyGuildExpression(
          creatorRef: currentUser,
          currentUserRef: currentUser,
          canCreate: true,
          canManage: false,
        ),
        isTrue,
      );
      expect(
        canModifyGuildExpression(
          creatorRef: otherUser,
          currentUserRef: currentUser,
          canCreate: true,
          canManage: false,
        ),
        isFalse,
      );
      expect(
        canModifyGuildExpression(
          creatorRef: currentUser,
          currentUserRef: currentUser,
          canCreate: false,
          canManage: false,
        ),
        isFalse,
      );
      expect(
        canModifyGuildExpression(
          creatorRef: otherUser,
          currentUserRef: currentUser,
          canCreate: false,
          canManage: true,
        ),
        isTrue,
      );
    });
  });

  test(
      'scheduled events preserve Discord entity/status values and exact patch fields',
      () {
    final event = GuildScheduledEvent.fromJson(_scheduledEventJson());
    expect(event.ref.wire, '95@chat.example');
    expect(event.guildRef, guild);
    expect(event.entityType, ScheduledEventEntityType.voice);
    expect(event.status, ScheduledEventStatus.scheduled);
    expect(event.channelRef?.wire, '2@chat.example');
    expect(event.userCount, 3);
    expect(event.meSubscribed, isTrue);

    final unchanged = ScheduledEventDraft.fromEvent(event).patchFor(event);
    expect(unchanged, isEmpty);
    final renamed = ScheduledEventDraft(
      name: 'Community town hall',
      description: event.description ?? '',
      entityType: event.entityType,
      channelRef: event.channelRef,
      startTime: event.startTime,
      endTime: event.endTime,
    ).patchFor(event);
    expect(renamed, <String, Object?>{'name': 'Community town hall'});

    final weekly = ScheduledEventDraft(
      name: event.name,
      description: event.description ?? '',
      entityType: event.entityType,
      channelRef: event.channelRef,
      startTime: event.startTime,
      endTime: event.endTime,
      recurrence: ScheduledEventRecurrencePreset.weekly,
    ).toCreateJson();
    expect(weekly['recurrence_rule'], <String, Object?>{
      'start': '2027-01-03T18:00:00.000Z',
      'end': null,
      'interval': 1,
      'frequency': 2,
      'by_weekday': <int>[6],
    });
  });

  test('scheduled event repository uses human routes for full lifecycle',
      () async {
    final adapter = _QueueAdapter([
      _Reply(jsonEncode(<Object?>[_scheduledEventJson()])),
      _Reply(jsonEncode(_scheduledEventJson())),
      _Reply(jsonEncode(_scheduledEventJson(status: 2))),
      _Reply(jsonEncode(<Object?>[_scheduledEventSubscriberJson()])),
      const _Reply('', status: 204),
      const _Reply('', status: 204),
      const _Reply('', status: 204),
    ]);
    final repository = _repository(adapter);
    final events = await repository.scheduledEvents(guild);
    final event = events.single;
    final draft = ScheduledEventDraft.fromEvent(event);
    await repository.createScheduledEvent(guild, draft);
    await repository.transitionScheduledEvent(
      guild,
      event,
      ScheduledEventStatus.active,
    );
    final subscribers = await repository.scheduledEventSubscribers(
      guild,
      event,
      after: EntityRef.parse('4@chat.example'),
    );
    await repository.setScheduledEventSubscription(
      guild,
      event,
      subscribed: true,
    );
    await repository.setScheduledEventSubscription(
      guild,
      event,
      subscribed: false,
    );
    await repository.deleteScheduledEvent(guild, event);

    expect(subscribers.single.user.ref.wire, '4@chat.example');
    expect(
      adapter.requests.map((request) => '${request.method} ${request.path}'),
      <String>[
        'GET /api/v1/guilds/1@chat.example/scheduled-events',
        'POST /api/v1/guilds/1@chat.example/scheduled-events',
        'PATCH /api/v1/guilds/1@chat.example/scheduled-events/95@chat.example',
        'GET /api/v1/guilds/1@chat.example/scheduled-events/95@chat.example/users',
        'PUT /api/v1/guilds/1@chat.example/scheduled-events/95@chat.example/users/@me',
        'DELETE /api/v1/guilds/1@chat.example/scheduled-events/95@chat.example/users/@me',
        'DELETE /api/v1/guilds/1@chat.example/scheduled-events/95@chat.example',
      ],
    );
    expect(adapter.requests[0].queryParameters['with_user_count'], true);
    expect(adapter.requests[2].data, <String, Object?>{'status': 2});
    expect(adapter.requests[3].queryParameters,
        containsPair('after', '4@chat.example'));
  });

  test('webhook moves and avatar endpoints use exact human API payloads',
      () async {
    final adapter = _QueueAdapter([
      _Reply(jsonEncode(_webhookJson(channelId: '3'))),
      _Reply(jsonEncode(<String, Object?>{
        'id': '91',
        'upload_url': 'https://uploads.example/avatar',
      })),
      _Reply(jsonEncode(<String, Object?>{
        'status': 'processing',
        'attachment': <String, Object?>{'scan_status': 'pending'},
      })),
      _Reply(jsonEncode(_webhookJson(avatarHash: null))),
    ]);
    final repository = _repository(adapter);

    final webhook = EntityRef.parse('80@remote.example');
    final webhookGuild = EntityRef.parse('1@remote.example');
    await repository.updateWebhook(webhookGuild, webhook, <String, Object?>{
      'name': 'Release bot',
      'channel_id': '3@remote.example',
    });
    await repository.createWebhookAvatarTicket(
      guild: webhookGuild,
      webhook: webhook,
      filename: 'avatar.png',
      contentType: 'image/png',
      size: 2048,
    );
    await repository.commitWebhookAvatar(webhookGuild, webhook, '91');
    await repository.clearWebhookAvatar(webhookGuild, webhook);

    expect(
      adapter.requests.map((request) => '${request.method} ${request.path}'),
      <String>[
        'PATCH /api/v1/webhooks/80%40remote.example',
        'POST /api/v1/webhooks/80%40remote.example/avatar/tickets',
        'PUT /api/v1/webhooks/80%40remote.example/avatar',
        'DELETE /api/v1/webhooks/80%40remote.example/avatar',
      ],
    );
    for (final request in adapter.requests) {
      expect(request.queryParameters,
          containsPair('guild_ref', '1@remote.example'));
    }
    expect(adapter.requests[0].data, <String, Object?>{
      'name': 'Release bot',
      'channel_id': '3@remote.example',
    });
    expect(adapter.requests[1].data, <String, Object?>{
      'filename': 'avatar.png',
      'content_type': 'image/png',
      'size': 2048,
    });
    expect(adapter.requests[2].data, <String, Object?>{
      'attachment_id': '91',
    });
  });

  test('webhook avatar scan lifecycle recommits only after a clean scan',
      () async {
    var commits = 0;
    final result = await completeScannedMediaResource<Map<String, Object?>>(
      commit: () async {
        commits += 1;
        return commits == 1
            ? <String, Object?>{
                'status': 'processing',
                'attachment': <String, Object?>{'scan_status': 'pending'},
              }
            : _webhookJson(avatarHash: 'c' * 64);
      },
      isComplete: (json) =>
          json['channel_id'] != null && json['avatar_hash'] is String,
      parse: (json) => json,
      pollInterval: Duration.zero,
      maxPollAttempts: 3,
    );

    expect(result['avatar_hash'], 'c' * 64);
    expect(commits, 2);
  });

  test('remote invite revocation binds its code to the qualified guild',
      () async {
    final adapter = _QueueAdapter([_Reply('{}')]);
    final repository = _repository(adapter);

    await repository.revokeInvite(
      'Ab12Cd34',
      guild: EntityRef.parse('1@remote.example'),
    );

    expect(
      '${adapter.requests.single.method} ${adapter.requests.single.path}',
      'DELETE /api/v1/invites/Ab12Cd34%40remote.example',
    );
    expect(
      adapter.requests.single.queryParameters,
      containsPair('guild_ref', '1@remote.example'),
    );
  });

  test('webhook avatar scan lifecycle reports a clear safety rejection',
      () async {
    await expectLater(
      completeScannedMediaResource<Map<String, Object?>>(
        commit: () async => <String, Object?>{
          'status': 'rejected',
          'attachment': <String, Object?>{'scan_status': 'rejected'},
        },
        isComplete: (json) => json['avatar_hash'] is String,
        parse: (json) => json,
        pollInterval: Duration.zero,
      ),
      throwsA(
        isA<KaedeException>().having(
          (error) => error.message,
          'message',
          contains('did not pass media safety processing'),
        ),
      ),
    );
  });

  test('repository covers AutoMod, prune, and bulk-ban endpoints', () async {
    final adapter = _QueueAdapter([
      _Reply(jsonEncode(<Object?>[_ruleJson()])),
      _Reply(jsonEncode(_ruleJson())),
      _Reply(jsonEncode(_ruleJson())),
      const _Reply('', status: 204),
      _Reply(jsonEncode(<String, Object?>{'pruned': 3, 'days': 14})),
      _Reply(jsonEncode(<String, Object?>{
        'days': 14,
        'pruned': 2,
        'pruned_user_ids': ['20@chat.example', '21@chat.example'],
        'failed_users': <Object?>[],
      })),
      _Reply(jsonEncode(<String, Object?>{
        'banned_users': ['30@chat.example'],
        'failed_users': ['31@chat.example'],
        'failed_user_details': <Object?>[
          <String, Object?>{
            'user_id': '31@chat.example',
            'code': 'ROLE_HIERARCHY',
            'message': 'That member is above your highest role.',
          },
        ],
      })),
    ]);
    final repository = _repository(adapter);
    final draft = AutoModRuleDraft.fromRule(
      AutoModRule.fromJson(_ruleJson()),
    );

    expect((await repository.autoModRules(guild)).single.name, 'Keyword guard');
    await repository.createAutoModRule(guild, draft);
    await repository.updateAutoModRule(
      guild,
      EntityRef.parse('90@chat.example'),
      draft,
    );
    await repository.deleteAutoModRule(
      guild,
      EntityRef.parse('90@chat.example'),
    );
    expect(
      await repository.estimatePrune(
        guild,
        days: 14,
        includeRoles: [EntityRef.parse('7@chat.example')],
      ),
      3,
    );
    final prune = await repository.pruneMembers(
      guild,
      days: 14,
      includeRoles: [EntityRef.parse('7@chat.example')],
    );
    final bans = await repository.bulkBanMembers(
      guild,
      [EntityRef.parse('30@chat.example'), EntityRef.parse('31@chat.example')],
      deleteMessageSeconds: 86400,
      reason: 'Raid',
    );

    expect(prune.pruned, 2);
    expect(bans.bannedUserRefs.single.wire, '30@chat.example');
    expect(bans.failures.single.code, 'ROLE_HIERARCHY');
    expect(
      adapter.requests.map((request) => request.path),
      [
        '/api/v1/guilds/1@chat.example/auto-moderation/rules',
        '/api/v1/guilds/1@chat.example/auto-moderation/rules',
        '/api/v1/guilds/1@chat.example/auto-moderation/rules/90',
        '/api/v1/guilds/1@chat.example/auto-moderation/rules/90',
        '/api/v1/guilds/1@chat.example/prune/estimate',
        '/api/v1/guilds/1@chat.example/prune',
        '/api/v1/guilds/1@chat.example/bulk-bans',
      ],
    );
    expect(adapter.requests[4].queryParameters, <String, Object?>{
      'days': 14,
      'include_roles': ['7@chat.example'],
    });
    expect(adapter.requests[6].data, <String, Object?>{
      'user_ids': ['30@chat.example', '31@chat.example'],
      'delete_message_seconds': 86400,
      'reason': 'Raid',
    });
  });

  test('repository covers expression editing and soundboard management',
      () async {
    final adapter = _QueueAdapter([
      _Reply(jsonEncode(<Object?>[
        <String, Object?>{
          'id': '50',
          'origin_domain': 'chat.example',
          'guild_id': '1',
          'guild_domain': 'chat.example',
          'name': 'wave',
          'animated': false,
          'available': true,
          'roles': <Object?>[],
          'media_hash': 'b' * 64,
        },
      ])),
      _Reply(jsonEncode(<String, Object?>{
        'id': '50',
        'origin_domain': 'chat.example',
        'guild_id': '1',
        'guild_domain': 'chat.example',
        'name': 'wave2',
      })),
      _Reply(jsonEncode(<String, Object?>{
        'items': <Object?>[_soundJson()],
      })),
      _Reply(jsonEncode(_soundJson(name: 'Party 2'))),
      _Reply(jsonEncode(<String, Object?>{
        'sound': _soundJson(),
        'download_url': 'https://media.example/sound.mp3',
        'effective_volume': .8,
      })),
      const _Reply('', status: 204),
    ]);
    final repository = _repository(adapter);

    expect((await repository.guildEmojis(guild)).single['name'], 'wave');
    await repository.updateGuildEmoji(
      guild,
      EntityRef.parse('50@chat.example'),
      <String, Object?>{
        'name': 'wave2',
        'role_ids': ['7@chat.example'],
      },
    );
    expect((await repository.soundboardSounds(guild)).single.name, 'Party');
    await repository.updateSoundboardSound(
      guild,
      EntityRef.parse('70@chat.example'),
      <String, Object?>{'name': 'Party 2'},
    );
    await repository.playSoundboardSound(
      EntityRef.parse('2@chat.example'),
      EntityRef.parse('70@chat.example'),
      guild,
      soundVersion: 1,
      volume: .8,
    );
    await repository.deleteSoundboardSound(
      guild,
      EntityRef.parse('70@chat.example'),
    );

    expect(adapter.requests[1].method, 'PATCH');
    expect(
      adapter.requests[1].path,
      '/api/v1/guilds/1@chat.example/emojis/50',
    );
    expect(
      adapter.requests[4].path,
      '/api/v1/channels/2@chat.example/send-soundboard-sound',
    );
    expect(adapter.requests[4].data, <String, Object?>{
      'sound_id': '70@chat.example',
      'sound_version': '1',
      'source_guild_id': '1@chat.example',
      'volume': .8,
    });
  });

  test('soundboard update payloads distinguish custom and Unicode emoji',
      () async {
    final adapter = _QueueAdapter([
      _Reply(jsonEncode(_soundJson())),
      _Reply(jsonEncode(_soundJson())),
    ]);
    final repository = _repository(adapter);
    final sound = EntityRef.parse('70@chat.example');

    await repository.updateSoundboardSound(guild, sound, <String, Object?>{
      'emoji_id': '50',
      'emoji_name': null,
    });
    await repository.updateSoundboardSound(guild, sound, <String, Object?>{
      'emoji_id': null,
      'emoji_name': '🎉',
    });

    expect(adapter.requests[0].data, <String, Object?>{
      'emoji_id': '50',
      'emoji_name': null,
    });
    expect(adapter.requests[1].data, <String, Object?>{
      'emoji_id': null,
      'emoji_name': '🎉',
    });
  });

  testWidgets('soundboard editor exposes custom guild emoji choices',
      (tester) async {
    SoundboardSoundDraft? result;
    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: Builder(
        builder: (context) => Scaffold(
          body: FilledButton(
            onPressed: () async {
              result = await showSoundboardSoundEditor(
                context,
                title: 'Edit sound',
                action: 'Save',
                initialName: 'Party',
                guildEmojis: <Map<String, Object?>>[
                  <String, Object?>{
                    'id': '50',
                    'origin_domain': 'chat.example',
                    'name': 'wave',
                    'available': true,
                  },
                ],
                fallbackDomain: Domain('chat.example'),
              );
            },
            child: const Text('Open sound editor'),
          ),
        ),
      ),
    ));

    await tester.tap(find.text('Open sound editor'));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('soundboard-emoji-source')));
    await tester.pumpAndSettle();
    await tester.tap(find.text(':wave: · custom').last);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Save'));
    await tester.pumpAndSettle();

    expect(result?.emojiRef?.wire, '50@chat.example');
    expect(result?.emojiName, isEmpty);
  });

  testWidgets('soundboard editor preserves an existing Unicode emoji',
      (tester) async {
    SoundboardSoundDraft? result;
    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: Builder(
        builder: (context) => Scaffold(
          body: FilledButton(
            onPressed: () async {
              result = await showSoundboardSoundEditor(
                context,
                title: 'Edit sound',
                action: 'Save',
                initialName: 'Party',
                initialEmojiName: '🎉',
                guildEmojis: const <Map<String, Object?>>[],
                fallbackDomain: Domain('chat.example'),
              );
            },
            child: const Text('Open Unicode sound editor'),
          ),
        ),
      ),
    ));

    await tester.tap(find.text('Open Unicode sound editor'));
    await tester.pumpAndSettle();
    expect(
      find.byKey(const Key('soundboard-unicode-emoji')),
      findsOneWidget,
    );
    await tester.tap(find.text('Save'));
    await tester.pumpAndSettle();

    expect(result?.emojiRef, isNull);
    expect(result?.emojiName, '🎉');
  });

  testWidgets('scheduled event editor offers stage, voice, and external',
      (tester) async {
    final channel = KaedeChannel.fromJson(<String, Object?>{
      'id': '2',
      'origin_domain': 'chat.example',
      'guild_id': '1',
      'guild_domain': 'chat.example',
      'type': 2,
      'position': 0,
      'permissions':
          '${Permission.createEvents | Permission.viewChannel | Permission.connect}',
      'name': 'Town square',
    });
    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: Builder(
        builder: (context) => Scaffold(
          body: FilledButton(
            onPressed: () => showScheduledEventEditor(
              context,
              eventChannels: [channel],
            ),
            child: const Text('Open event editor'),
          ),
        ),
      ),
    ));

    await tester.tap(find.text('Open event editor'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Voice channel').first);
    await tester.pumpAndSettle();

    expect(find.text('Stage channel'), findsOneWidget);
    expect(find.text('External'), findsOneWidget);
    expect(find.text('Voice channel'), findsWidgets);
  });

  testWidgets('advanced invite picks an authoritative scheduled event',
      (tester) async {
    final scheduledEvent = GuildScheduledEvent.fromJson(_scheduledEventJson());
    final inviteGuild = KaedeGuild.fromJson(<String, Object?>{
      'id': '1',
      'origin_domain': 'chat.example',
      'name': 'Guild',
      'owner_id': '4',
      'owner_domain': 'chat.example',
      'permissions': '${Permission.createInvite}',
      'unavailable': false,
      'channels': <Object?>[
        <String, Object?>{
          'id': '2',
          'origin_domain': 'chat.example',
          'guild_id': '1',
          'guild_domain': 'chat.example',
          'type': 2,
          'position': 0,
          'permissions': '${Permission.viewChannel | Permission.connect}',
          'name': 'Town square',
        },
      ],
      'roles': const <Object?>[],
    });
    Map<String, Object?>? result;
    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: Builder(
        builder: (context) => Scaffold(
          body: FilledButton(
            onPressed: () async {
              result = await showAdvancedInviteEditor(
                context,
                inviteGuild,
                scheduledEvents: [scheduledEvent],
              );
            },
            child: const Text('Open invite editor'),
          ),
        ),
      ),
    ));

    await tester.tap(find.text('Open invite editor'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('No event association'));
    await tester.pumpAndSettle();
    await tester.tap(find.textContaining('Town hall'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Create invite'));
    await tester.pumpAndSettle();

    expect(result?.containsKey('target_type'), isFalse);
    expect(result?['scheduled_event_id'], '95@chat.example');
  });

  testWidgets('advanced invite hides channel-overwrite denied destinations',
      (tester) async {
    final inviteGuild = KaedeGuild(
      ref: EntityRef.parse('1@chat.example'),
      name: 'Guild',
      ownerRef: EntityRef.parse('4@chat.example'),
      permissions: BigInt.from(Permission.createInvite),
      unavailable: false,
      channels: <KaedeChannel>[
        KaedeChannel(
          ref: EntityRef.parse('2@chat.example'),
          guildRef: EntityRef.parse('1@chat.example'),
          type: ChannelType.voice,
          position: 0,
          permissions: BigInt.from(Permission.createInvite),
          name: 'Allowed voice',
        ),
        KaedeChannel(
          ref: EntityRef.parse('3@chat.example'),
          guildRef: EntityRef.parse('1@chat.example'),
          type: ChannelType.voice,
          position: 1,
          permissions: BigInt.from(Permission.viewChannel),
          name: 'Denied voice',
        ),
      ],
    );

    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: Builder(
        builder: (context) => Scaffold(
          body: FilledButton(
            onPressed: () => showAdvancedInviteEditor(context, inviteGuild),
            child: const Text('Open invite editor'),
          ),
        ),
      ),
    ));

    await tester.tap(find.text('Open invite editor'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Guild landing (no channel)'));
    await tester.pumpAndSettle();

    expect(find.textContaining('Allowed voice'), findsOneWidget);
    expect(find.textContaining('Denied voice'), findsNothing);
    expect(find.text('Guild landing (no channel)'), findsWidgets);
  });
}

Map<String, Object?> _ruleJson() => <String, Object?>{
      'id': '90',
      'origin_domain': 'chat.example',
      'guild_id': '1',
      'guild_domain': 'chat.example',
      'name': 'Keyword guard',
      'creator_id': '4',
      'creator_domain': 'chat.example',
      'event_type': 'message_send',
      'trigger_type': 'keyword',
      'trigger_metadata': <String, Object?>{
        'keyword_filter': ['blocked*'],
        'regex_patterns': [r'bad\s+word'],
        'presets': <Object?>[],
        'allow_list': ['allowed phrase'],
        'mention_total_limit': null,
        'mention_raid_protection_enabled': false,
      },
      'actions': <Object?>[
        <String, Object?>{
          'type': 'block_message',
          'metadata': <String, Object?>{'custom_message': 'Please rephrase.'},
        },
        <String, Object?>{
          'type': 'send_alert_message',
          'metadata': <String, Object?>{'channel_id': '2@chat.example'},
        },
        <String, Object?>{
          'type': 'timeout',
          'metadata': <String, Object?>{'duration_seconds': 600},
        },
      ],
      'enabled': true,
      'exempt_roles': ['7@chat.example'],
      'exempt_channels': ['3@chat.example'],
      'version': 2,
      'created_at': '2026-08-27T01:00:00Z',
      'updated_at': '2026-08-27T02:00:00Z',
    };

Map<String, Object?> _soundJson({String name = 'Party'}) => <String, Object?>{
      'id': '70',
      'origin_domain': 'chat.example',
      'guild_id': '1',
      'guild_domain': 'chat.example',
      'name': name,
      'media_hash': 'a' * 64,
      'content_type': 'audio/mpeg',
      'volume': .8,
      'emoji_id': null,
      'emoji_domain': null,
      'emoji_name': '🎉',
      'available': true,
      'duration_ms': 2100,
      'created_by_id': '4',
      'created_by_domain': 'chat.example',
      'version': '2',
    };

Map<String, Object?> _scheduledEventJson({int status = 1}) => <String, Object?>{
      'id': '95',
      'origin_domain': 'chat.example',
      'guild_id': '1',
      'guild_domain': 'chat.example',
      'channel_id': '2',
      'channel_domain': 'chat.example',
      'creator_id': '4',
      'creator_domain': 'chat.example',
      'name': 'Town hall',
      'description': 'Quarterly questions',
      'scheduled_start_time': '2027-01-03T18:00:00Z',
      'scheduled_end_time': null,
      'privacy_level': 2,
      'status': status,
      'entity_type': 2,
      'entity_id': null,
      'entity_domain': null,
      'entity_metadata': null,
      'image': null,
      'created_at': '2027-01-01T00:00:00Z',
      'updated_at': '2027-01-01T00:00:00Z',
      'version': '1',
      'user_count': 3,
      'me_subscribed': true,
    };

Map<String, Object?> _scheduledEventSubscriberJson() => <String, Object?>{
      'guild_scheduled_event_id': '95',
      'guild_scheduled_event_domain': 'chat.example',
      'user': <String, Object?>{
        'id': '4',
        'origin_domain': 'chat.example',
        'username': 'mika',
        'display_name': 'Mika',
        'avatar_hash': null,
        'handle': '@mika@chat.example',
      },
      'member': <String, Object?>{
        'guild_id': '1',
        'guild_domain': 'chat.example',
        'user': <String, Object?>{
          'id': '4',
          'origin_domain': 'chat.example',
          'username': 'mika',
          'display_name': 'Mika',
          'avatar_hash': null,
          'handle': '@mika@chat.example',
        },
        'nickname': 'Host',
        'role_ids': <String>[],
      },
      'subscribed_at': '2027-01-01T01:00:00Z',
    };

Map<String, Object?> _webhookJson({
  String channelId = '2',
  Object? avatarHash = _defaultWebhookAvatar,
}) =>
    <String, Object?>{
      'id': '80',
      'guild_id': '1',
      'guild_domain': 'chat.example',
      'channel_id': channelId,
      'channel_domain': 'chat.example',
      'name': 'Release bot',
      'avatar_hash': avatarHash,
    };

const _defaultWebhookAvatar =
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';

KaedeRepository _repository(_QueueAdapter adapter) => KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );

final class _Reply {
  const _Reply(this.body, {this.status = 200});

  final String body;
  final int status;
}

final class _QueueAdapter implements HttpClientAdapter {
  _QueueAdapter(this._replies);

  final List<_Reply> _replies;
  final List<RequestOptions> requests = [];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    requests.add(options);
    final reply = _replies.removeAt(0);
    return ResponseBody.fromString(
      reply.body,
      reply.status,
      headers: <String, List<String>>{
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}
