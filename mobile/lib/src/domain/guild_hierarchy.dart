import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';

/// Matches the backend's `(position, -snowflake id)` role ordering.
int compareGuildRoleRank(KaedeRole left, KaedeRole right) {
  final position = left.position.compareTo(right.position);
  if (position != 0) return position;
  return BigInt.parse(right.ref.id.value)
      .compareTo(BigInt.parse(left.ref.id.value));
}

KaedeRole? guildRoleById(KaedeGuild guild, String roleId) {
  final normalized = roleId.contains('@') ? roleId.split('@').first : roleId;
  for (final role in guild.roles) {
    if (role.ref.wire == roleId || role.ref.id.value == normalized) return role;
  }
  return null;
}

KaedeRole? guildActorHighestRole(KaedeGuild guild) {
  final id = guild.actorHighestRoleId;
  return id == null ? null : guildRoleById(guild, id);
}

GuildMember? guildMemberByRef(
  Iterable<GuildMember> members,
  EntityRef target,
) {
  for (final member in members) {
    if (member.user.ref == target) return member;
  }
  return null;
}

GuildMember? guildMemberByIdentity(
  Iterable<GuildMember> members,
  String identity,
) {
  try {
    return guildMemberByRef(members, EntityRef.parse(identity));
  } on FormatException {
    return null;
  }
}

KaedeRole? guildMemberHighestRole(KaedeGuild guild, GuildMember member) {
  final everyone = guild.roles
      .where((role) => role.ref == guild.ref || role.position == 0)
      .firstOrNull;
  if (everyone == null) return null;
  final roles = <KaedeRole>[everyone];
  for (final id in member.roleIds) {
    final role = guildRoleById(guild, id);
    if (role == null) return null;
    if (!roles.any((item) => item.ref == role.ref)) roles.add(role);
  }
  roles.sort(compareGuildRoleRank);
  return roles.last;
}

bool guildActorCanManageRole({
  required KaedeGuild guild,
  required EntityRef? actorRef,
  required KaedeRole? actorHighestRole,
  required KaedeRole target,
}) {
  if (actorRef == null) return false;
  if (actorRef == guild.ownerRef) return true;
  return actorHighestRole != null &&
      compareGuildRoleRank(actorHighestRole, target) > 0;
}

bool guildActorCanManageMember({
  required KaedeGuild guild,
  required EntityRef? actorRef,
  required KaedeRole? actorHighestRole,
  required GuildMember target,
}) {
  if (actorRef == null ||
      target.user.ref == guild.ownerRef ||
      target.user.ref == actorRef) {
    return false;
  }
  if (actorRef == guild.ownerRef) return true;
  final targetHighestRole = guildMemberHighestRole(guild, target);
  return actorHighestRole != null &&
      targetHighestRole != null &&
      compareGuildRoleRank(actorHighestRole, targetHighestRole) > 0;
}
