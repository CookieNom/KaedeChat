import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';

sealed class GuildNavigationItem {
  const GuildNavigationItem();

  Map<String, Object?> toJson();
}

final class GuildNavigationGuildItem extends GuildNavigationItem {
  const GuildNavigationGuildItem(this.guild);

  final EntityRef guild;

  @override
  Map<String, Object?> toJson() => <String, Object?>{
        'kind': 'guild',
        'guild': guild.wire,
      };
}

final class GuildNavigationGroupItem extends GuildNavigationItem {
  const GuildNavigationGroupItem({
    required this.id,
    required this.name,
    required this.guilds,
    this.collapsed = false,
  });

  final String id;
  final String name;
  final List<EntityRef> guilds;
  final bool collapsed;

  GuildNavigationGroupItem copyWith({
    String? name,
    List<EntityRef>? guilds,
    bool? collapsed,
  }) =>
      GuildNavigationGroupItem(
        id: id,
        name: name ?? this.name,
        guilds: guilds ?? this.guilds,
        collapsed: collapsed ?? this.collapsed,
      );

  @override
  Map<String, Object?> toJson() => <String, Object?>{
        'kind': 'group',
        'id': id,
        'name': name,
        'guilds': guilds.map((guild) => guild.wire).toList(),
        'collapsed': collapsed,
      };
}

final class GuildNavigation {
  const GuildNavigation({this.items = const <GuildNavigationItem>[]});

  factory GuildNavigation.fromJson(Object? value) {
    if (value is! Map || value['items'] is! List) {
      return const GuildNavigation();
    }
    final items = <GuildNavigationItem>[];
    final seenGuilds = <EntityRef>{};
    final seenGroups = <String>{};
    for (final raw in (value['items'] as List).take(200)) {
      if (raw is! Map) continue;
      try {
        if (raw['kind'] == 'guild') {
          final guild = EntityRef.fromJson(raw['guild']);
          if (seenGuilds.add(guild)) items.add(GuildNavigationGuildItem(guild));
          continue;
        }
        final id = raw['id'];
        final name = raw['name'];
        final rawGuilds = raw['guilds'];
        if (raw['kind'] != 'group' ||
            id is! String ||
            !RegExp(r'^[A-Za-z0-9_-]{1,36}$').hasMatch(id) ||
            !seenGroups.add(id) ||
            name is! String ||
            name.trim().isEmpty ||
            rawGuilds is! List) {
          continue;
        }
        final guilds = <EntityRef>[];
        for (final rawGuild in rawGuilds.take(100)) {
          try {
            final guild = EntityRef.fromJson(rawGuild);
            if (seenGuilds.add(guild)) guilds.add(guild);
          } on FormatException {
            // Ignore a malformed member without losing the rest of the layout.
          }
        }
        if (guilds.isNotEmpty) {
          items.add(
            GuildNavigationGroupItem(
              id: id,
              name: name.trim().substring(0, name.trim().length.clamp(0, 32)),
              guilds: List.unmodifiable(guilds),
              collapsed: raw['collapsed'] == true,
            ),
          );
        }
      } on FormatException {
        // The server will repair stale references on the next successful load.
      }
    }
    return GuildNavigation(items: List.unmodifiable(items));
  }

  final List<GuildNavigationItem> items;

  Map<String, Object?> toJson() => <String, Object?>{
        'items': items.map((item) => item.toJson()).toList(),
      };
}

GuildNavigation reconcileGuildNavigation(
  GuildNavigation navigation,
  List<KaedeGuild> guilds,
) {
  final accessible = {for (final guild in guilds) guild.ref};
  final seen = <EntityRef>{};
  final items = <GuildNavigationItem>[];
  for (final item in navigation.items) {
    switch (item) {
      case GuildNavigationGuildItem():
        if (accessible.contains(item.guild) && seen.add(item.guild)) {
          items.add(item);
        }
      case GuildNavigationGroupItem():
        final members = item.guilds
            .where((guild) => accessible.contains(guild) && seen.add(guild))
            .toList(growable: false);
        if (members.isNotEmpty) items.add(item.copyWith(guilds: members));
    }
  }
  for (final guild in guilds) {
    if (seen.add(guild.ref)) items.add(GuildNavigationGuildItem(guild.ref));
  }
  return GuildNavigation(items: List.unmodifiable(items));
}

GuildNavigation reorderGuildNavigation(
  GuildNavigation navigation,
  int oldIndex,
  int newIndex,
) {
  final items = List<GuildNavigationItem>.of(navigation.items);
  if (oldIndex < 0 ||
      oldIndex >= items.length ||
      newIndex < 0 ||
      newIndex > items.length) {
    return navigation;
  }
  if (newIndex > oldIndex) newIndex -= 1;
  final item = items.removeAt(oldIndex);
  items.insert(newIndex, item);
  return GuildNavigation(items: List.unmodifiable(items));
}

GuildNavigation updateGuildNavigationGroup(
  GuildNavigation navigation,
  String groupId, {
  String? name,
  bool? collapsed,
  List<EntityRef>? guilds,
}) =>
    GuildNavigation(
      items: navigation.items
          .map(
            (item) => item is GuildNavigationGroupItem && item.id == groupId
                ? item.copyWith(
                    name: name, collapsed: collapsed, guilds: guilds)
                : item,
          )
          .toList(growable: false),
    );

GuildNavigation createGuildNavigationGroup(
  GuildNavigation navigation,
  String id,
  String name,
  List<EntityRef> selected,
) {
  final selectedSet = selected.toSet();
  if (selectedSet.isEmpty) return navigation;
  final items = <GuildNavigationItem>[];
  var insertion = navigation.items.length;
  for (final item in navigation.items) {
    if (item is GuildNavigationGuildItem && selectedSet.contains(item.guild)) {
      insertion = insertion.clamp(0, items.length);
      continue;
    }
    if (item is GuildNavigationGroupItem) {
      final guilds =
          item.guilds.where((guild) => !selectedSet.contains(guild)).toList();
      if (guilds.length != item.guilds.length) {
        insertion = insertion.clamp(0, items.length);
      }
      if (guilds.isNotEmpty) items.add(item.copyWith(guilds: guilds));
      continue;
    }
    items.add(item);
  }
  items.insert(
    insertion.clamp(0, items.length),
    GuildNavigationGroupItem(
      id: id,
      name: name.trim().isEmpty ? 'Guild group' : name.trim(),
      guilds: List.unmodifiable(selected),
    ),
  );
  return GuildNavigation(items: List.unmodifiable(items));
}

GuildNavigation replaceGuildNavigationGroup(
  GuildNavigation navigation,
  String id,
  String name,
  List<EntityRef> selected,
) {
  final selectedSet = selected.toSet();
  if (selectedSet.isEmpty) return navigation;
  final items = <GuildNavigationItem>[];
  var insertion = navigation.items.length;
  for (final item in navigation.items) {
    if (item is GuildNavigationGroupItem && item.id == id) {
      insertion = items.length;
      continue;
    }
    if (item is GuildNavigationGuildItem && selectedSet.contains(item.guild)) {
      continue;
    }
    if (item is GuildNavigationGroupItem) {
      final guilds =
          item.guilds.where((guild) => !selectedSet.contains(guild)).toList();
      if (guilds.isNotEmpty) items.add(item.copyWith(guilds: guilds));
      continue;
    }
    items.add(item);
  }
  items.insert(
    insertion.clamp(0, items.length),
    GuildNavigationGroupItem(
      id: id,
      name: name.trim().isEmpty ? 'Guild group' : name.trim(),
      guilds: List.unmodifiable(selected),
    ),
  );
  return GuildNavigation(items: List.unmodifiable(items));
}

GuildNavigation ungroupGuildNavigation(
        GuildNavigation navigation, String groupId) =>
    GuildNavigation(
      items: navigation.items.expand((item) sync* {
        if (item is GuildNavigationGroupItem && item.id == groupId) {
          for (final guild in item.guilds) {
            yield GuildNavigationGuildItem(guild);
          }
        } else {
          yield item;
        }
      }).toList(growable: false),
    );
