import 'package:kaede_mobile/src/core/refs.dart';

const mobileDirectoryCollectionSlugs = <String>[
  'featured',
  'staff-picks',
  'new-and-noteworthy',
];

const _directoryCategories = <String>{
  'entertainment',
  'games',
  'moderation',
  'productivity',
  'social',
  'utilities',
};
const _installTypes = <String>{'guild_install', 'user_install'};
const _recentApplicationLimit = 20;
final _snowflakePattern = RegExp(r'^[1-9][0-9]{0,18}$');
final _assetHashPattern = RegExp(r'^[0-9a-f]{64}$');
final _tagPattern = RegExp(r'^[a-z0-9][a-z0-9_-]{0,31}$');
final _templateSlugPattern = RegExp(r'^[a-z0-9][a-z0-9_-]{1,63}$');

final class MobileDirectoryInstallTemplate {
  const MobileDirectoryInstallTemplate({
    required this.slug,
    required this.name,
    required this.description,
    required this.installTypes,
    required this.defaultInstallType,
  });

  factory MobileDirectoryInstallTemplate.fromJson(
    Map<String, Object?> json,
  ) {
    _requireExactKeys(
      json,
      const <String>{
        'slug',
        'name',
        'description',
        'install_types',
        'default_install_type',
      },
      'Directory install template',
    );
    final slug = _boundedText(json['slug'], 1, 64);
    final name = _boundedText(json['name'], 1, 100);
    final description = _optionalBoundedText(json['description'], 500);
    final installTypes = _stringList(
      json['install_types'],
      label: 'Directory install types',
      minimum: 1,
      maximum: 2,
      allowed: _installTypes,
    );
    final defaultInstallType = json['default_install_type'];
    if (!_templateSlugPattern.hasMatch(slug) ||
        defaultInstallType is! String ||
        !installTypes.contains(defaultInstallType)) {
      throw const FormatException('Directory install template is invalid.');
    }
    return MobileDirectoryInstallTemplate(
      slug: slug,
      name: name,
      description: description,
      installTypes: installTypes,
      defaultInstallType: defaultInstallType,
    );
  }

  final String slug;
  final String name;
  final String? description;
  final List<String> installTypes;
  final String defaultInstallType;
}

final class MobileDirectoryApplication {
  const MobileDirectoryApplication({
    required this.ref,
    required this.name,
    required this.summary,
    required this.category,
    required this.tags,
    required this.collections,
    required this.iconHash,
    required this.bannerHash,
    required this.verified,
    required this.installTemplate,
    required this.userInstallSupported,
  });

  factory MobileDirectoryApplication.fromJson(
    Map<String, Object?> json, {
    required Domain expectedOrigin,
  }) {
    _requireExactKeys(
      json,
      const <String>{
        'id',
        'ref',
        'origin_domain',
        'name',
        'summary',
        'category',
        'tags',
        'collections',
        'icon_hash',
        'banner_hash',
        'verified',
        'install_template',
        'user_install_supported',
      },
      'Directory application',
    );
    final id = _snowflake(json['id'], 'Directory application id');
    final origin = json['origin_domain'];
    final rawRef = json['ref'];
    if (origin is! String || rawRef is! String) {
      throw const FormatException('Directory application identity is invalid.');
    }
    final ref = EntityRef.parse(rawRef);
    if (ref.id.value != id ||
        ref.domain.value != origin ||
        ref.domain != expectedOrigin) {
      throw const FormatException('Directory application identity is invalid.');
    }
    final category = json['category'];
    final verified = json['verified'];
    final userInstallSupported = json['user_install_supported'];
    if (category is! String ||
        !_directoryCategories.contains(category) ||
        verified is! bool ||
        !verified ||
        userInstallSupported is! bool) {
      throw const FormatException('Directory application is not reviewed.');
    }
    final tags = _stringList(
      json['tags'],
      label: 'Directory tags',
      minimum: 1,
      maximum: 5,
    );
    if (tags.any((tag) => !_tagPattern.hasMatch(tag))) {
      throw const FormatException('Directory tags are invalid.');
    }
    final collections = _stringList(
      json['collections'],
      label: 'Directory collections',
      minimum: 0,
      maximum: 3,
      allowed: mobileDirectoryCollectionSlugs.toSet(),
    );
    final iconHash = _assetHash(json['icon_hash']);
    final bannerHash = _assetHash(json['banner_hash']);
    final rawTemplate = json['install_template'];
    if (rawTemplate is! Map || rawTemplate.keys.any((key) => key is! String)) {
      throw const FormatException('Directory install template is invalid.');
    }
    final template = MobileDirectoryInstallTemplate.fromJson(
      Map<String, Object?>.from(rawTemplate),
    );
    if (userInstallSupported !=
        template.installTypes.contains('user_install')) {
      throw const FormatException('Directory install support is inconsistent.');
    }
    return MobileDirectoryApplication(
      ref: ref,
      name: _boundedText(json['name'], 1, 100),
      summary: _boundedText(json['summary'], 1, 200),
      category: category,
      tags: tags,
      collections: collections,
      iconHash: iconHash,
      bannerHash: bannerHash,
      verified: verified,
      installTemplate: template,
      userInstallSupported: userInstallSupported,
    );
  }

  final EntityRef ref;
  final String name;
  final String summary;
  final String category;
  final List<String> tags;
  final List<String> collections;
  final String? iconHash;
  final String? bannerHash;
  final bool verified;
  final MobileDirectoryInstallTemplate installTemplate;
  final bool userInstallSupported;
}

final class MobileBotProfileApplication {
  const MobileBotProfileApplication({
    required this.bot,
    required this.application,
    required this.name,
    required this.installTemplate,
    required this.directoryListed,
  });

  factory MobileBotProfileApplication.fromJson(
    Map<String, Object?> json, {
    required EntityRef expectedBot,
  }) {
    _requireExactKeys(
      json,
      const <String>{
        'bot_ref',
        'application_ref',
        'origin_domain',
        'name',
        'install_template',
        'directory_listed',
      },
      'Bot profile application',
    );
    final rawBot = json['bot_ref'];
    final rawApplication = json['application_ref'];
    final rawOrigin = json['origin_domain'];
    final rawTemplate = json['install_template'];
    final directoryListed = json['directory_listed'];
    if (rawBot is! String ||
        rawApplication is! String ||
        rawOrigin is! String ||
        rawTemplate is! Map ||
        rawTemplate.keys.any((key) => key is! String) ||
        directoryListed is! bool) {
      throw const FormatException('Bot profile application is invalid.');
    }
    final bot = EntityRef.parse(rawBot);
    final application = EntityRef.parse(rawApplication);
    if (bot != expectedBot ||
        bot.domain.value != rawOrigin ||
        application.domain.value != rawOrigin) {
      throw const FormatException(
        'Bot profile application identity is inconsistent.',
      );
    }
    return MobileBotProfileApplication(
      bot: bot,
      application: application,
      name: _boundedText(json['name'], 1, 100),
      installTemplate: MobileDirectoryInstallTemplate.fromJson(
        Map<String, Object?>.from(rawTemplate),
      ),
      directoryListed: directoryListed,
    );
  }

  final EntityRef bot;
  final EntityRef application;
  final String name;
  final MobileDirectoryInstallTemplate installTemplate;
  final bool directoryListed;
}

final class MobileDirectoryCollection {
  const MobileDirectoryCollection({
    required this.slug,
    required this.name,
    required this.description,
  });

  factory MobileDirectoryCollection.fromJson(Map<String, Object?> json) {
    _requireExactKeys(
      json,
      const <String>{'slug', 'name', 'description'},
      'Directory collection',
    );
    final slug = json['slug'];
    if (slug is! String || !mobileDirectoryCollectionSlugs.contains(slug)) {
      throw const FormatException('Directory collection is invalid.');
    }
    return MobileDirectoryCollection(
      slug: slug,
      name: _boundedText(json['name'], 1, 100),
      description: _boundedText(json['description'], 1, 300),
    );
  }

  final String slug;
  final String name;
  final String description;
}

final class MobileDirectoryPage {
  const MobileDirectoryPage({
    required this.items,
    required this.nextCursor,
    required this.collections,
    required this.selectedCollection,
  });

  factory MobileDirectoryPage.fromJson(
    Map<String, Object?> json, {
    required Domain expectedOrigin,
    required String? expectedCollection,
    int requestedLimit = 50,
  }) {
    if (requestedLimit < 1 || requestedLimit > 50) {
      throw ArgumentError.value(requestedLimit, 'requestedLimit');
    }
    _requireExactKeys(
      json,
      const <String>{
        'items',
        'next_cursor',
        'collections',
        'selected_collection',
      },
      'Directory page',
    );
    if (expectedCollection != null &&
        !mobileDirectoryCollectionSlugs.contains(expectedCollection)) {
      throw ArgumentError.value(expectedCollection, 'expectedCollection');
    }
    final rawItems = _objectList(json['items'], 'Directory items');
    if (rawItems.length > requestedLimit) {
      throw const FormatException('Directory page is too large.');
    }
    final items = rawItems
        .map(
          (item) => MobileDirectoryApplication.fromJson(
            item,
            expectedOrigin: expectedOrigin,
          ),
        )
        .toList(growable: false);
    final refs = items.map((item) => item.ref).toList(growable: false);
    if (refs.toSet().length != refs.length) {
      throw const FormatException('Directory page repeats an application.');
    }
    for (var index = 1; index < refs.length; index += 1) {
      if (BigInt.parse(refs[index - 1].id.value) >=
          BigInt.parse(refs[index].id.value)) {
        throw const FormatException('Directory page is not ordered.');
      }
    }
    final nextCursor = json['next_cursor'];
    if (nextCursor != null &&
        (nextCursor is! String ||
            !_snowflakePattern.hasMatch(nextCursor) ||
            items.length != requestedLimit ||
            items.last.ref.id.value != nextCursor)) {
      throw const FormatException('Directory page cursor is invalid.');
    }
    final rawCollections = _objectList(
      json['collections'],
      'Directory collections',
    );
    if (rawCollections.length > mobileDirectoryCollectionSlugs.length) {
      throw const FormatException('Directory collection catalog is too large.');
    }
    final collections = rawCollections
        .map(MobileDirectoryCollection.fromJson)
        .toList(growable: false);
    final collectionSlugs = collections.map((item) => item.slug).toList();
    if (collectionSlugs.toSet().length != collectionSlugs.length ||
        !_listEquals(collectionSlugs, mobileDirectoryCollectionSlugs)) {
      throw const FormatException('Directory collection catalog is invalid.');
    }
    final selectedCollection = json['selected_collection'];
    if (selectedCollection != expectedCollection) {
      throw const FormatException(
          'Directory collection filter is inconsistent.');
    }
    return MobileDirectoryPage(
      items: List<MobileDirectoryApplication>.unmodifiable(items),
      nextCursor: nextCursor as String?,
      collections: List<MobileDirectoryCollection>.unmodifiable(collections),
      selectedCollection: selectedCollection as String?,
    );
  }

  final List<MobileDirectoryApplication> items;
  final String? nextCursor;
  final List<MobileDirectoryCollection> collections;
  final String? selectedCollection;
}

String mobileAppRecentStorageKey(EntityRef account) =>
    'app-launcher-recents-v1:${account.wire}';

List<String> mobileRememberRecentApplication(
  Iterable<String> history,
  EntityRef application,
) {
  final retained = <String>[];
  final seen = <String>{};
  for (final value in history) {
    if (value.length > 320 || value == application.wire) continue;
    try {
      final ref = EntityRef.parse(value);
      if (ref.wire == value && seen.add(value)) retained.add(value);
    } on FormatException {
      // Corrupt local ranking data is discarded without affecting commands.
    }
  }
  retained.add(application.wire);
  if (retained.length > _recentApplicationLimit) {
    retained.removeRange(0, retained.length - _recentApplicationLimit);
  }
  return List<String>.unmodifiable(retained);
}

List<EntityRef> mobileRecentApplicationRefs(Iterable<String> history) {
  final refs = <EntityRef>[];
  final seen = <EntityRef>{};
  for (final value in history.toList().reversed) {
    if (refs.length == _recentApplicationLimit) break;
    if (value.length > 320) continue;
    try {
      final ref = EntityRef.parse(value);
      if (ref.wire == value && seen.add(ref)) refs.add(ref);
    } on FormatException {
      // Corrupt local ranking data is ignored.
    }
  }
  return List<EntityRef>.unmodifiable(refs);
}

Map<String, List<MobileDirectoryApplication>> mobileUniqueDirectorySections(
  Map<String, Iterable<MobileDirectoryApplication>> sections, {
  Iterable<EntityRef> excluded = const <EntityRef>[],
}) {
  final seen = excluded.toSet();
  return Map<String, List<MobileDirectoryApplication>>.unmodifiable({
    for (final entry in sections.entries)
      entry.key: List<MobileDirectoryApplication>.unmodifiable(
        entry.value.where((item) => seen.add(item.ref)),
      ),
  });
}

List<MobileDirectoryApplication> mobileUniqueDirectoryApplications(
  Iterable<MobileDirectoryApplication> items, {
  Iterable<EntityRef> excluded = const <EntityRef>[],
}) {
  final seen = excluded.toSet();
  return List<MobileDirectoryApplication>.unmodifiable(
    items.where((item) => seen.add(item.ref)),
  );
}

bool mobileDirectoryResponseIsCurrent({
  required int requestGeneration,
  required int currentGeneration,
  required String requestQuery,
  required String currentQuery,
}) =>
    requestGeneration == currentGeneration &&
    requestQuery == currentQuery.trim();

String mobileApplicationInstallPath(
  MobileDirectoryApplication application,
  Domain home,
) =>
    _mobileApplicationInstallPath(
      application.ref,
      application.installTemplate.slug,
      home,
    );

String mobileBotApplicationInstallPath(
  MobileBotProfileApplication application,
  Domain home,
) =>
    _mobileApplicationInstallPath(
      application.application,
      application.installTemplate.slug,
      home,
    );

String _mobileApplicationInstallPath(
  EntityRef application,
  String templateSlug,
  Domain home,
) =>
    '/applications/${Uri.encodeComponent(application.wire)}/install/'
    '${Uri.encodeComponent(templateSlug)}'
    '?instance=${Uri.encodeQueryComponent(home.value)}';

void _requireExactKeys(
  Map<String, Object?> json,
  Set<String> expected,
  String label,
) {
  if (json.length != expected.length ||
      !json.keys.toSet().containsAll(expected)) {
    throw FormatException('$label has an invalid shape.');
  }
}

List<Map<String, Object?>> _objectList(Object? value, String label) {
  if (value is! List) throw FormatException('$label must be an array.');
  final result = <Map<String, Object?>>[];
  for (final item in value) {
    if (item is! Map || item.keys.any((key) => key is! String)) {
      throw FormatException('$label contains an invalid item.');
    }
    result.add(Map<String, Object?>.from(item));
  }
  return result;
}

List<String> _stringList(
  Object? value, {
  required String label,
  required int minimum,
  required int maximum,
  Set<String>? allowed,
}) {
  if (value is! List ||
      value.length < minimum ||
      value.length > maximum ||
      value.any((item) => item is! String)) {
    throw FormatException('$label is invalid.');
  }
  final result = value.cast<String>().toList(growable: false);
  if (result.toSet().length != result.length ||
      allowed != null && result.any((item) => !allowed.contains(item))) {
    throw FormatException('$label is invalid.');
  }
  return List<String>.unmodifiable(result);
}

String _snowflake(Object? value, String label) {
  if (value is! String ||
      !_snowflakePattern.hasMatch(value) ||
      BigInt.parse(value) > BigInt.parse('9223372036854775807')) {
    throw FormatException('$label is invalid.');
  }
  return value;
}

String _boundedText(Object? value, int minimum, int maximum) {
  if (value is! String || value.length < minimum || value.length > maximum) {
    throw const FormatException('Directory text is invalid.');
  }
  return value;
}

String? _optionalBoundedText(Object? value, int maximum) {
  if (value == null) return null;
  if (value is! String || value.length > maximum) {
    throw const FormatException('Directory text is invalid.');
  }
  return value;
}

String? _assetHash(Object? value) {
  if (value == null) return null;
  if (value is! String || !_assetHashPattern.hasMatch(value)) {
    throw const FormatException('Directory asset hash is invalid.');
  }
  return value;
}

bool _listEquals<T>(List<T> left, List<T> right) {
  if (left.length != right.length) return false;
  for (var index = 0; index < left.length; index += 1) {
    if (left[index] != right[index]) return false;
  }
  return true;
}
