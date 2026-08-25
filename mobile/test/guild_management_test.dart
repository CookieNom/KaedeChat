import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/guild/guild_management_screen.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

void main() {
  testWidgets('sticker editor crops and enables optional background removal',
      (tester) async {
    final directory = Directory.systemTemp.createTempSync('kaede-sticker-');
    addTearDown(() => directory.deleteSync(recursive: true));
    final file = File('${directory.path}/sticker.png');
    file.writeAsBytesSync(base64Decode(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
    ));
    StickerEdit? result;

    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: Builder(
        builder: (context) => Scaffold(
          body: FilledButton(
            onPressed: () async {
              result = await showStickerEditor(
                context,
                file: file,
                animated: false,
                backgroundRemovalAvailable: true,
              );
            },
            child: const Text('Open sticker editor'),
          ),
        ),
      ),
    ));

    await tester.tap(find.text('Open sticker editor'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.byKey(const ValueKey('sticker-crop-preview')), findsOneWidget);
    final southeast =
        find.byKey(const ValueKey('sticker-crop-handle-southeast'));
    await tester.ensureVisible(southeast);
    await tester.drag(southeast, const Offset(-60, -50));
    await tester.pump();
    expect(
      tester
          .widget<Text>(find.byKey(const ValueKey('sticker-crop-summary')))
          .data,
      isNot('Selection: 100% × 100%'),
    );
    final selection = find.byKey(const ValueKey('sticker-crop-selection'));
    await tester.drag(selection, const Offset(20, 15));
    await tester.pump();
    await tester.enterText(
        find.byKey(const ValueKey('sticker-name')), 'party_blob');
    final removeBackground =
        find.byKey(const ValueKey('sticker-remove-background'));
    final removeBackgroundTile =
        tester.widget<SwitchListTile>(removeBackground);
    expect(removeBackgroundTile.onChanged, isNotNull);
    removeBackgroundTile.onChanged!(true);
    await tester.pump();
    await tester.tap(find.text('Create'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));

    expect(result?.name, 'party_blob');
    expect(result?.removeBackground, isTrue);
    expect(result?.cropWidth, lessThan(1));
    expect(result?.cropHeight, lessThan(1));
    expect(result?.cropX, greaterThan(0));
    expect(result?.cropY, greaterThan(0));
    expect(tester.takeException(), isNull);
  });

  group('guild channel drafts', () {
    testWidgets('editor stays usable above a phone keyboard', (tester) async {
      tester.view.physicalSize = const Size(1080, 2160);
      tester.view.devicePixelRatio = 3;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetViewInsets);
      GuildChannelDraft? result;

      await tester.pumpWidget(MaterialApp(
        theme: kaedeTheme(),
        home: Builder(
          builder: (context) => Scaffold(
            body: Center(
              child: FilledButton(
                onPressed: () async {
                  result = await showGuildChannelEditorSheet(context);
                },
                child: const Text('Open editor'),
              ),
            ),
          ),
        ),
      ));

      await tester.tap(find.text('Open editor'));
      await tester.pumpAndSettle();
      tester.view.viewInsets = const FakeViewPadding(bottom: 900);
      await tester.pumpAndSettle();

      expect(find.byKey(const ValueKey('channel-name-field')), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.enterText(
          find.byKey(const ValueKey('channel-name-field')), 'mobile-room');
      final saveButton = find.byKey(const ValueKey('save-channel-button'));
      await tester.tap(saveButton);
      await tester.pumpAndSettle();

      expect(result?.name, 'mobile-room');
      expect(tester.takeException(), isNull);
    });

    testWidgets('editor scrolls instead of overflowing in short landscape',
        (tester) async {
      tester.view.physicalSize = const Size(360, 320);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetViewInsets);

      await tester.pumpWidget(MaterialApp(
        theme: kaedeTheme(),
        home: Builder(
          builder: (context) => Scaffold(
            body: FilledButton(
              onPressed: () => showGuildChannelEditorSheet(context),
              child: const Text('Open editor'),
            ),
          ),
        ),
      ));

      await tester.tap(find.text('Open editor'));
      await tester.pumpAndSettle();
      tester.view.viewInsets = const FakeViewPadding(bottom: 260);
      await tester.pumpAndSettle();

      expect(find.byKey(const ValueKey('channel-name-field')), findsOneWidget);
      expect(tester.takeException(), isNull);
    });

    test('includes the selected category in create and edit requests', () {
      final parent = EntityRef(Snowflake('12'), Domain('chat.example'));
      final draft = GuildChannelDraft(
        name: 'Lounge',
        topic: 'Drop in and talk',
        type: ChannelType.voice,
        slowModeSeconds: 10,
        parentRef: parent,
      );

      expect(
        draft.json,
        containsPair('parent_id', parent.id.value),
      );
      expect(draft.json, containsPair('type', 2));
      expect(draft.json, containsPair('rate_limit_per_user', 10));
    });

    test('categories cannot retain a parent, topic, or slow mode', () {
      final draft = GuildChannelDraft(
        name: 'Projects',
        topic: 'ignored',
        type: ChannelType.category,
        slowModeSeconds: 60,
        parentRef: EntityRef(Snowflake('12'), Domain('chat.example')),
      );

      expect(draft.json, containsPair('parent_id', null));
      expect(draft.json, containsPair('topic', null));
      expect(draft.json, containsPair('rate_limit_per_user', 0));
    });

    test('reorder payload keeps local IDs and every category parent', () {
      final category = KaedeChannel(
        ref: EntityRef.parse('11@chat.example'),
        guildRef: EntityRef.parse('1@chat.example'),
        type: ChannelType.category,
        position: 0,
        permissions: BigInt.zero,
      );
      final child = KaedeChannel(
        ref: EntityRef.parse('12@chat.example'),
        guildRef: EntityRef.parse('1@chat.example'),
        parentRef: category.ref,
        type: ChannelType.text,
        position: 1,
        permissions: BigInt.zero,
      );

      expect(
        guildChannelPositionRequest(<KaedeChannel>[category, child]),
        <Map<String, Object?>>[
          <String, Object?>{'id': '11', 'position': 0, 'parent_id': null},
          <String, Object?>{'id': '12', 'position': 1, 'parent_id': '11'},
        ],
      );
    });

    test('invite and webhook pickers include text and announcement channels',
        () {
      KaedeChannel channel(String id, ChannelType type, int position) =>
          KaedeChannel.fromJson(<String, Object?>{
            'id': id,
            'origin_domain': 'chat.example',
            'guild_id': '1',
            'guild_domain': 'chat.example',
            'type': switch (type) {
              ChannelType.text => 0,
              ChannelType.voice => 2,
              ChannelType.category => 4,
              ChannelType.announcement => 5,
              _ => -1,
            },
            'position': position,
            'name': 'channel-$id',
          });

      final targets = guildTextChannelTargets(<KaedeChannel>[
        channel('2', ChannelType.voice, 0),
        channel('3', ChannelType.announcement, 2),
        channel('4', ChannelType.category, 1),
        channel('5', ChannelType.text, 1),
      ]);

      expect(
          targets.map((channel) => channel.ref.id.value), <String>['5', '3']);
    });
  });

  group('role permission parity', () {
    test('base role editor includes text and voice channel grants', () {
      final permissions = guildRolePermissionMetadata();
      final bits = permissions.map((permission) => permission.bit).toSet();

      expect(bits, contains(Permission.manageGuild));
      expect(bits, contains(Permission.sendMessages));
      expect(bits, contains(Permission.connect));
      expect(bits, contains(Permission.speak));
      expect(bits, contains(Permission.stream));
      expect(bits, contains(Permission.moveMembers));
      expect(
        permissions.map((permission) => permission.group).toSet(),
        containsAll(<String>['Text', 'Voice', 'Voice moderation']),
      );
    });

    test('permission search finds voice grants instead of empty headings', () {
      final matches = guildRolePermissionMetadata('voice');

      expect(matches, isNotEmpty);
      expect(matches.any((permission) => permission.bit == Permission.connect),
          isTrue);
      expect(
        matches.every((permission) =>
            '${permission.label} ${permission.description}'
                .toLowerCase()
                .contains('voice')),
        isTrue,
      );
    });
  });

  test('channel overwrite requests use the backend target_type contract', () {
    final request = channelOverwriteRequest(
      target: EntityRef.parse('42@chat.example'),
      targetType: 'member',
      allow: BigInt.from(Permission.viewChannel),
      deny: BigInt.from(Permission.sendMessages),
    );

    expect(request, containsPair('target_id', '42@chat.example'));
    expect(request, containsPair('target_type', 'member'));
    expect(request, isNot(contains('type')));
  });

  test('saved and removed overwrites update the local editor snapshot', () {
    final target = EntityRef.parse('42@chat.example');
    final domain = Domain('chat.example');
    final original = <Map<String, Object?>>[
      <String, Object?>{
        'target_id': '42',
        'target_domain': 'chat.example',
        'target_type': 'member',
        'allow': '1',
        'deny': '0',
      },
    ];

    final saved = upsertChannelOverwrite(
      original,
      target: target,
      targetType: 'member',
      allow: BigInt.from(8),
      deny: BigInt.from(16),
      defaultDomain: domain,
    );
    expect(saved, hasLength(1));
    expect(saved.single, containsPair('allow', '8'));
    expect(saved.single, containsPair('deny', '16'));
    expect(
      channelOverwriteMatches(
        saved.single,
        target: target,
        targetType: 'member',
        defaultDomain: domain,
      ),
      isTrue,
    );

    final removed = removeChannelOverwrite(
      saved,
      target: target,
      targetType: 'member',
      defaultDomain: domain,
    );
    expect(removed, isEmpty);
  });

  test('overwrite matching accepts composite target IDs from older payloads',
      () {
    expect(
      channelOverwriteMatches(
        <String, Object?>{
          'target_id': '42@chat.example',
          'type': 'role',
        },
        target: EntityRef.parse('42@chat.example'),
        targetType: 'role',
        defaultDomain: Domain('fallback.example'),
      ),
      isTrue,
    );
  });

  test('role reorder uses local IDs and requires concurrency versions', () {
    KaedeRole role(String id, String? version) => KaedeRole(
          ref: EntityRef.parse('$id@chat.example'),
          guildRef: EntityRef.parse('1@chat.example'),
          name: 'Role $id',
          position: int.parse(id),
          permissions: BigInt.zero,
          color: 0,
          hoist: false,
          mentionable: false,
          version: version,
        );

    expect(
      guildRolePositionRequest(<KaedeRole>[
        role('2', 'v2'),
        role('3', 'v3'),
      ]),
      <Map<String, Object?>>[
        <String, Object?>{'id': '2', 'position': 1, 'version': 'v2'},
        <String, Object?>{'id': '3', 'position': 2, 'version': 'v3'},
      ],
    );
    expect(
      () => guildRolePositionRequest(<KaedeRole>[role('2', null)]),
      throwsStateError,
    );
  });

  test('member role selection normalizes bare and composite payload IDs', () {
    expect(
      normalizedMemberRoleIds(<String>[
        '2',
        '3@chat.example',
        '2@chat.example',
      ]),
      <String>{'2', '3'},
    );
  });

  test('guild history policy survives model round trips', () {
    final guild = KaedeGuild.fromJson(<String, Object?>{
      'id': '1',
      'origin_domain': 'chat.example',
      'name': 'History guild',
      'owner_id': '2',
      'owner_domain': 'chat.example',
      'permissions': '0',
      'unavailable': false,
      'channels': const <Object?>[],
      'roles': const <Object?>[],
      'federated_history_policy': 'full_retained',
    });

    expect(guild.federatedHistoryPolicy, 'full_retained');
    expect(
      guild.toJson(),
      containsPair('federated_history_policy', 'full_retained'),
    );
  });

  test('instance-ban and audit helpers use authoritative payload keys', () {
    expect(
      guildInstanceBanDomain(<String, Object?>{
        'instance_domain': 'REMOTE.EXAMPLE.',
      })?.value,
      'remote.example',
    );
    expect(
      guildInstanceBanDomain(<String, Object?>{'domain': 'legacy.example'}),
      isNull,
    );
    expect(
      guildAuditActionLabel(<String, Object?>{
        'action_type': 'guild.channel.create',
      }),
      'Channel created',
    );
    expect(
      guildAuditActionLabel(<String, Object?>{
        'action_type': 25,
        'target_type': 'instance',
      }),
      'Instance banned',
    );
  });

  test('audit helpers build readable summaries and change details', () {
    final item = <String, Object?>{
      'action_type': 11,
      'target_type': 'channel',
      'changes': <Object?>[
        <String, Object?>{
          'key': 'rate_limit_per_user',
          'old_value': 0,
          'new_value': 15,
        },
      ],
    };

    expect(
      guildAuditSummary(
        item,
        actorName: 'Kaede',
        targetName: '#general',
      ),
      'Kaede updated #general',
    );
    expect(guildAuditChanges(item), hasLength(1));
    expect(guildAuditFieldLabel('rate_limit_per_user'), 'Rate limit per user');
    expect(
      guildAuditChangeDescription(guildAuditChanges(item).single),
      '0 → 15',
    );
    expect(
      guildAuditRelativeTime(
        DateTime.utc(2026, 8, 21, 10),
        now: DateTime.utc(2026, 8, 21, 12),
      ),
      '2 hours ago',
    );
  });
}
