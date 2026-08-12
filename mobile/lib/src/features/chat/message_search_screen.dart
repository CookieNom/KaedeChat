import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:shared_preferences/shared_preferences.dart';

final class MessageSearchScreen extends StatefulWidget {
  const MessageSearchScreen({
    required this.repository,
    required this.scope,
    required this.scopeRef,
    required this.channel,
    required this.accountRef,
    this.users = const <KaedeUser>[],
    required this.onJump,
    super.key,
  });

  final KaedeRepository repository;
  final String scope;
  final EntityRef? scopeRef;
  final KaedeChannel? channel;
  final EntityRef? accountRef;
  final List<KaedeUser> users;
  final Future<void> Function(MessageSearchResult result) onJump;

  @override
  State<MessageSearchScreen> createState() => _MessageSearchScreenState();
}

final class _MessageSearchScreenState extends State<MessageSearchScreen> {
  final _query = TextEditingController();
  var _sort = 'relevance';
  final _has = <String>{};
  bool? _pinned;
  String? _authorType;
  EntityRef? _author;
  EntityRef? _mention;
  DateTime? _after;
  DateTime? _before;
  List<String> _history = const <String>[];
  MessageSearchPage? _page;
  bool? _featureAvailable;
  var _loading = false;
  String? _error;

  bool get _encrypted =>
      widget.scope == 'channel' &&
      (widget.channel?.encryptionMode == 'e2ee' ||
          widget.channel?.searchAvailable == false);

  bool get _hasCriteria =>
      _query.text.trim().isNotEmpty ||
      _author != null ||
      _mention != null ||
      _has.isNotEmpty ||
      _pinned != null ||
      _authorType != null ||
      _after != null ||
      _before != null;

  String? get _historyKey => widget.accountRef == null
      ? null
      : 'kaede.message-search.history.${widget.accountRef!.wire}';

  @override
  void initState() {
    super.initState();
    _loadHistory();
    _loadAvailability();
  }

  Future<void> _loadAvailability() async {
    final account = widget.accountRef;
    if (account == null) return;
    try {
      final configuration = await widget.repository.authConfig(account.domain);
      if (!mounted) return;
      setState(() =>
          _featureAvailable = configuration['message_search_enabled'] == true);
    } on Object {
      // Searching retains its normal structured error if config discovery is
      // temporarily unavailable.
    }
  }

  Future<void> _loadHistory() async {
    final key = _historyKey;
    if (key == null) return;
    final preferences = await SharedPreferences.getInstance();
    if (!mounted) return;
    setState(
        () => _history = preferences.getStringList(key) ?? const <String>[]);
  }

  Future<void> _rememberSearch() async {
    final key = _historyKey;
    final query = _query.text.trim();
    if (key == null || query.isEmpty) return;
    final next = <String>[
      query,
      ..._history.where((item) => item != query),
    ].take(8).toList(growable: false);
    if (mounted) setState(() => _history = next);
    final preferences = await SharedPreferences.getInstance();
    await preferences.setStringList(key, next);
  }

  Future<void> _clearHistory() async {
    final key = _historyKey;
    if (mounted) setState(() => _history = const <String>[]);
    if (key != null) {
      final preferences = await SharedPreferences.getInstance();
      await preferences.remove(key);
    }
  }

  void _clearFilters() {
    setState(() {
      _sort = 'relevance';
      _has.clear();
      _pinned = null;
      _authorType = null;
      _author = null;
      _mention = null;
      _after = null;
      _before = null;
    });
  }

  @override
  void dispose() {
    _query.dispose();
    super.dispose();
  }

  Future<void> _search({bool more = false}) async {
    if (_loading ||
        _encrypted ||
        _featureAvailable == false ||
        (!more && !_hasCriteria)) {
      return;
    }
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final next = await widget.repository.searchMessages(
        query: _query.text.trim(),
        scope: widget.scope,
        scopeRef: widget.scopeRef,
        sort: _sort,
        has: _has.toList(growable: false),
        pinned: _pinned,
        authorType: _authorType,
        authors: _author == null ? const <EntityRef>[] : <EntityRef>[_author!],
        mentions:
            _mention == null ? const <EntityRef>[] : <EntityRef>[_mention!],
        after: _after,
        before: _before == null
            ? null
            : DateTime.utc(
                _before!.year, _before!.month, _before!.day, 23, 59, 59),
        cursor: more ? _page?.nextCursor : null,
      );
      if (!mounted) return;
      setState(() {
        _page = more && _page != null
            ? MessageSearchPage(
                results: <MessageSearchResult>[
                  ..._page!.results,
                  ...next.results,
                ],
                localCoverage: next.localCoverage,
                authorityCoverage: next.authorityCoverage,
                nextCursor: next.nextCursor,
                encryptedChannelRefs: next.encryptedChannelRefs,
                indexing: next.indexing,
              )
            : next;
      });
      await _rememberSearch();
    } on Object catch (error) {
      if (!mounted) return;
      setState(() => _error = userFacingError(
            error,
            summary: 'Could not search messages.',
          ));
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final users = <EntityRef, KaedeUser>{
      for (final user in widget.users) user.ref: user,
    }.values.toList()
      ..sort((left, right) => left.name.compareTo(right.name));
    return Scaffold(
      appBar: AppBar(title: const Text('Search messages')),
      body: _encrypted
          ? const Center(
              child: Padding(
                padding: EdgeInsets.all(28),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(Icons.lock_rounded, size: 44),
                    SizedBox(height: 16),
                    Text(
                        'Search is unavailable for this encrypted conversation.',
                        textAlign: TextAlign.center,
                        style: TextStyle(
                            fontSize: 18, fontWeight: FontWeight.w800)),
                    SizedBox(height: 8),
                    Text(
                      'End-to-end encrypted message bodies never leave your devices and are never indexed by Kaede.',
                      textAlign: TextAlign.center,
                    ),
                  ],
                ),
              ),
            )
          : _featureAvailable == false
              ? const Center(
                  child: Padding(
                    padding: EdgeInsets.all(28),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Icon(Icons.search_off_rounded, size: 44),
                        SizedBox(height: 16),
                        Text(
                          'Message search is disabled on this instance.',
                          textAlign: TextAlign.center,
                          style: TextStyle(
                              fontSize: 18, fontWeight: FontWeight.w800),
                        ),
                        SizedBox(height: 8),
                        Text(
                          'Your instance administrator can enable the private search service during setup.',
                          textAlign: TextAlign.center,
                        ),
                      ],
                    ),
                  ),
                )
              : Column(
                  children: [
                    Padding(
                      padding: const EdgeInsets.all(16),
                      child: Column(
                        children: [
                          SearchBar(
                            controller: _query,
                            hintText: 'Search messages',
                            leading: const Icon(Icons.search_rounded),
                            onChanged: (_) => setState(() {}),
                            onSubmitted: (_) => _search(),
                          ),
                          const SizedBox(height: 12),
                          Row(
                            children: [
                              Expanded(
                                child: DropdownButtonFormField<String>(
                                  key: ValueKey('search-sort-$_sort'),
                                  initialValue: _sort,
                                  decoration:
                                      const InputDecoration(labelText: 'Sort'),
                                  items: const [
                                    DropdownMenuItem(
                                        value: 'relevance',
                                        child: Text('Most relevant')),
                                    DropdownMenuItem(
                                        value: 'newest', child: Text('Newest')),
                                    DropdownMenuItem(
                                        value: 'oldest', child: Text('Oldest')),
                                  ],
                                  onChanged: (value) => setState(
                                      () => _sort = value ?? 'relevance'),
                                ),
                              ),
                              const SizedBox(width: 12),
                              Expanded(
                                child: DropdownButtonFormField<bool?>(
                                  key: ValueKey('search-pinned-$_pinned'),
                                  initialValue: _pinned,
                                  decoration: const InputDecoration(
                                      labelText: 'Pinned'),
                                  items: const [
                                    DropdownMenuItem(
                                        value: null, child: Text('Either')),
                                    DropdownMenuItem(
                                        value: true, child: Text('Pinned')),
                                    DropdownMenuItem(
                                        value: false,
                                        child: Text('Not pinned')),
                                  ],
                                  onChanged: (value) =>
                                      setState(() => _pinned = value),
                                ),
                              ),
                            ],
                          ),
                          const SizedBox(height: 10),
                          DropdownButtonFormField<String?>(
                            key: ValueKey('search-author-type-$_authorType'),
                            initialValue: _authorType,
                            decoration:
                                const InputDecoration(labelText: 'Author type'),
                            items: const <DropdownMenuItem<String?>>[
                              DropdownMenuItem<String?>(
                                  value: null, child: Text('Anyone')),
                              DropdownMenuItem<String?>(
                                  value: 'user', child: Text('People')),
                              DropdownMenuItem<String?>(
                                  value: 'webhook', child: Text('Webhooks')),
                            ],
                            onChanged: (value) =>
                                setState(() => _authorType = value),
                          ),
                          const SizedBox(height: 10),
                          Row(
                            children: [
                              Expanded(
                                child: DropdownButtonFormField<EntityRef?>(
                                  key: ValueKey('search-author-$_author'),
                                  initialValue: _author,
                                  decoration:
                                      const InputDecoration(labelText: 'From'),
                                  items: <DropdownMenuItem<EntityRef?>>[
                                    const DropdownMenuItem<EntityRef?>(
                                        value: null, child: Text('Anyone')),
                                    for (final user in users)
                                      DropdownMenuItem<EntityRef?>(
                                          value: user.ref,
                                          child: Text(user.name,
                                              overflow: TextOverflow.ellipsis)),
                                  ],
                                  onChanged: (value) =>
                                      setState(() => _author = value),
                                ),
                              ),
                              const SizedBox(width: 12),
                              Expanded(
                                child: DropdownButtonFormField<EntityRef?>(
                                  key: ValueKey('search-mention-$_mention'),
                                  initialValue: _mention,
                                  decoration: const InputDecoration(
                                      labelText: 'Mentions'),
                                  items: <DropdownMenuItem<EntityRef?>>[
                                    const DropdownMenuItem<EntityRef?>(
                                        value: null, child: Text('Anyone')),
                                    for (final user in users)
                                      DropdownMenuItem<EntityRef?>(
                                          value: user.ref,
                                          child: Text(user.name,
                                              overflow: TextOverflow.ellipsis)),
                                  ],
                                  onChanged: (value) =>
                                      setState(() => _mention = value),
                                ),
                              ),
                            ],
                          ),
                          const SizedBox(height: 10),
                          Row(
                            children: [
                              Expanded(
                                child: OutlinedButton.icon(
                                  icon: const Icon(Icons.date_range_rounded),
                                  label: Text(_after == null
                                      ? 'After date'
                                      : MaterialLocalizations.of(context)
                                          .formatMediumDate(_after!)),
                                  onPressed: () async {
                                    final value = await showDatePicker(
                                      context: context,
                                      firstDate: DateTime(2020),
                                      lastDate: DateTime.now(),
                                      initialDate: _after ?? DateTime.now(),
                                    );
                                    if (value != null) {
                                      setState(() => _after = value);
                                    }
                                  },
                                ),
                              ),
                              const SizedBox(width: 12),
                              Expanded(
                                child: OutlinedButton.icon(
                                  icon: const Icon(Icons.event_rounded),
                                  label: Text(_before == null
                                      ? 'Before date'
                                      : MaterialLocalizations.of(context)
                                          .formatMediumDate(_before!)),
                                  onPressed: () async {
                                    final value = await showDatePicker(
                                      context: context,
                                      firstDate: DateTime(2020),
                                      lastDate: DateTime.now(),
                                      initialDate: _before ?? DateTime.now(),
                                    );
                                    if (value != null) {
                                      setState(() => _before = value);
                                    }
                                  },
                                ),
                              ),
                            ],
                          ),
                          const SizedBox(height: 10),
                          Wrap(
                            spacing: 8,
                            children: [
                              for (final kind in const [
                                'image',
                                'video',
                                'audio',
                                'file',
                                'link',
                                'embed'
                              ])
                                FilterChip(
                                  label: Text(kind),
                                  selected: _has.contains(kind),
                                  onSelected: (selected) => setState(() {
                                    if (selected) {
                                      _has.add(kind);
                                    } else {
                                      _has.remove(kind);
                                    }
                                  }),
                                ),
                            ],
                          ),
                          Align(
                            alignment: Alignment.centerLeft,
                            child: TextButton(
                              onPressed: _clearFilters,
                              child: const Text('Clear filters'),
                            ),
                          ),
                          SizedBox(
                            width: double.infinity,
                            child: FilledButton.icon(
                              onPressed:
                                  _loading || !_hasCriteria ? null : _search,
                              icon: const Icon(Icons.search_rounded),
                              label: Text(_loading ? 'Searching…' : 'Search'),
                            ),
                          ),
                          if (_history.isNotEmpty) ...[
                            const SizedBox(height: 10),
                            Row(
                              children: [
                                const Expanded(
                                    child: Text('Recent searches',
                                        style: TextStyle(
                                            fontWeight: FontWeight.w700))),
                                TextButton(
                                    onPressed: _clearHistory,
                                    child: const Text('Clear')),
                              ],
                            ),
                            Align(
                              alignment: Alignment.centerLeft,
                              child: Wrap(
                                spacing: 8,
                                children: [
                                  for (final item in _history)
                                    ActionChip(
                                      label: Text(item),
                                      onPressed: () {
                                        _query.text = item;
                                        _search();
                                      },
                                    ),
                                ],
                              ),
                            ),
                          ],
                          if (_error case final error?)
                            Padding(
                              padding: const EdgeInsets.only(top: 8),
                              child: Text(error,
                                  style: TextStyle(
                                      color:
                                          Theme.of(context).colorScheme.error)),
                            ),
                          if (_page?.authorityCoverage
                              case 'unavailable' || 'unsupported')
                            const Padding(
                              padding: EdgeInsets.only(top: 8),
                              child: Text(
                                  'Showing locally cached matches. The home instance could not provide complete results.'),
                            ),
                          if (_page?.localCoverage == 'cached' &&
                              _page?.authorityCoverage == 'not_queried')
                            const Padding(
                              padding: EdgeInsets.only(top: 8),
                              child: Text(
                                  'Account-wide direct-message search uses this home’s recent federated cache. Search inside a conversation for complete results from its authority.'),
                            ),
                          if (_page?.indexing == true)
                            const Padding(
                              padding: EdgeInsets.only(top: 8),
                              child: Text(
                                  'Search is catching up with recent messages. Results may be incomplete for a moment.'),
                            ),
                          if (_page?.encryptedChannelRefs.isNotEmpty == true)
                            const Padding(
                              padding: EdgeInsets.only(top: 8),
                              child: Text(
                                  'Encrypted conversations were excluded from these results.'),
                            ),
                        ],
                      ),
                    ),
                    Expanded(
                      child: _page != null && _page!.results.isEmpty
                          ? const Center(
                              child: Padding(
                                padding: EdgeInsets.all(24),
                                child: Text(
                                  'No messages matched those filters.',
                                  textAlign: TextAlign.center,
                                ),
                              ),
                            )
                          : ListView.builder(
                              itemCount: (_page?.results.length ?? 0) +
                                  (_page?.nextCursor != null ? 1 : 0),
                              itemBuilder: (context, index) {
                                final results = _page?.results ??
                                    const <MessageSearchResult>[];
                                if (index == results.length) {
                                  return TextButton(
                                    onPressed: _loading
                                        ? null
                                        : () => _search(more: true),
                                    child: const Text('Load more'),
                                  );
                                }
                                final result = results[index];
                                final recipients = result.channel.recipients
                                    .map((user) => user.name)
                                    .join(', ');
                                final contextLabel = result.guild == null
                                    ? (recipients.isEmpty
                                        ? 'Direct message'
                                        : recipients)
                                    : '${result.guild!.name} · #${result.channel.name ?? 'channel'}';
                                return ListTile(
                                  title: Text(result.message.author?.name ??
                                      'Unknown sender'),
                                  subtitle: Column(
                                    crossAxisAlignment:
                                        CrossAxisAlignment.start,
                                    children: [
                                      Text(
                                        contextLabel,
                                        maxLines: 1,
                                        overflow: TextOverflow.ellipsis,
                                        style: Theme.of(context)
                                            .textTheme
                                            .labelMedium,
                                      ),
                                      Text(result.snippet,
                                          maxLines: 3,
                                          overflow: TextOverflow.ellipsis),
                                    ],
                                  ),
                                  trailing: Text(
                                      MaterialLocalizations.of(context)
                                          .formatTimeOfDay(
                                    TimeOfDay.fromDateTime(
                                        result.message.createdAt.toLocal()),
                                  )),
                                  onTap: () => widget.onJump(result),
                                );
                              },
                            ),
                    ),
                  ],
                ),
    );
  }
}
