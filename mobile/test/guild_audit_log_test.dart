import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/guild/guild_management_screen.dart';

void main() {
  test('audit repository sends every authoritative filter with pagination',
      () async {
    final adapter = _AuditAdapter();
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );

    await repository.auditLog(
      EntityRef.parse('1@chat.example'),
      before: '900',
      userId: EntityRef.parse('7@remote.example'),
      actionType: 111,
      targetType: 'thread',
      limit: 25,
    );

    expect(adapter.requests, hasLength(1));
    expect(
      adapter.requests.single.path,
      '/api/v1/guilds/1@chat.example/audit-logs',
    );
    expect(adapter.requests.single.queryParameters, <String, Object?>{
      'limit': 25,
      'before': '900',
      'user_id': '7@remote.example',
      'action_type': 111,
      'target_type': 'thread',
    });
  });

  test('audit actor member lookup sends search and federated pagination',
      () async {
    final adapter = _AuditAdapter();
    final repository = KaedeRepository(
      KaedeApiClient(
        vault: const SessionVault(),
        httpClient: Dio()..httpClientAdapter = adapter,
      ),
    );

    await repository.members(
      EntityRef.parse('1@chat.example'),
      query: 'remote moderator',
      after: EntityRef.parse('100@remote.example'),
    );

    expect(adapter.requests, hasLength(1));
    expect(
      adapter.requests.single.path,
      '/api/v1/guilds/1@chat.example/members',
    );
    expect(adapter.requests.single.queryParameters, <String, Object?>{
      'limit': 100,
      'query': 'remote moderator',
      'after': '100@remote.example',
    });
  });

  test('audit action filters retain Kaede target-specific meanings', () {
    expect(
      parseGuildAuditActionFilter('25|instance'),
      (actionType: 25, targetType: 'instance'),
    );
    expect(
      parseGuildAuditActionFilter('110|'),
      (actionType: 110, targetType: null),
    );
    expect(
      guildAuditActionFilterKey(<String, Object?>{
        'action_type': 11,
        'target_type': 'channel_order',
      }),
      '11|channel_order',
    );
    expect(
      guildAuditActionFilterKey(<String, Object?>{
        'action_type': 110,
        'target_type': 'thread',
      }),
      '110|',
    );
    expect(guildAuditActionFilterLabel('130|'), 'Soundboard sound created');
  });

  test('every audit filter and row label comes from one definition', () {
    for (final definition in guildAuditActionDefinitions) {
      final key = '${definition.actionType}|${definition.targetType ?? ''}';
      final item = <String, Object?>{
        'action_type': definition.actionType,
        if (definition.targetType != null) 'target_type': definition.targetType,
      };

      expect(guildAuditActionFilterOptions[key], definition.label);
      expect(guildAuditActionFilterLabel(key), definition.label);
      expect(guildAuditActionLabel(item), definition.label);
      expect(guildAuditActionFilterKey(item), key);
      expect(guildAuditActionDefinition(item), same(definition));
      expect(guildAuditActionTone(item), definition.tone);
    }
  });

  test('stage audit actions match backend codes and presentation', () {
    const cases = <({
      int actionType,
      String label,
      String verb,
      GuildAuditActionTone tone,
    })>[
      (
        actionType: 83,
        label: 'Stage instance created',
        verb: 'created',
        tone: GuildAuditActionTone.success,
      ),
      (
        actionType: 84,
        label: 'Stage instance updated',
        verb: 'updated',
        tone: GuildAuditActionTone.neutral,
      ),
      (
        actionType: 85,
        label: 'Stage instance deleted',
        verb: 'deleted',
        tone: GuildAuditActionTone.danger,
      ),
    ];

    for (final value in cases) {
      final item = <String, Object?>{
        'action_type': value.actionType,
        'target_type': 'stage_instance',
      };

      expect(
          guildAuditActionFilterOptions['${value.actionType}|'], value.label);
      expect(guildAuditActionFilterKey(item), '${value.actionType}|');
      expect(guildAuditActionLabel(item), value.label);
      expect(guildAuditActionTone(item), value.tone);
      expect(guildAuditActionIcon(item), Icons.record_voice_over_outlined);
      expect(
        guildAuditSummary(
          item,
          actorName: 'Moderator',
          targetName: 'the Stage instance',
        ),
        'Moderator ${value.verb} the Stage instance',
      );
    }
  });

  test('instance ban and unban retain distinct semantic tones', () {
    final banned = <String, Object?>{
      'action_type': 25,
      'target_type': 'instance',
    };
    final unbanned = <String, Object?>{
      'action_type': 26,
      'target_type': 'instance',
    };

    expect(guildAuditActionTone(banned), GuildAuditActionTone.danger);
    expect(guildAuditActionTone(unbanned), GuildAuditActionTone.success);
    expect(
      guildAuditActionTone(<String, Object?>{
        'action_type': 25,
        'target_type': 'member',
      }),
      GuildAuditActionTone.neutral,
    );
    expect(
      guildAuditActionTone(<String, Object?>{
        'action_type': 26,
        'target_type': 'member',
      }),
      GuildAuditActionTone.neutral,
    );
    expect(guildAuditActionIcon(banned), Icons.public_off_outlined);
    expect(guildAuditActionIcon(unbanned), Icons.public_off_outlined);
    expect(
      guildAuditSummary(
        banned,
        actorName: 'Moderator',
        targetName: 'remote.example',
      ),
      'Moderator banned remote.example',
    );
    expect(
      guildAuditSummary(
        unbanned,
        actorName: 'Moderator',
        targetName: 'remote.example',
      ),
      'Moderator unbanned remote.example',
    );
  });

  test('audit labels and targets cover advanced Discord-style categories', () {
    final guild = KaedeGuild.fromJson(<String, Object?>{
      'id': '1',
      'origin_domain': 'chat.example',
      'name': 'Kaede Guild',
      'owner_id': '7',
      'owner_domain': 'chat.example',
      'permissions': '0',
      'unavailable': false,
      'channels': const <Object?>[],
      'roles': const <Object?>[],
    });
    const users = <EntityRef, KaedeUser>{};

    expect(
      guildAuditActionLabel(<String, Object?>{'action_type': 21}),
      'Members pruned',
    );
    expect(
      guildAuditActionLabel(<String, Object?>{'action_type': 90}),
      'Sticker created',
    );
    expect(
      guildAuditActionLabel(<String, Object?>{'action_type': 110}),
      'Thread created',
    );
    expect(
      guildAuditActionLabel(<String, Object?>{'action_type': 130}),
      'Soundboard sound created',
    );
    expect(
      guildAuditActionLabel(<String, Object?>{'action_type': 140}),
      'AutoMod rule created',
    );

    expect(
      guildAuditTargetName(
        <String, Object?>{
          'action_type': 110,
          'target_type': 'thread',
          'target_ref': <String, Object?>{
            'id': '50',
            'name': 'release-notes',
          },
        },
        guild,
        users,
      ),
      '#release-notes',
    );
    expect(
      guildAuditTargetName(
        <String, Object?>{
          'action_type': 142,
          'target_type': 'auto_mod_rule',
          'target_ref': <String, Object?>{'id': '60'},
          'changes': <Object?>[
            <String, Object?>{'key': 'name', 'old_value': 'Link filter'},
          ],
        },
        guild,
        users,
      ),
      'AutoMod rule Link filter',
    );
    expect(
      guildAuditTargetName(
        <String, Object?>{
          'action_type': 130,
          'target_type': 'soundboard_sound',
          'target_ref': <String, Object?>{'id': '70', 'name': 'Air horn'},
        },
        guild,
        users,
      ),
      'sound Air horn',
    );
    expect(
      guildAuditTargetName(
        <String, Object?>{
          'action_type': 21,
          'target_type': 'member_prune',
          'target_ref': <String, Object?>{'members_removed': 12},
        },
        guild,
        users,
      ),
      '12 inactive members',
    );
  });
}

final class _AuditAdapter implements HttpClientAdapter {
  final List<RequestOptions> requests = <RequestOptions>[];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    requests.add(options);
    return ResponseBody.fromString(
      '[]',
      200,
      headers: <String, List<String>>{
        Headers.contentTypeHeader: <String>[Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}
