import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';

void main() {
  test('encrypted guild mention intent expands qualified role recipients', () {
    final guildRef = EntityRef.parse('1@guild.example');
    final roleRef = EntityRef.parse('2@guild.example');
    final guild = KaedeGuild(
      ref: guildRef,
      name: 'Guild',
      ownerRef: EntityRef.parse('10@guild.example'),
      permissions: BigInt.zero,
      unavailable: false,
      roles: <KaedeRole>[
        KaedeRole(
          ref: roleRef,
          guildRef: guildRef,
          name: 'Notify',
          color: 0,
          permissions: BigInt.zero,
          position: 1,
          hoist: false,
          mentionable: true,
        ),
      ],
    );
    KaedeUser user(String id) => KaedeUser(
          ref: EntityRef.parse('$id@guild.example'),
          username: 'u$id',
          handle: '@u$id@guild.example',
        );
    final members = <GuildMember>[
      GuildMember(user: user('10'), roleIds: const <String>['2']),
      GuildMember(user: user('11'), roleIds: const <String>[]),
    ];

    expect(
      expandedMobileEncryptedGuildMentionRecipients(
        userRefs: const <String>['20@remote.example'],
        roleRefs: const <String>['2@guild.example'],
        everyone: false,
        guild: guild,
        members: members,
        canMentionEveryone: false,
        repliedUser: EntityRef.parse('11@guild.example'),
      ).map((item) => item.wire),
      <String>[
        '10@guild.example',
        '11@guild.example',
        '20@remote.example',
      ],
    );

    expect(
      expandedMobileEncryptedGuildMentionRecipients(
        userRefs: const <String>[],
        roleRefs: const <String>['2@guild.example'],
        everyone: true,
        guild: KaedeGuild(
          ref: guildRef,
          name: 'Guild',
          ownerRef: EntityRef.parse('10@guild.example'),
          permissions: BigInt.zero,
          unavailable: false,
          roles: <KaedeRole>[
            KaedeRole(
              ref: roleRef,
              guildRef: guildRef,
              name: 'Quiet',
              color: 0,
              permissions: BigInt.zero,
              position: 1,
              hoist: false,
              mentionable: false,
            ),
          ],
        ),
        members: members,
        canMentionEveryone: false,
      ),
      isEmpty,
    );
  });
}
