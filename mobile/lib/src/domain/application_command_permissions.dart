import 'package:kaede_mobile/src/core/network_json.dart';
import 'package:kaede_mobile/src/core/refs.dart';

typedef CommandPermissionJson = Map<String, Object?>;

final class ApplicationCommandPermissionEntry {
  const ApplicationCommandPermissionEntry({
    required this.target,
    required this.type,
    required this.permission,
  });

  factory ApplicationCommandPermissionEntry.fromJson(
    CommandPermissionJson json,
  ) =>
      ApplicationCommandPermissionEntry(
        target: EntityRef.fromJson(json['id']),
        type: '${json['type']}',
        permission: json['permission'] == true,
      );

  final EntityRef target;
  final String type;
  final bool permission;

  ApplicationCommandPermissionEntry copyWith({bool? permission}) =>
      ApplicationCommandPermissionEntry(
        target: target,
        type: type,
        permission: permission ?? this.permission,
      );

  CommandPermissionJson toJson() => <String, Object?>{
        'id': target.wire,
        'type': type,
        'permission': permission,
      };
}

final class ApplicationCommandPermissionScope {
  const ApplicationCommandPermissionScope({
    required this.id,
    required this.application,
    required this.applicationName,
    required this.guild,
    required this.synced,
    required this.permissions,
    this.command,
    this.commandName,
  });

  factory ApplicationCommandPermissionScope.fromJson(
    CommandPermissionJson json,
  ) {
    final command = json['command'] is Map
        ? Map<String, Object?>.from(json['command']! as Map)
        : null;
    return ApplicationCommandPermissionScope(
      id: EntityRef.fromJson(json['id']),
      application: EntityRef.fromJson(json['application_ref']),
      applicationName: '${json['application_name']}',
      guild: EntityRef.fromJson(json['guild_ref']),
      command: command == null ? null : EntityRef.fromJson(command['ref']),
      commandName: command == null ? null : '${command['name']}',
      synced: json['synced'] == true,
      permissions: strictNetworkObjectList(
        json['permissions'],
        label: 'Application command permissions',
      ).map(ApplicationCommandPermissionEntry.fromJson).toList(growable: false),
    );
  }

  final EntityRef id;
  final EntityRef application;
  final String applicationName;
  final EntityRef guild;
  final EntityRef? command;
  final String? commandName;
  final bool synced;
  final List<ApplicationCommandPermissionEntry> permissions;

  String get label =>
      commandName == null ? 'All $applicationName commands' : '/$commandName';
}

CommandPermissionJson commandPermissionUpdateData(
  Iterable<ApplicationCommandPermissionEntry> entries,
) =>
    <String, Object?>{
      'permissions': entries.map((entry) => entry.toJson()).toList(),
    };
