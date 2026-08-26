import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/role_colors.dart';
import 'package:kaede_mobile/src/features/chat/channel_view.dart';
import 'package:kaede_mobile/src/features/guild/guild_management_screen.dart';
import 'package:kaede_mobile/src/features/home/mobile_shell.dart';
import 'package:kaede_mobile/src/features/shared/remote_media.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

KaedeUser _user(
        {String name = 'Maple', String handle = '@maple@home.example'}) =>
    KaedeUser(
      ref: EntityRef.parse('7@home.example'),
      username: 'maple',
      handle: handle,
      displayName: name,
      profileResolved: true,
    );

KaedeChannel _channel({
  EntityRef? guild,
  String? name,
  String conversationType = 'dm',
  List<KaedeUser> recipients = const <KaedeUser>[],
}) =>
    KaedeChannel(
      ref: EntityRef.parse('10@home.example'),
      guildRef: guild,
      type: guild == null ? ChannelType.dm : ChannelType.text,
      name: name,
      conversationType: conversationType,
      recipients: recipients,
      position: 0,
      permissions: BigInt.zero,
    );

void main() {
  group('transcript day dividers', () {
    final now = DateTime(2026, 8, 20, 12);

    test('recent days read as words', () {
      expect(transcriptDayLabel(now, now: now), 'Today');
      expect(
        transcriptDayLabel(DateTime(2026, 8, 19, 23, 59), now: now),
        'Yesterday',
      );
      expect(
        transcriptDayLabel(DateTime(2026, 8, 17), now: now),
        'Monday',
      );
    });

    test('older days fall back to dates and include the year when needed', () {
      expect(
        transcriptDayLabel(DateTime(2026, 3, 4), now: now),
        'March 4',
      );
      expect(
        transcriptDayLabel(DateTime(2025, 12, 31), now: now),
        'December 31, 2025',
      );
    });

    test('day boundaries are compared in local time, not UTC', () {
      final first = DateTime(2026, 8, 20, 23, 58);
      final second = DateTime(2026, 8, 21, 0, 2);
      expect(sameCalendarDay(first, first.add(const Duration(minutes: 1))),
          isTrue);
      expect(sameCalendarDay(first, second), isFalse);
    });
  });

  group('deep links match the web routes', () {
    final message = EntityRef.parse('99@home.example');

    test('guild channels link through /g/<guild>/<channel>', () {
      final link = messageLink(
        instance: 'kaede.chat',
        channel: _channel(
          guild: EntityRef.parse('20@home.example'),
          name: 'general',
        ),
        message: message,
      );
      expect(
        link,
        'https://kaede.chat/g/20%40home.example/10%40home.example'
        '?around=99%40home.example',
      );
    });

    test('conversations link through /home/<channel>', () {
      expect(
        messageLink(
          instance: 'kaede.chat',
          channel: _channel(),
          message: message,
        ),
        'https://kaede.chat/home/10%40home.example?around=99%40home.example',
      );
    });
  });

  group('composer placeholder names the destination', () {
    test('guild channels use the hash form', () {
      expect(
        composerHint(
          _channel(guild: EntityRef.parse('20@home.example'), name: 'general'),
        ),
        'Message #general',
      );
    });

    test('conversations use the recipient or group name', () {
      expect(
        composerHint(_channel(recipients: <KaedeUser>[_user()])),
        'Message Maple',
      );
      expect(
        composerHint(_channel(conversationType: 'group', name: 'Study group')),
        'Message Study group',
      );
      expect(composerHint(_channel(conversationType: 'group')),
          'Message the group');
    });
  });

  group('attachment sizes stay readable', () {
    test('scales the unit to the value', () {
      expect(formatAttachmentSize(512), '512 B');
      expect(formatAttachmentSize(2048), '2 KB');
      expect(formatAttachmentSize(5 * 1024 * 1024), '5.0 MB');
      expect(formatAttachmentSize(64 * 1024 * 1024), '64 MB');
      expect(formatAttachmentSize(3 * 1024 * 1024 * 1024), '3.0 GB');
    });
  });

  group('guild initials', () {
    test('prefers one letter per word', () {
      expect(guildInitials('Maple Syrup'), 'MS');
      expect(guildInitials('kaede-dev'), 'KD');
      expect(guildInitials('Kaede'), 'KA');
      expect(guildInitials('   '), '?');
    });
  });

  group('presence wording is shared across surfaces', () {
    test('every status has a label', () {
      expect(presenceLabel(PresenceStatus.online), 'Online');
      expect(presenceLabel(PresenceStatus.idle), 'Idle');
      expect(presenceLabel(PresenceStatus.dnd), 'Do not disturb');
      expect(presenceLabel(PresenceStatus.invisible), 'Invisible');
      expect(presenceLabel(PresenceStatus.offline), 'Offline');
    });
  });

  group('layout holds on small phones', () {
    Future<void> pump(WidgetTester tester, Widget child) async {
      tester.view.physicalSize = const Size(320, 640);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      await tester.pumpWidget(MaterialApp(
        theme: kaedeTheme(),
        home: Scaffold(body: child),
      ));
      await tester.pump();
    }

    testWidgets('conversation headers keep the title legible beside actions',
        (tester) async {
      await pump(
        tester,
        ConversationCompactHeader(
          leading: const BackButton(),
          avatar: UserAvatar(
            user: _user(),
            radius: 17,
            presence: PresenceStatus.online,
          ),
          title: '#a-rather-long-channel-name',
          subtitle: 'Encrypted · identities unverified',
          actions: const [
            Icon(Icons.lock_rounded),
            Icon(Icons.push_pin_outlined),
            Icon(Icons.search_rounded),
          ],
        ),
      );

      expect(tester.takeException(), isNull);
      expect(
        tester.getSize(find.text('#a-rather-long-channel-name')).width,
        greaterThanOrEqualTo(60),
      );
    });

    testWidgets('avatars with presence stay square', (tester) async {
      await pump(
        tester,
        Center(
          child: UserAvatar(
            user: _user(),
            radius: 20,
            presence: PresenceStatus.idle,
          ),
        ),
      );

      expect(tester.takeException(), isNull);
      expect(tester.getSize(find.byType(UserAvatar)), const Size(40, 40));
    });

    testWidgets('the profile sheet renders without overflow', (tester) async {
      await pump(
        tester,
        UserProfileSheet(
          user: _user(),
          presence: PresenceStatus.dnd,
          memberOf: 'Maple Syrup',
          actions: [
            FilledButton(onPressed: () {}, child: const Text('Message')),
          ],
        ),
      );

      expect(tester.takeException(), isNull);
      expect(find.text('Maple'), findsOneWidget);
      expect(find.text('Do not disturb'), findsOneWidget);
      expect(find.text('Message'), findsOneWidget);
    });
  });

  group('member roster grouping matches the web client', () {
    KaedeUser person(String id, String name) => KaedeUser(
          ref: EntityRef.parse('$id@home.example'),
          username: name.toLowerCase(),
          handle: '@${name.toLowerCase()}@home.example',
          displayName: name,
          profileResolved: true,
        );

    KaedeRole role(String id, String name, int position,
            {bool hoist = true, String? iconHash}) =>
        KaedeRole(
          ref: EntityRef.parse('$id@home.example'),
          guildRef: EntityRef.parse('900@home.example'),
          name: name,
          iconHash: iconHash,
          color: 0,
          permissions: BigInt.zero,
          position: position,
          hoist: hoist,
          mentionable: false,
        );

    test('the highest assigned role icon wins independently of role color', () {
      final lower =
          role('10', 'Lower', 2, iconHash: List<String>.filled(64, 'a').join());
      final higherWithoutIcon = role('11', 'Higher', 5);
      final highestIcon =
          role('12', 'Guard', 4, iconHash: List<String>.filled(64, 'b').join());
      final guild = KaedeGuild(
        ref: EntityRef.parse('900@home.example'),
        name: 'Home',
        ownerRef: EntityRef.parse('1@home.example'),
        permissions: BigInt.zero,
        unavailable: false,
        roles: [lower, higherWithoutIcon, highestIcon],
      );
      final member = GuildMember(
        user: person('1', 'Ada'),
        roleIds: const ['10', '11', '12'],
      );

      expect(highestIconRole(guild, member), same(highestIcon));
    });

    test('hoisted roles come first, then online, then offline', () {
      final admin = role('10', 'Admins', 3);
      final mods = role('11', 'Mods', 2);
      final plain = role('12', 'Members', 1, hoist: false);
      final presence = <String, PresenceStatus>{
        '1@home.example': PresenceStatus.online,
        '2@home.example': PresenceStatus.idle,
        '3@home.example': PresenceStatus.online,
        '4@home.example': PresenceStatus.offline,
      };

      final sections = groupGuildMembers(
        members: [
          GuildMember(user: person('1', 'Ada'), roleIds: const ['10', '11']),
          GuildMember(user: person('2', 'Ben'), roleIds: const ['11']),
          GuildMember(user: person('3', 'Cleo'), roleIds: const ['12']),
          GuildMember(user: person('4', 'Dana'), roleIds: const ['10']),
        ],
        roles: [admin, mods, plain],
        presenceFor: (user) =>
            presence[user.ref.wire] ?? PresenceStatus.offline,
      );

      expect(
        sections.map((section) => section.title).toList(),
        ['Admins', 'Mods', 'Online', 'Offline'],
      );
      // A member only appears under their highest hoisted role.
      expect(sections.first.members.single.user.name, 'Ada');
      expect(sections[1].members.single.user.name, 'Ben');
      expect(sections[2].members.single.user.name, 'Cleo');
      expect(sections.last.members.single.user.name, 'Dana');
      expect(sections.last.offline, isTrue);
    });

    test('an offline member never lands in a hoisted section', () {
      final sections = groupGuildMembers(
        members: [
          GuildMember(user: person('1', 'Ada'), roleIds: const ['10']),
        ],
        roles: [role('10', 'Admins', 2)],
        presenceFor: (_) => PresenceStatus.offline,
      );

      expect(sections.map((section) => section.title).toList(), ['Offline']);
    });

    test('a member takes the colour of their highest coloured role', () {
      final guild = KaedeGuild(
        ref: EntityRef.parse('900@home.example'),
        name: 'Guild',
        ownerRef: EntityRef.parse('1@home.example'),
        permissions: BigInt.zero,
        unavailable: false,
        roles: [
          KaedeRole(
            ref: EntityRef.parse('10@home.example'),
            guildRef: EntityRef.parse('900@home.example'),
            name: 'Low',
            color: 0x111111,
            permissions: BigInt.zero,
            position: 1,
            hoist: false,
            mentionable: false,
          ),
          KaedeRole(
            ref: EntityRef.parse('11@home.example'),
            guildRef: EntityRef.parse('900@home.example'),
            name: 'High',
            color: 0x55B998,
            permissions: BigInt.zero,
            position: 5,
            hoist: false,
            mentionable: false,
          ),
        ],
      );

      expect(
        memberRoleColor(
          guild,
          GuildMember(user: person('1', 'Ada'), roleIds: const ['10', '11']),
        ),
        const Color(0xFF55B998),
      );
      expect(
        memberRoleColor(
          guild,
          GuildMember(user: person('1', 'Ada'), roleIds: const []),
        ),
        isNull,
      );
    });

    test('a colourless higher role lets the next coloured role show through',
        () {
      final guild = KaedeGuild(
        ref: EntityRef.parse('900@home.example'),
        name: 'Guild',
        ownerRef: EntityRef.parse('1@home.example'),
        permissions: BigInt.zero,
        unavailable: false,
        roles: [
          KaedeRole(
            ref: EntityRef.parse('10@home.example'),
            guildRef: EntityRef.parse('900@home.example'),
            name: 'Colour',
            color: 0x55B998,
            permissions: BigInt.zero,
            position: 3,
            hoist: false,
            mentionable: false,
          ),
          KaedeRole(
            ref: EntityRef.parse('11@home.example'),
            guildRef: EntityRef.parse('900@home.example'),
            name: 'Colourless moderator',
            color: 0,
            permissions: BigInt.zero,
            position: 8,
            hoist: false,
            mentionable: false,
          ),
        ],
      );

      expect(
        memberRoleColor(
          guild,
          GuildMember(user: person('1', 'Ada'), roleIds: const ['10', '11']),
        ),
        const Color(0xFF55B998),
      );
    });
  });

  group('management rows explain themselves', () {
    test('roles summarise permissions rather than raw positions', () {
      KaedeRole role(BigInt permissions, {bool hoist = false}) => KaedeRole(
            ref: EntityRef.parse('10@home.example'),
            guildRef: EntityRef.parse('900@home.example'),
            name: 'Role',
            color: 0,
            permissions: permissions,
            position: 4,
            hoist: hoist,
            mentionable: false,
          );

      expect(
        roleSummaryLine(role(BigInt.from(Permission.administrator))),
        'Administrator',
      );
      expect(roleSummaryLine(role(BigInt.zero)), 'No extra permissions');
      expect(
        roleSummaryLine(
          role(BigInt.from(Permission.kickMembers), hoist: true),
        ),
        '1 permission · shown separately',
      );
    });

    test('channels state their type and placement', () {
      final category = KaedeChannel(
        ref: EntityRef.parse('5@home.example'),
        guildRef: EntityRef.parse('900@home.example'),
        type: ChannelType.category,
        name: 'Lounge',
        position: 0,
        permissions: BigInt.zero,
      );
      final voice = KaedeChannel(
        ref: EntityRef.parse('6@home.example'),
        guildRef: EntityRef.parse('900@home.example'),
        parentRef: category.ref,
        type: ChannelType.voice,
        name: 'Chat',
        position: 1,
        permissions: BigInt.zero,
      );

      expect(channelSummaryLine(category, null), 'Category');
      expect(channelSummaryLine(voice, category), 'Voice channel · in Lounge');
      expect(
        channelSummaryLine(
          KaedeChannel(
            ref: EntityRef.parse('7@home.example'),
            guildRef: EntityRef.parse('900@home.example'),
            type: ChannelType.text,
            name: 'general',
            topic: 'Anything goes',
            position: 2,
            permissions: BigInt.zero,
          ),
          null,
        ),
        'Text channel · no category · Anything goes',
      );
    });
  });
}
