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
  test('audit-only invite summaries do not invent management metadata', () {
    expect(
      inviteSummaryLine(<String, Object?>{'expires_at': null}),
      'never expires',
    );
  });

  test('sticker fields follow Discord length and serialized tag limits', () {
    expect(validStickerName('Friendly Wave'), isTrue);
    expect(validStickerName('x'), isFalse);
    expect(validStickerName(List.filled(31, 'x').join()), isFalse);
    expect(validStickerDescription(''), isTrue);
    expect(validStickerDescription('x'), isFalse);
    expect(validStickerDescription('A friendly wave'), isTrue);
    expect(normalizedStickerTags('wave\nhello'), ['wave', 'hello']);
    expect(normalizedStickerTags('wave\nwave'), isNull);
    expect(
      normalizedStickerTags(
        '${List.filled(100, 'x').join()}\n${List.filled(100, 'y').join()}',
      ),
      isNull,
    );
  });

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

    testWidgets('voice Channel Settings uses the authority region catalog',
        (tester) async {
      GuildChannelDraft? result;
      final channel = KaedeChannel(
        ref: EntityRef.parse('12@guild.example'),
        guildRef: EntityRef.parse('1@guild.example'),
        name: 'Lounge',
        type: ChannelType.voice,
        position: 0,
        permissions: BigInt.zero,
      );

      await tester.pumpWidget(MaterialApp(
        theme: kaedeTheme(),
        home: Builder(
          builder: (context) => Scaffold(
            body: FilledButton(
              onPressed: () async {
                result = await showGuildChannelEditorSheet(
                  context,
                  channel: channel,
                  loadVoiceRegions: () async => const <VoiceRegion>[
                    VoiceRegion(
                      id: 'sydney',
                      name: 'Sydney',
                      optimal: true,
                      deprecated: false,
                      custom: false,
                    ),
                  ],
                );
              },
              child: const Text('Edit voice channel'),
            ),
          ),
        ),
      ));

      await tester.tap(find.text('Edit voice channel'));
      await tester.pumpAndSettle();
      final selector =
          find.byKey(const ValueKey('voice-region-override-field'));
      await tester.ensureVisible(selector);
      await tester.tap(selector);
      await tester.pumpAndSettle();
      expect(find.text('Sydney — Recommended'), findsOneWidget);
      await tester.tap(find.text('Sydney — Recommended').last);
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('save-channel-button')));
      await tester.pumpAndSettle();

      expect(result?.rtcRegion, 'sydney');
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

    test('age restriction serializes only for supported text surfaces', () {
      const text = GuildChannelDraft(
        name: 'adult-chat',
        topic: '',
        nsfw: true,
        type: ChannelType.text,
        slowModeSeconds: 0,
      );
      const voice = GuildChannelDraft(
        name: 'voice',
        topic: '',
        nsfw: true,
        type: ChannelType.voice,
        slowModeSeconds: 0,
      );

      expect(text.json, containsPair('nsfw', true));
      expect(voice.json, containsPair('nsfw', false));
    });

    test('reorder payload keeps local IDs and omits unchanged parents', () {
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
        guildChannelPositionRequest(
          <KaedeChannel>[category, child],
          <KaedeChannel>[child, category],
          movedRef: child.ref,
        ),
        <Map<String, Object?>>[
          <String, Object?>{'id': '12', 'position': 0},
        ],
      );
    });

    test('reorder payload includes only actual parent changes', () {
      final category = KaedeChannel(
        ref: EntityRef.parse('11@chat.example'),
        guildRef: EntityRef.parse('1@chat.example'),
        type: ChannelType.category,
        position: 0,
        permissions: BigInt.zero,
      );
      final ungrouped = KaedeChannel(
        ref: EntityRef.parse('12@chat.example'),
        guildRef: EntityRef.parse('1@chat.example'),
        type: ChannelType.text,
        position: 1,
        permissions: BigInt.zero,
      );
      final moved = KaedeChannel(
        ref: ungrouped.ref,
        guildRef: ungrouped.guildRef,
        parentRef: category.ref,
        type: ungrouped.type,
        position: ungrouped.position,
        permissions: ungrouped.permissions,
      );

      expect(
        guildChannelPositionRequest(
          <KaedeChannel>[category, ungrouped],
          <KaedeChannel>[category, moved],
          movedRef: moved.ref,
        ),
        <Map<String, Object?>>[
          <String, Object?>{'id': '12', 'position': 1, 'parent_id': '11'},
        ],
      );
      expect(
        guildChannelPositionRequest(
          <KaedeChannel>[category, moved],
          <KaedeChannel>[category, ungrouped],
          movedRef: ungrouped.ref,
        ),
        <Map<String, Object?>>[
          <String, Object?>{'id': '12', 'position': 1, 'parent_id': null},
        ],
      );
    });

    test('reorder omits unrelated channels shifted by the moved row', () {
      KaedeChannel channel(String id, ChannelType type, int position,
              {EntityRef? parent}) =>
          KaedeChannel(
            ref: EntityRef.parse('$id@chat.example'),
            guildRef: EntityRef.parse('1@chat.example'),
            parentRef: parent,
            type: type,
            position: position,
            permissions: BigInt.zero,
          );
      final deniedCategory = channel('20', ChannelType.category, 0);
      final deniedChild = channel(
        '21',
        ChannelType.text,
        1,
        parent: deniedCategory.ref,
      );
      final moved = channel('22', ChannelType.text, 2);

      expect(
        guildChannelPositionRequest(
          <KaedeChannel>[deniedCategory, deniedChild, moved],
          <KaedeChannel>[moved, deniedCategory, deniedChild],
          movedRef: moved.ref,
        ),
        <Map<String, Object?>>[
          <String, Object?>{'id': '22', 'position': 0},
        ],
      );
    });

    test('moving a category carries only that category and its children', () {
      KaedeChannel channel(String id, ChannelType type, int position,
              {EntityRef? parent}) =>
          KaedeChannel(
            ref: EntityRef.parse('$id@chat.example'),
            guildRef: EntityRef.parse('1@chat.example'),
            parentRef: parent,
            type: type,
            position: position,
            permissions: BigInt.zero,
          );
      final unrelated = channel('30', ChannelType.category, 0);
      final category = channel('31', ChannelType.category, 1);
      final child = channel(
        '32',
        ChannelType.text,
        2,
        parent: category.ref,
      );

      expect(
        guildChannelPositionRequest(
          <KaedeChannel>[unrelated, category, child],
          <KaedeChannel>[category, child, unrelated],
          movedRef: category.ref,
        ),
        <Map<String, Object?>>[
          <String, Object?>{'id': '31', 'position': 0},
          <String, Object?>{'id': '32', 'position': 1},
        ],
      );
    });

    test('invite and webhook targets include their supported channel types',
        () {
      KaedeChannel channel(
        String id,
        ChannelType type,
        int position, {
        int permissions = 0,
      }) =>
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
              ChannelType.stage => 13,
              ChannelType.forum => 15,
              ChannelType.tracker => 17,
              _ => -1,
            },
            'position': position,
            'permissions': '$permissions',
            'name': 'channel-$id',
          });

      final targets = guildTextChannelTargets(<KaedeChannel>[
        channel('2', ChannelType.voice, 0),
        channel('3', ChannelType.announcement, 2),
        channel('4', ChannelType.category, 1),
        channel('5', ChannelType.text, 1),
        channel('6', ChannelType.forum, 3),
      ]);

      expect(targets.map((channel) => channel.ref.id.value),
          <String>['5', '3', '6']);

      final creatable = guildInviteCreationTargets(<KaedeChannel>[
        channel('7', ChannelType.text, 0),
        channel(
          '8',
          ChannelType.announcement,
          1,
          permissions: Permission.createInvite,
        ),
        channel(
          '9',
          ChannelType.forum,
          2,
          permissions: Permission.administrator,
        ),
        channel(
          '10',
          ChannelType.voice,
          3,
          permissions: Permission.createInvite,
        ),
        channel(
          '11',
          ChannelType.stage,
          4,
          permissions: Permission.createInvite,
        ),
        channel(
          '12',
          ChannelType.tracker,
          5,
          permissions: Permission.createInvite,
        ),
        channel('13', ChannelType.voice, 6),
      ], isOwner: false);
      expect(
        creatable.map((channel) => channel.ref.id.value),
        <String>['8', '9', '10', '11', '12'],
      );
    });
  });

  group('webhook management parity', () {
    KaedeChannel channel(
      String id,
      String name,
      ChannelType type,
      int position,
    ) =>
        KaedeChannel.fromJson(<String, Object?>{
          'id': id,
          'origin_domain': 'chat.example',
          'guild_id': '1',
          'guild_domain': 'chat.example',
          'type': switch (type) {
            ChannelType.text => 0,
            ChannelType.announcement => 5,
            ChannelType.forum => 15,
            _ => -1,
          },
          'position': position,
          'permissions': '0',
          'name': name,
        });

    testWidgets('webhook editor can move a webhook into a forum channel',
        (tester) async {
      WebhookSettingsDraft? result;
      final channels = <KaedeChannel>[
        channel('2', 'general', ChannelType.text, 0),
        channel('3', 'announcements', ChannelType.announcement, 1),
        channel('4', 'help', ChannelType.forum, 2),
      ];
      await tester.pumpWidget(MaterialApp(
        theme: kaedeTheme(),
        home: Builder(
          builder: (context) => Scaffold(
            body: FilledButton(
              onPressed: () async {
                result = await showWebhookSettingsEditor(
                  context,
                  webhook: <String, Object?>{
                    'name': 'Release bot',
                    'channel_id': '2',
                    'channel_domain': 'chat.example',
                  },
                  channels: channels,
                  fallbackDomain: Domain('chat.example'),
                );
              },
              child: const Text('Edit webhook'),
            ),
          ),
        ),
      ));

      await tester.tap(find.text('Edit webhook'));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const Key('webhook-name-field')),
        'Forum helper',
      );
      await tester.tap(find.byKey(const Key('webhook-channel-field')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Forum · help').last);
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('save-webhook-settings')));
      await tester.pumpAndSettle();

      expect(result?.name, 'Forum helper');
      expect(result?.channel.wire, '4@chat.example');
    });

    testWidgets('webhook avatar replacement shows a preview and disclosure',
        (tester) async {
      final directory = Directory.systemTemp.createTempSync('kaede-webhook-');
      addTearDown(() => directory.deleteSync(recursive: true));
      final file = File('${directory.path}/avatar.png');
      file.writeAsBytesSync(base64Decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
      ));
      bool? accepted;
      await tester.pumpWidget(MaterialApp(
        theme: kaedeTheme(),
        home: Builder(
          builder: (context) => Scaffold(
            body: FilledButton(
              onPressed: () async {
                accepted = await showWebhookAvatarPreviewConfirmation(
                  context,
                  file: file,
                  webhookName: 'Release bot',
                  replacing: true,
                );
              },
              child: const Text('Choose avatar'),
            ),
          ),
        ),
      ));

      await tester.tap(find.text('Choose avatar'));
      await tester.pumpAndSettle();
      expect(
        find.byKey(const Key('webhook-avatar-preview')),
        findsOneWidget,
      );
      expect(find.textContaining('safety scanning'), findsOneWidget);
      await tester.tap(find.byKey(const Key('confirm-webhook-avatar')));
      await tester.pumpAndSettle();

      expect(accepted, isTrue);
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

  test('channel management gates use each channel effective permission mask',
      () {
    final managed = KaedeChannel(
      ref: EntityRef.parse('10@chat.example'),
      guildRef: EntityRef.parse('1@chat.example'),
      type: ChannelType.text,
      position: 0,
      permissions: BigInt.from(
        Permission.manageChannels |
            Permission.manageRoles |
            Permission.manageWebhooks,
      ),
      name: 'managed',
    );
    final guild = KaedeGuild(
      ref: EntityRef.parse('1@chat.example'),
      name: 'Guild',
      ownerRef: EntityRef.parse('2@chat.example'),
      permissions: BigInt.zero,
      unavailable: false,
      channels: <KaedeChannel>[managed],
    );

    expect(guild.allows(Permission.manageChannels), isFalse);
    expect(
      guildHasEffectiveChannelPermission(
        guild,
        Permission.manageChannels,
        isOwner: false,
      ),
      isTrue,
    );
    expect(
      canManageEffectiveChannel(
        managed,
        Permission.manageRoles,
        isOwner: false,
      ),
      isTrue,
    );
  });

  test('webhook management targets stay channel-scoped and plaintext', () {
    KaedeChannel channel(
      String id,
      ChannelType type,
      int permissions, {
      String encryption = 'plaintext',
    }) =>
        KaedeChannel(
          ref: EntityRef.parse('$id@chat.example'),
          guildRef: EntityRef.parse('1@chat.example'),
          type: type,
          position: int.parse(id),
          permissions: BigInt.from(permissions),
          encryptionMode: encryption,
          name: 'channel-$id',
        );

    final allowed = channel('10', ChannelType.text, Permission.manageWebhooks);
    final encrypted = channel(
      '11',
      ChannelType.text,
      Permission.manageWebhooks,
      encryption: 'e2ee',
    );
    final denied = channel('12', ChannelType.forum, Permission.viewChannel);
    final voice = channel('13', ChannelType.voice, Permission.manageWebhooks);

    expect(
      guildWebhookManagementTargets(
        <KaedeChannel>[allowed, encrypted, denied, voice],
        isOwner: false,
      ),
      <KaedeChannel>[allowed],
    );
  });

  test('overwrite controls enforce the held-bit ceiling and show dependencies',
      () {
    final held = BigInt.from(Permission.viewChannel | Permission.manageRoles);

    expect(
      channelOverwritePermissionCanChange(held, Permission.manageRoles),
      isTrue,
    );
    expect(
      channelOverwritePermissionCanChange(held, Permission.sendMessages),
      isFalse,
    );
    expect(
      channelOverwriteCanReset(
        BigInt.from(Permission.viewChannel),
        BigInt.zero,
        held,
      ),
      isTrue,
    );
    expect(
      channelOverwriteCanReset(
        BigInt.from(Permission.sendMessages),
        BigInt.zero,
        held,
      ),
      isFalse,
    );

    final useVad = permissionMetadata
        .firstWhere((permission) => permission.bit == Permission.useVad);
    expect(
      channelPermissionDependencyLabels(useVad),
      <String>['Speak'],
    );
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
