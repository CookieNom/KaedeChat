import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/guild_hierarchy.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/stage_permissions.dart';
import 'package:kaede_mobile/src/features/chat/forum_channel_view.dart';
import 'package:kaede_mobile/src/features/guild/guild_management_screen.dart';
import 'package:kaede_mobile/src/features/home/mobile_shell.dart';
import 'package:kaede_mobile/src/features/voice/voice_room.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';

void main() {
  final guildRef = EntityRef.parse('1@chat.example');
  final actorRef = EntityRef.parse('2@chat.example');
  final ownerRef = EntityRef.parse('3@chat.example');

  KaedeRole role(String id, int position) => KaedeRole(
        ref: EntityRef.parse('$id@chat.example'),
        guildRef: guildRef,
        name: 'Role $id',
        color: 0,
        permissions: BigInt.zero,
        position: position,
        hoist: false,
        mentionable: false,
      );

  final everyone = role('1', 0);
  final actorRole = role('20', 5);
  final tiedAboveActor = role('10', 5);
  final tiedBelowActor = role('30', 5);
  final lower = role('40', 4);
  final higher = role('50', 6);

  KaedeUser user(String id) => KaedeUser(
        ref: EntityRef.parse('$id@chat.example'),
        username: 'user$id',
        handle: '@user$id@chat.example',
      );

  GuildMember member(String id, String roleId) => GuildMember(
        user: user(id),
        roleIds: <String>[roleId],
      );

  KaedeGuild guild({int permissions = 0, EntityRef? owner}) => KaedeGuild(
        ref: guildRef,
        name: 'Guild',
        ownerRef: owner ?? ownerRef,
        permissions: BigInt.from(permissions),
        unavailable: false,
        actorHighestRoleId: actorRole.ref.id.value,
        roles: <KaedeRole>[
          everyone,
          actorRole,
          tiedAboveActor,
          tiedBelowActor,
          lower,
          higher,
        ],
      );

  group('exact guild hierarchy', () {
    test('uses snowflake ID as the equal-position tiebreaker', () {
      expect(compareGuildRoleRank(actorRole, lower), greaterThan(0));
      expect(compareGuildRoleRank(actorRole, higher), lessThan(0));
      expect(compareGuildRoleRank(actorRole, tiedBelowActor), greaterThan(0));
      expect(compareGuildRoleRank(actorRole, tiedAboveActor), lessThan(0));
    });

    test('live member updates replace REST roles and preserve removals', () {
      final target = member('4', lower.ref.id.value);
      final elevated = member('4', higher.ref.id.value);
      final removed = member('5', tiedBelowActor.ref.id.value);

      expect(
        reconcileGuildManagementMembers(
          members: <GuildMember>[target, removed],
          liveMembers: <GuildMember>[elevated],
          removedRefs: removedGuildManagementMemberRefs(
            <GuildMember>[target, removed],
            <GuildMember>[elevated],
          ),
        ),
        <GuildMember>[elevated],
      );
      expect(
        guildActorCanManageMember(
          guild: guild(),
          actorRef: actorRef,
          actorHighestRole: actorRole,
          target: elevated,
        ),
        isFalse,
      );
    });

    test('unknown live member roles fail hierarchy closed', () {
      final unresolved = member('4', '999');
      expect(
        guildActorCanManageMember(
          guild: guild(),
          actorRef: actorRef,
          actorHighestRole: actorRole,
          target: unresolved,
        ),
        isFalse,
      );
    });

    test('role and member targets require a strictly lower rank', () {
      final model = guild();
      expect(
        guildActorCanManageRole(
          guild: model,
          actorRef: actorRef,
          actorHighestRole: actorRole,
          target: lower,
        ),
        isTrue,
      );
      expect(
        guildActorCanManageRole(
          guild: model,
          actorRef: actorRef,
          actorHighestRole: actorRole,
          target: tiedAboveActor,
        ),
        isFalse,
      );
      expect(
        guildActorCanManageMember(
          guild: model,
          actorRef: actorRef,
          actorHighestRole: actorRole,
          target: member('4', tiedBelowActor.ref.id.value),
        ),
        isTrue,
      );
      expect(
        guildActorCanManageMember(
          guild: model,
          actorRef: actorRef,
          actorHighestRole: actorRole,
          target: member('5', tiedAboveActor.ref.id.value),
        ),
        isFalse,
      );
      expect(
        guildActorCanManageMember(
          guild: model,
          actorRef: actorRef,
          actorHighestRole: actorRole,
          target: GuildMember(user: user('3'), roleIds: const <String>[]),
        ),
        isFalse,
      );
      expect(
        guildActorCanManageMember(
          guild: model,
          actorRef: actorRef,
          actorHighestRole: actorRole,
          target: GuildMember(user: user('2'), roleIds: const <String>[]),
        ),
        isFalse,
      );
    });

    test('overwrite targets reuse exact role and member hierarchy', () {
      final model = guild();
      final members = <GuildMember>[
        member('4', tiedBelowActor.ref.id.value),
        member('5', tiedAboveActor.ref.id.value),
        GuildMember(user: user('3'), roleIds: const <String>[]),
      ];
      expect(
        channelOverwriteTargetEligible(
          guild: model,
          actorRef: actorRef,
          actorHighestRole: actorRole,
          target: lower.ref,
          targetType: 'role',
          members: members,
        ),
        isTrue,
      );
      expect(
        channelOverwriteTargetEligible(
          guild: model,
          actorRef: actorRef,
          actorHighestRole: actorRole,
          target: tiedAboveActor.ref,
          targetType: 'role',
          members: members,
        ),
        isFalse,
      );
      expect(
        channelOverwriteTargetEligible(
          guild: model,
          actorRef: actorRef,
          actorHighestRole: actorRole,
          target: members[0].user.ref,
          targetType: 'member',
          members: members,
        ),
        isTrue,
      );
      expect(
        channelOverwriteTargetEligible(
          guild: model,
          actorRef: actorRef,
          actorHighestRole: actorRole,
          target: members[2].user.ref,
          targetType: 'member',
          members: members,
        ),
        isFalse,
      );
    });

    test('nickname authority distinguishes self and other members', () {
      final self = GuildMember(user: user('2'), roleIds: <String>['20']);
      final target = member('4', lower.ref.id.value);
      expect(
        canChangeGuildMemberNickname(
          guild: guild(),
          actorRef: actorRef,
          actorHighestRole: actorRole,
          target: self,
        ),
        isFalse,
      );
      expect(
        canChangeGuildMemberNickname(
          guild: guild(permissions: Permission.changeNickname),
          actorRef: actorRef,
          actorHighestRole: actorRole,
          target: self,
        ),
        isTrue,
      );
      expect(
        canChangeGuildMemberNickname(
          guild: guild(permissions: Permission.manageNicknames),
          actorRef: actorRef,
          actorHighestRole: actorRole,
          target: target,
        ),
        isTrue,
      );
    });
  });

  test('role and overwrite editors enforce the actor held-bit ceiling', () {
    final held = BigInt.from(Permission.viewChannel | Permission.manageRoles);
    expect(rolePermissionCanChange(held, Permission.manageRoles), isTrue);
    expect(rolePermissionCanChange(held, Permission.sendMessages), isFalse);
    expect(
      rolePermissionChangesWithinCeiling(
        BigInt.zero,
        BigInt.from(Permission.manageRoles),
        held,
      ),
      isTrue,
    );
    expect(
      rolePermissionChangesWithinCeiling(
        BigInt.zero,
        BigInt.from(Permission.sendMessages),
        held,
      ),
      isFalse,
    );
    expect(
      channelOverwriteCanReset(
        BigInt.from(Permission.sendMessages),
        BigInt.zero,
        held,
      ),
      isFalse,
    );
  });

  test('category and invite targets use current effective permissions', () {
    KaedeChannel channel(String id, int permissions) => KaedeChannel(
          ref: EntityRef.parse('$id@chat.example'),
          guildRef: guildRef,
          type: ChannelType.category,
          position: int.parse(id),
          permissions: BigInt.from(permissions),
        );
    expect(
      channelCategoryTargetEligible(
        channel(
          '6',
          Permission.viewChannel | Permission.manageChannels,
        ),
        isOwner: false,
      ),
      isTrue,
    );
    expect(
      channelCategoryTargetEligible(
        channel('7', Permission.manageChannels),
        isOwner: false,
      ),
      isFalse,
    );
  });

  test('position-only reorder uses guild and current-parent authority', () {
    KaedeChannel channel(
      String id,
      ChannelType type,
      int permissions, {
      EntityRef? parent,
    }) =>
        KaedeChannel(
          ref: EntityRef.parse('$id@chat.example'),
          guildRef: guildRef,
          parentRef: parent,
          type: type,
          position: int.parse(id),
          permissions: BigInt.from(permissions),
        );
    final allowedParent = channel(
      '80',
      ChannelType.category,
      Permission.manageChannels,
    );
    final deniedParent = channel('81', ChannelType.category, 0);
    final deniedChild = channel(
      '82',
      ChannelType.text,
      0,
      parent: allowedParent.ref,
    );
    final deniedUncategorized = channel('83', ChannelType.text, 0);

    expect(
      channelPositionReorderAllowed(
        deniedChild,
        <KaedeChannel>[allowedParent, deniedChild],
        canManageGuildChannels: true,
        isOwner: false,
      ),
      isTrue,
    );
    expect(
      channelPositionReorderAllowed(
        deniedUncategorized,
        <KaedeChannel>[deniedUncategorized],
        canManageGuildChannels: true,
        isOwner: false,
      ),
      isTrue,
    );
    expect(
      channelPositionReorderAllowed(
        channel('84', ChannelType.text, 0, parent: deniedParent.ref),
        <KaedeChannel>[deniedParent],
        canManageGuildChannels: true,
        isOwner: false,
      ),
      isFalse,
    );
    expect(
      channelPositionReorderAllowed(
        deniedUncategorized,
        <KaedeChannel>[deniedUncategorized],
        canManageGuildChannels: false,
        isOwner: false,
      ),
      isFalse,
    );
  });

  test('history, forum, and roster surfaces require their complete grants', () {
    KaedeChannel text(int permissions) => KaedeChannel(
          ref: EntityRef.parse('60@chat.example'),
          guildRef: guildRef,
          type: ChannelType.text,
          position: 0,
          permissions: BigInt.from(permissions),
        );
    expect(
      canReadRetainedChannelHistory(text(Permission.viewChannel)),
      isFalse,
    );
    expect(
      canReadRetainedChannelHistory(text(
        Permission.viewChannel | Permission.readMessageHistory,
      )),
      isTrue,
    );
    final forum = KaedeChannel(
      ref: EntityRef.parse('61@chat.example'),
      guildRef: guildRef,
      type: ChannelType.forum,
      position: 1,
      permissions: BigInt.from(
        Permission.viewChannel | Permission.sendMessages,
      ),
    );
    expect(canCreateForumPostNow(forum, hasAttachments: false), isTrue);
    expect(canCreateForumPostNow(forum, hasAttachments: true), isFalse);
    expect(canViewGuildMemberRoster(guild(), actorRef), isFalse);
    expect(
      canViewGuildMemberRoster(
        guild(permissions: Permission.viewChannel),
        actorRef,
      ),
      isTrue,
    );
  });

  test('voice status, Stage start, and move destinations stay channel scoped',
      () {
    KaedeChannel voice(String id, ChannelType type, int permissions) =>
        KaedeChannel(
          ref: EntityRef.parse('$id@chat.example'),
          guildRef: guildRef,
          type: type,
          position: int.parse(id),
          permissions: BigInt.from(permissions),
        );
    final source = voice('70', ChannelType.voice, Permission.moveMembers);
    final allowed = voice('71', ChannelType.voice, Permission.moveMembers);
    final denied = voice('72', ChannelType.stage, Permission.connect);
    final voiceGuild = KaedeGuild(
      ref: guildRef,
      name: 'Voice guild',
      ownerRef: ownerRef,
      permissions: BigInt.zero,
      unavailable: false,
      channels: <KaedeChannel>[source, allowed, denied],
    );
    expect(
      voiceMoveDestinationChannels(voiceGuild, source),
      <KaedeChannel>[allowed],
    );
    expect(
      canSetVoiceChannelStatusNow(
        voice('73', ChannelType.voice, Permission.setVoiceChannelStatus),
        joined: false,
      ),
      isFalse,
    );
    expect(
      canSetVoiceChannelStatusNow(
        voice('73', ChannelType.voice, Permission.setVoiceChannelStatus),
        joined: true,
      ),
      isTrue,
    );
    final stage = voice(
      '74',
      ChannelType.stage,
      stageModeratorPermissions,
    );
    expect(canStartStageNow(stage, notify: false), isTrue);
    expect(canStartStageNow(stage, notify: true), isFalse);
  });
}
