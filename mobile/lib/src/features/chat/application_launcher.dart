import 'dart:async';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/application_commands.dart';
import 'package:kaede_mobile/src/domain/application_directory.dart';
import 'package:kaede_mobile/src/domain/application_installations.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

typedef MobileDirectoryLoader = Future<MobileDirectoryPage> Function({
  String? query,
  String? collection,
  Domain? domain,
  required int limit,
});

typedef MobileBotProfileApplicationLoader = Future<MobileBotProfileApplication?>
    Function(EntityRef bot);

sealed class MobileAppLauncherSelection {
  const MobileAppLauncherSelection();
}

final class MobileAppLauncherCommandSelection
    extends MobileAppLauncherSelection {
  const MobileAppLauncherCommandSelection(this.command);

  final MobileApplicationCommand command;
}

final class MobileAppLauncherInstallSelection
    extends MobileAppLauncherSelection {
  const MobileAppLauncherInstallSelection(this.application);

  final MobileDirectoryApplication application;
}

final class MobileAppLauncherBotInstallSelection
    extends MobileAppLauncherSelection {
  const MobileAppLauncherBotInstallSelection(this.application);

  final MobileBotProfileApplication application;
}

final class MobileApplicationLauncherSheet extends StatefulWidget {
  const MobileApplicationLauncherSheet({
    super.key,
    required this.commands,
    required this.account,
    required this.home,
    required this.isAccountCurrent,
    required this.loadInstalledApplications,
    required this.loadRecentApplications,
    required this.loadDirectory,
    required this.loadBotProfileApplication,
    this.searchDebounce = const Duration(milliseconds: 300),
  });

  final List<MobileApplicationCommand> commands;
  final EntityRef account;
  final Domain home;
  final bool Function(EntityRef account) isAccountCurrent;
  final Future<List<UserApplicationInstallation>> Function()
      loadInstalledApplications;
  final Future<List<String>> Function() loadRecentApplications;
  final MobileDirectoryLoader loadDirectory;
  final MobileBotProfileApplicationLoader loadBotProfileApplication;
  final Duration searchDebounce;

  @override
  State<MobileApplicationLauncherSheet> createState() =>
      _MobileApplicationLauncherSheetState();
}

final class _MobileApplicationLauncherSheetState
    extends State<MobileApplicationLauncherSheet> {
  final _search = TextEditingController();
  Timer? _searchTimer;
  var _searchGeneration = 0;
  var _directoryGeneration = 0;
  var _applicationOpenGeneration = 0;
  late Domain _directoryDomain;
  var _initialLoading = true;
  var _searchLoading = false;
  var _discoveryFailed = false;
  var _searchFailed = false;
  List<String> _recentHistory = const <String>[];
  List<UserApplicationInstallation> _installations =
      const <UserApplicationInstallation>[];
  Map<String, MobileDirectoryPage> _curated =
      const <String, MobileDirectoryPage>{};
  List<MobileDirectoryApplication> _searchResults =
      const <MobileDirectoryApplication>[];
  final Map<EntityRef, Future<MobileBotProfileApplication?>>
      _applicationLookups = <EntityRef, Future<MobileBotProfileApplication?>>{};
  final Set<EntityRef> _openingApplications = <EntityRef>{};

  bool get _requestIsCurrent =>
      mounted && widget.isAccountCurrent(widget.account);

  @override
  void initState() {
    super.initState();
    _directoryDomain = widget.home;
    unawaited(_loadInitial());
  }

  @override
  void dispose() {
    _searchGeneration += 1;
    _applicationOpenGeneration += 1;
    _searchTimer?.cancel();
    _search.dispose();
    super.dispose();
  }

  Future<void> _reviewInstalledApplication(
    _LauncherApplication application,
  ) async {
    final bot = application.bot;
    if (bot == null || _openingApplications.contains(application.ref)) return;
    final generation = ++_applicationOpenGeneration;
    setState(() => _openingApplications.add(application.ref));
    final lookup = _applicationLookups.putIfAbsent(
      bot,
      () => widget.loadBotProfileApplication(bot),
    );
    try {
      final profile = await lookup;
      if (!mounted ||
          !widget.isAccountCurrent(widget.account) ||
          generation != _applicationOpenGeneration) {
        return;
      }
      if (profile == null || profile.application != application.ref) {
        _showApplicationUnavailable();
        return;
      }
      Navigator.pop(
        context,
        MobileAppLauncherBotInstallSelection(profile),
      );
    } on Object {
      if (_requestIsCurrent && generation == _applicationOpenGeneration) {
        _showApplicationUnavailable();
      }
    } finally {
      if (identical(_applicationLookups[bot], lookup)) {
        _applicationLookups.remove(bot);
      }
      if (_requestIsCurrent && mounted) {
        setState(() => _openingApplications.remove(application.ref));
      }
    }
  }

  void _showApplicationUnavailable() {
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(
        const SnackBar(content: Text('App details are unavailable.')),
      );
  }

  Future<void> _loadInitial() async {
    final work = <Future<void>>[
      _loadRecents(),
      _loadInstallations(),
      for (final collection in mobileDirectoryCollectionSlugs)
        _loadCollection(collection),
    ];
    await Future.wait(work);
    if (_requestIsCurrent) setState(() => _initialLoading = false);
  }

  Future<void> _loadRecents() async {
    try {
      final history = await widget.loadRecentApplications();
      if (_requestIsCurrent) {
        setState(
            () => _recentHistory = history.take(20).toList(growable: false));
      }
    } on Object {
      // Local ranking is optional and must not block commands or discovery.
    }
  }

  Future<void> _loadInstallations() async {
    try {
      final installations = await widget.loadInstalledApplications();
      if (_requestIsCurrent) {
        setState(() {
          _installations = installations.take(100).toList(growable: false);
        });
      }
    } on Object {
      // Installed commands remain available from channel discovery offline.
    }
  }

  Future<void> _loadCollection(String collection) async {
    final authority = _directoryDomain;
    final generation = _directoryGeneration;
    try {
      final page = await widget.loadDirectory(
        collection: collection,
        domain: authority,
        limit: 12,
      );
      if (_requestIsCurrent &&
          generation == _directoryGeneration &&
          authority == _directoryDomain) {
        setState(() {
          _curated = Map<String, MobileDirectoryPage>.unmodifiable(
            <String, MobileDirectoryPage>{..._curated, collection: page},
          );
        });
      }
    } on Object {
      if (_requestIsCurrent &&
          generation == _directoryGeneration &&
          authority == _directoryDomain) {
        setState(() => _discoveryFailed = true);
      }
    }
  }

  void _queryChanged(String value) {
    _searchTimer?.cancel();
    final query = value.trim();
    final generation = ++_searchGeneration;
    if (query.isEmpty) {
      setState(() {
        _searchLoading = false;
        _searchFailed = false;
        _searchResults = const <MobileDirectoryApplication>[];
      });
      return;
    }
    setState(() {
      _searchLoading = true;
      _searchFailed = false;
    });
    _searchTimer = Timer(widget.searchDebounce, () {
      unawaited(_searchDirectory(query, generation, _directoryDomain));
    });
  }

  Future<void> _searchDirectory(
    String query,
    int generation,
    Domain authority,
  ) async {
    try {
      final page = await widget.loadDirectory(
        query: query,
        domain: authority,
        limit: 50,
      );
      if (_requestIsCurrent &&
          authority == _directoryDomain &&
          mobileDirectoryResponseIsCurrent(
            requestGeneration: generation,
            currentGeneration: _searchGeneration,
            requestQuery: query,
            currentQuery: _search.text,
          )) {
        setState(() {
          _searchResults = page.items;
          _searchLoading = false;
          _searchFailed = false;
        });
      }
    } on Object {
      if (_requestIsCurrent &&
          authority == _directoryDomain &&
          mobileDirectoryResponseIsCurrent(
            requestGeneration: generation,
            currentGeneration: _searchGeneration,
            requestQuery: query,
            currentQuery: _search.text,
          )) {
        setState(() {
          _searchResults = const <MobileDirectoryApplication>[];
          _searchLoading = false;
          _searchFailed = true;
        });
      }
    }
  }

  Future<void> _chooseDirectoryInstance() async {
    final selected = await showDialog<Domain>(
      context: context,
      builder: (_) => _DirectoryInstanceDialog(
        current: _directoryDomain,
        home: widget.home,
      ),
    );
    if (selected == null ||
        !_requestIsCurrent ||
        selected == _directoryDomain) {
      return;
    }
    _setDirectoryInstance(selected);
  }

  void _setDirectoryInstance(Domain authority) {
    _searchTimer?.cancel();
    _directoryGeneration += 1;
    final searchGeneration = ++_searchGeneration;
    final query = _search.text.trim();
    setState(() {
      _directoryDomain = authority;
      _curated = const <String, MobileDirectoryPage>{};
      _searchResults = const <MobileDirectoryApplication>[];
      _discoveryFailed = false;
      _searchFailed = false;
      _searchLoading = query.isNotEmpty;
    });
    for (final collection in mobileDirectoryCollectionSlugs) {
      unawaited(_loadCollection(collection));
    }
    if (query.isNotEmpty) {
      _searchTimer = Timer(widget.searchDebounce, () {
        unawaited(_searchDirectory(query, searchGeneration, authority));
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final query = _search.text.trim();
    final locale = Localizations.localeOf(context).toLanguageTag();
    final commandGroups = mobileApplicationCommandLauncherGroups(
      widget.commands,
      query,
      locale,
    );
    final recentApps = _recentApplications(locale);
    final recentRefs = recentApps.map((item) => item.ref).toSet();
    final visibleCommandGroups = query.isEmpty
        ? commandGroups
            .where((group) => !recentRefs.contains(group.application))
            .toList(growable: false)
        : commandGroups;
    final installedMatches = query.isEmpty
        ? const <_LauncherApplication>[]
        : _matchingInstalledApplications(
            query,
            excluded:
                visibleCommandGroups.map((group) => group.application).toSet(),
          );
    final renderedRefs = <EntityRef>{
      ...recentRefs,
      ...visibleCommandGroups.map((group) => group.application),
      ...installedMatches.map((item) => item.ref),
    };
    final searchResults = mobileUniqueDirectoryApplications(
      _searchResults,
      excluded: renderedRefs,
    );
    final curatedSections = mobileUniqueDirectorySections(
      <String, Iterable<MobileDirectoryApplication>>{
        for (final slug in mobileDirectoryCollectionSlugs)
          slug: _curated[slug]?.items ?? const <MobileDirectoryApplication>[],
      },
      excluded: renderedRefs,
    );
    final hasCurated = curatedSections.values.any((items) => items.isNotEmpty);
    final hasSearchContent = visibleCommandGroups.isNotEmpty ||
        installedMatches.isNotEmpty ||
        searchResults.isNotEmpty;

    return FractionallySizedBox(
      heightFactor: .9,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    'Apps',
                    style: Theme.of(context).textTheme.headlineSmall,
                  ),
                ),
                IconButton(
                  tooltip: 'Close Apps',
                  onPressed: () => Navigator.pop(context),
                  icon: const Icon(Icons.close_rounded),
                ),
              ],
            ),
            const SizedBox(height: 8),
            TextField(
              key: const ValueKey('app-launcher-search'),
              controller: _search,
              maxLength: 100,
              decoration: const InputDecoration(
                labelText: 'Search apps and commands',
                prefixIcon: Icon(Icons.search_rounded),
                counterText: '',
              ),
              onChanged: _queryChanged,
            ),
            Align(
              alignment: Alignment.centerLeft,
              child: TextButton.icon(
                key: const ValueKey('app-launcher-directory-instance'),
                onPressed: _chooseDirectoryInstance,
                icon: const Icon(Icons.public_rounded, size: 18),
                label: Text(
                  _directoryDomain == widget.home
                      ? 'Directory: ${_directoryDomain.value} (Home)'
                      : 'Directory: ${_directoryDomain.value}',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
            ),
            const SizedBox(height: 2),
            Expanded(
              child: query.isEmpty
                  ? _emptyQueryContent(
                      context,
                      locale,
                      recentApps,
                      visibleCommandGroups,
                      curatedSections,
                      hasCurated,
                    )
                  : _searchContent(
                      context,
                      locale,
                      visibleCommandGroups,
                      installedMatches,
                      searchResults,
                      hasSearchContent,
                    ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _emptyQueryContent(
    BuildContext context,
    String locale,
    List<_LauncherApplication> recentApps,
    List<MobileApplicationCommandGroup> commandGroups,
    Map<String, List<MobileDirectoryApplication>> curatedSections,
    bool hasCurated,
  ) {
    if (_initialLoading && recentApps.isEmpty && commandGroups.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }
    final hasContent =
        recentApps.isNotEmpty || commandGroups.isNotEmpty || hasCurated;
    return ListView(
      key: const ValueKey('app-launcher-empty-query'),
      keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
      children: [
        if (recentApps.isNotEmpty) ...[
          _sectionHeader(context, 'Recent Apps'),
          SizedBox(
            height: 174,
            child: ListView.separated(
              scrollDirection: Axis.horizontal,
              itemCount: recentApps.length,
              separatorBuilder: (_, __) => const SizedBox(width: 10),
              itemBuilder: (context, index) =>
                  _recentCard(context, recentApps[index], locale),
            ),
          ),
          const SizedBox(height: 8),
        ],
        if (commandGroups.isNotEmpty) ...[
          _sectionHeader(context, 'Commands in this channel'),
          for (final group in commandGroups)
            _commandGroup(context, group, locale),
        ],
        for (final slug in mobileDirectoryCollectionSlugs)
          if (curatedSections[slug] case final items?
              when items.isNotEmpty) ...[
            _sectionHeader(context, _collectionName(slug)),
            SizedBox(
              height: 150,
              child: ListView.separated(
                key: ValueKey('app-launcher-collection-$slug'),
                scrollDirection: Axis.horizontal,
                itemCount: items.length,
                separatorBuilder: (_, __) => const SizedBox(width: 10),
                itemBuilder: (context, index) =>
                    _directoryCard(context, items[index]),
              ),
            ),
          ],
        if (!hasContent)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 34),
            child: Column(
              children: [
                Icon(Icons.apps_outage_rounded,
                    size: 40, color: context.kaede.muted),
                const SizedBox(height: 10),
                Text(
                  _discoveryFailed
                      ? 'App discovery is unavailable. Installed commands still work offline.'
                      : 'No apps or commands are available here yet.',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: context.kaede.muted),
                ),
              ],
            ),
          )
        else if (_discoveryFailed)
          Padding(
            padding: const EdgeInsets.only(top: 8, bottom: 12),
            child: Text(
              'Some app recommendations could not be loaded.',
              style: TextStyle(color: context.kaede.muted),
            ),
          ),
      ],
    );
  }

  Widget _searchContent(
    BuildContext context,
    String locale,
    List<MobileApplicationCommandGroup> commandGroups,
    List<_LauncherApplication> installed,
    List<MobileDirectoryApplication> directory,
    bool hasContent,
  ) =>
      ListView(
        key: const ValueKey('app-launcher-search-results'),
        keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
        children: [
          if (commandGroups.isNotEmpty) ...[
            _sectionHeader(context, 'Installed commands'),
            for (final group in commandGroups)
              _commandGroup(context, group, locale),
          ],
          if (installed.isNotEmpty) ...[
            _sectionHeader(context, 'Installed apps'),
            for (final application in installed)
              ListTile(
                key: ValueKey('installed-app-${application.ref.wire}'),
                leading: _applicationIcon(
                  application.ref.domain,
                  application.iconHash,
                ),
                title: Text(application.name),
                subtitle: Text(
                  application.summary ?? 'Installed for your account',
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
                trailing: _openingApplications.contains(application.ref)
                    ? const SizedBox.square(
                        dimension: 20,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Icon(Icons.chevron_right_rounded),
                onTap: _openingApplications.contains(application.ref)
                    ? null
                    : () => _reviewInstalledApplication(application),
              ),
          ],
          if (directory.isNotEmpty) ...[
            _sectionHeader(context, 'Discover apps'),
            for (final application in directory)
              _directoryTile(context, application),
          ],
          if (_searchLoading)
            const Padding(
              padding: EdgeInsets.all(24),
              child: Center(child: CircularProgressIndicator()),
            )
          else if (!hasContent)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 34),
              child: Text(
                _searchFailed
                    ? 'App discovery is unavailable. You can still use installed commands.'
                    : 'No apps or commands match your search.',
                textAlign: TextAlign.center,
                style: TextStyle(color: context.kaede.muted),
              ),
            )
          else if (_searchFailed)
            Padding(
              padding: const EdgeInsets.only(top: 8, bottom: 12),
              child: Text(
                'Directory search is unavailable. Installed commands are still shown.',
                style: TextStyle(color: context.kaede.muted),
              ),
            ),
        ],
      );

  Widget _sectionHeader(BuildContext context, String text) => Semantics(
        header: true,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(4, 12, 4, 7),
          child: Text(
            text,
            style: Theme.of(context)
                .textTheme
                .titleMedium
                ?.copyWith(fontWeight: FontWeight.w800),
          ),
        ),
      );

  Widget _commandGroup(
    BuildContext context,
    MobileApplicationCommandGroup group,
    String locale,
  ) =>
      Card(
        margin: const EdgeInsets.only(bottom: 9),
        child: Column(
          children: [
            ListTile(
              dense: true,
              leading: const Icon(Icons.apps_rounded),
              title: Text(group.applicationName),
              subtitle: Text(group.application.wire),
            ),
            for (final command in group.commands)
              _commandTile(context, command, locale),
          ],
        ),
      );

  Widget _commandTile(
    BuildContext context,
    MobileApplicationCommand command,
    String locale,
  ) =>
      ListTile(
        key: ValueKey(
          'app-command-${command.application.wire}-${command.id}',
        ),
        leading: const Icon(Icons.terminal_rounded),
        title: Text('/${command.displayName(locale)}'),
        subtitle: Text(
          command.displayDescription(locale).isEmpty
              ? 'Run command'
              : command.displayDescription(locale),
        ),
        onTap: () => Navigator.pop(
          context,
          MobileAppLauncherCommandSelection(command),
        ),
      );

  Widget _recentCard(
    BuildContext context,
    _LauncherApplication application,
    String locale,
  ) =>
      SizedBox(
        width: 244,
        child: Card(
          key: ValueKey('recent-app-${application.ref.wire}'),
          margin: EdgeInsets.zero,
          clipBehavior: Clip.antiAlias,
          child: InkWell(
            onTap: application.commands.isEmpty &&
                    application.bot != null &&
                    !_openingApplications.contains(application.ref)
                ? () => _reviewInstalledApplication(application)
                : null,
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      _applicationIcon(
                        application.ref.domain,
                        application.iconHash,
                      ),
                      const SizedBox(width: 9),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              application.name,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style:
                                  const TextStyle(fontWeight: FontWeight.w800),
                            ),
                            Text(
                              application.installed
                                  ? 'Installed'
                                  : 'Recently used',
                              style: TextStyle(
                                color: context.kaede.muted,
                                fontSize: 12,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  if (application.commands.isEmpty)
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Expanded(
                            child: Text(
                              application.summary ??
                                  'No commands are available in this channel.',
                              maxLines: 2,
                              overflow: TextOverflow.ellipsis,
                              style: TextStyle(color: context.kaede.muted),
                            ),
                          ),
                          if (_openingApplications.contains(application.ref))
                            const SizedBox.square(
                              dimension: 18,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          else
                            const Row(
                              children: [
                                Icon(Icons.open_in_new_rounded, size: 16),
                                SizedBox(width: 5),
                                Text('Review app'),
                              ],
                            ),
                        ],
                      ),
                    )
                  else
                    Expanded(
                      child: ListView(
                        padding: EdgeInsets.zero,
                        children: [
                          for (final command in application.commands.take(3))
                            TextButton.icon(
                              key: ValueKey(
                                'recent-command-${command.application.wire}-${command.id}',
                              ),
                              style: TextButton.styleFrom(
                                alignment: Alignment.centerLeft,
                                padding:
                                    const EdgeInsets.symmetric(horizontal: 4),
                              ),
                              onPressed: () => Navigator.pop(
                                context,
                                MobileAppLauncherCommandSelection(command),
                              ),
                              icon:
                                  const Icon(Icons.terminal_rounded, size: 17),
                              label: Text(
                                '/${command.displayName(locale)}',
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                              ),
                            ),
                        ],
                      ),
                    ),
                ],
              ),
            ),
          ),
        ),
      );

  Widget _directoryCard(
    BuildContext context,
    MobileDirectoryApplication application,
  ) =>
      SizedBox(
        width: 248,
        child: Card(
          key: ValueKey('directory-card-${application.ref.wire}'),
          margin: EdgeInsets.zero,
          clipBehavior: Clip.antiAlias,
          child: InkWell(
            onTap: () => Navigator.pop(
              context,
              MobileAppLauncherInstallSelection(application),
            ),
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  _applicationIcon(
                      application.ref.domain, application.iconHash),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          application.name,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(fontWeight: FontWeight.w800),
                        ),
                        const SizedBox(height: 4),
                        Expanded(
                          child: Text(
                            application.summary,
                            maxLines: 4,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(color: context.kaede.muted),
                          ),
                        ),
                        const Row(
                          children: [
                            Icon(Icons.add_circle_outline_rounded, size: 16),
                            SizedBox(width: 5),
                            Text('Review app'),
                          ],
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      );

  Widget _directoryTile(
    BuildContext context,
    MobileDirectoryApplication application,
  ) =>
      ListTile(
        key: ValueKey('directory-result-${application.ref.wire}'),
        leading: _applicationIcon(application.ref.domain, application.iconHash),
        title: Text(application.name),
        subtitle: Text(
          application.summary,
          maxLines: 2,
          overflow: TextOverflow.ellipsis,
        ),
        trailing: const Icon(Icons.chevron_right_rounded),
        onTap: () => Navigator.pop(
          context,
          MobileAppLauncherInstallSelection(application),
        ),
      );

  Widget _applicationIcon(Domain domain, String? hash) {
    final uri = publicAssetUri(domain, hash, variant: 'thumbnail_128');
    if (uri == null) {
      return const CircleAvatar(child: Icon(Icons.apps_rounded));
    }
    return ClipRRect(
      borderRadius: BorderRadius.circular(12),
      child: CachedNetworkImage(
        imageUrl: uri.toString(),
        width: 44,
        height: 44,
        fit: BoxFit.cover,
        placeholder: (_, __) => const SizedBox.square(
          dimension: 44,
          child: Center(child: CircularProgressIndicator(strokeWidth: 2)),
        ),
        errorWidget: (_, __, ___) => const SizedBox.square(
          dimension: 44,
          child: ColoredBox(
            color: Colors.transparent,
            child: Icon(Icons.apps_rounded),
          ),
        ),
      ),
    );
  }

  List<_LauncherApplication> _recentApplications(String locale) {
    final groups = mobileApplicationCommandLauncherGroups(
      widget.commands,
      '',
      locale,
    );
    final commandsByApp = <EntityRef, MobileApplicationCommandGroup>{
      for (final group in groups) group.application: group,
    };
    final installations = _installations
        .where((item) => item.status != 'revoked')
        .toList(growable: false)
      ..sort((left, right) {
        final leftDate = left.updatedAt ?? left.createdAt;
        final rightDate = right.updatedAt ?? right.createdAt;
        if (leftDate == null && rightDate == null) {
          return left.application.wire.compareTo(right.application.wire);
        }
        if (leftDate == null) return 1;
        if (rightDate == null) return -1;
        return rightDate.compareTo(leftDate);
      });
    final installationByApp = <EntityRef, UserApplicationInstallation>{
      for (final item in installations) item.application: item,
    };
    final orderedRefs = <EntityRef>[
      ...mobileRecentApplicationRefs(_recentHistory),
      ...installations.map((item) => item.application),
    ];
    final seen = <EntityRef>{};
    final result = <_LauncherApplication>[];
    for (final application in orderedRefs) {
      if (!seen.add(application)) continue;
      final group = commandsByApp[application];
      final installation = installationByApp[application];
      if (group == null && installation == null) continue;
      result.add(
        _LauncherApplication(
          ref: application,
          name: installation?.applicationName ?? group!.applicationName,
          summary: installation?.applicationDescription,
          iconHash: installation?.applicationIconHash,
          bot: installation?.botUser,
          installed: installation != null,
          commands: group?.commands ?? const <MobileApplicationCommand>[],
        ),
      );
      if (result.length == 8) break;
    }
    return List<_LauncherApplication>.unmodifiable(result);
  }

  List<_LauncherApplication> _matchingInstalledApplications(
    String query, {
    required Set<EntityRef> excluded,
  }) {
    final needle = query.toLowerCase();
    final seen = <EntityRef>{...excluded};
    return List<_LauncherApplication>.unmodifiable(
      _installations
          .where((item) => item.status != 'revoked')
          .where((item) => <String>[
                item.applicationName,
                item.applicationDescription ?? '',
              ].join(' ').toLowerCase().contains(needle))
          .where((item) => seen.add(item.application))
          .take(20)
          .map(
            (item) => _LauncherApplication(
              ref: item.application,
              name: item.applicationName,
              summary: item.applicationDescription,
              iconHash: item.applicationIconHash,
              bot: item.botUser,
              installed: true,
              commands: const <MobileApplicationCommand>[],
            ),
          ),
    );
  }
}

final class _LauncherApplication {
  const _LauncherApplication({
    required this.ref,
    required this.name,
    required this.summary,
    required this.iconHash,
    required this.bot,
    required this.installed,
    required this.commands,
  });

  final EntityRef ref;
  final String name;
  final String? summary;
  final String? iconHash;
  final EntityRef? bot;
  final bool installed;
  final List<MobileApplicationCommand> commands;
}

final class _DirectoryInstanceDialog extends StatefulWidget {
  const _DirectoryInstanceDialog({required this.current, required this.home});

  final Domain current;
  final Domain home;

  @override
  State<_DirectoryInstanceDialog> createState() =>
      _DirectoryInstanceDialogState();
}

final class _DirectoryInstanceDialogState
    extends State<_DirectoryInstanceDialog> {
  late final TextEditingController _controller;
  String? _error;

  @override
  void initState() {
    super.initState();
    _controller = TextEditingController(text: widget.current.value);
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  void _apply() {
    try {
      Navigator.pop(context, Domain(_controller.text));
    } on FormatException {
      setState(() => _error = 'Enter a valid domain.');
    }
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: const Text('Directory instance'),
        content: TextField(
          key: const ValueKey('app-launcher-directory-domain-input'),
          controller: _controller,
          autofocus: true,
          autocorrect: false,
          keyboardType: TextInputType.url,
          maxLength: 253,
          decoration: InputDecoration(
            labelText: 'Instance domain',
            helperText: 'Apps are reviewed by this instance.',
            errorText: _error,
            counterText: '',
          ),
          onSubmitted: (_) => _apply(),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('Cancel'),
          ),
          TextButton(
            key: const ValueKey('app-launcher-directory-use-home'),
            onPressed: () => Navigator.pop(context, widget.home),
            child: const Text('Use home'),
          ),
          FilledButton(
            key: const ValueKey('app-launcher-directory-apply'),
            onPressed: _apply,
            child: const Text('Apply'),
          ),
        ],
      );
}

String _collectionName(String slug) => switch (slug) {
      'featured' => 'Featured',
      'staff-picks' => 'Staff Picks',
      'new-and-noteworthy' => 'New & Noteworthy',
      _ => 'Discover',
    };
