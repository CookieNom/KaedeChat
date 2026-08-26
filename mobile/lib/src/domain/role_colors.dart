import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/domain/models.dart';

/// Returns the highest assigned role with an explicit colour. Colourless roles
/// are transparent for display-colour precedence.
KaedeRole? highestColoredRole(KaedeGuild guild, GuildMember member) {
  KaedeRole? best;
  for (final role in guild.roles) {
    if (role.color == 0 || !member.roleIds.contains(role.ref.id.value)) {
      continue;
    }
    if (best == null ||
        role.position > best.position ||
        (role.position == best.position &&
            BigInt.parse(role.ref.id.value) <
                BigInt.parse(best.ref.id.value))) {
      best = role;
    }
  }
  return best;
}

Color? memberRoleColor(KaedeGuild guild, GuildMember member) {
  final role = highestColoredRole(guild, member);
  return role == null ? null : Color(0xFF000000 | role.color);
}

/// Returns the highest assigned role carrying a custom chat icon. Icon and
/// colour precedence are intentionally independent.
KaedeRole? highestIconRole(KaedeGuild guild, GuildMember member) {
  KaedeRole? best;
  for (final role in guild.roles) {
    if (role.iconHash == null || !member.roleIds.contains(role.ref.id.value)) {
      continue;
    }
    if (best == null ||
        role.position > best.position ||
        (role.position == best.position &&
            BigInt.parse(role.ref.id.value) <
                BigInt.parse(best.ref.id.value))) {
      best = role;
    }
  }
  return best;
}
