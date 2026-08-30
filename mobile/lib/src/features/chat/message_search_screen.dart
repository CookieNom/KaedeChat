import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/shared/remote_media.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:shared_preferences/shared_preferences.dart';

enum MessageSearchOperator { from, mentions, has }

final class MessageSearchOperatorMatch {
  MessageSearchOperatorMatch({
    required this.operator,
    required this.needle,
    required this.start,
  });

  final MessageSearchOperator operator;
  final String needle;
  final int start;
}

MessageSearchOperatorMatch? messageSearchOperator(String query) {
  final match = RegExp(
    r'(?:^|\s)(from|mentions|has):([^\s]*)$',
    caseSensitive: false,
  ).firstMatch(query);
  if (match == null) return null;
  return MessageSearchOperatorMatch(
    operator:
        MessageSearchOperator.values.byName(match.group(1)!.toLowerCase()),
    needle: match.group(2)!.toLowerCase(),
    start: match.start,
  );
}

String beginMessageSearchOperator(
  String query,
  MessageSearchOperator operator,
) {
  final prefix = query.trimRight();
  return '$prefix${prefix.isEmpty ? '' : ' '}${operator.name}:';
}

String replaceMessageSearchOperator(String query, [String replacement = '']) {
  final match = messageSearchOperator(query);
  if (match == null) return query;
  final prefix = query.substring(0, match.start).trimRight();
  return <String>[prefix, replacement]
      .where((part) => part.isNotEmpty)
      .join(' ');
}

/// Keeps federated identities distinct even when their names are identical.
List<KaedeUser> messageSearchUserCandidates(Iterable<KaedeUser?> users) {
  final candidates = <EntityRef, KaedeUser>{};
  for (final user in users) {
    if (user != null) candidates.putIfAbsent(user.ref, () => user);
  }
  return candidates.values.toList(growable: false)
    ..sort((left, right) {
      final byName =
          left.name.toLowerCase().compareTo(right.name.toLowerCase());
      return byName != 0
          ? byName
          : left.handle.toLowerCase().compareTo(right.handle.toLowerCase());
    });
}

List<KaedeUser> filterMessageSearchUsers(
  Iterable<KaedeUser> users,
  String needle,
) {
  final normalized = needle.trim().toLowerCase();
  if (normalized.isEmpty) return users.toList(growable: false);
  return users
      .where((user) =>
          '${user.name} ${user.handle}'.toLowerCase().contains(normalized))
      .toList(growable: false);
}

/// Immutable request inputs used to bind a result page to its search.
final class MessageSearchCriteria {
  MessageSearchCriteria({
    required String query,
    required this.scope,
    required this.scopeRef,
    required this.sort,
    Iterable<String> has = const <String>[],
    required this.pinned,
    required this.authorType,
    required this.author,
    required this.mention,
    required this.after,
    required this.before,
  })  : query = query.trim(),
        has = List<String>.unmodifiable(List<String>.of(has)..sort());

  final String query;
  final String scope;
  final EntityRef? scopeRef;
  final String sort;
  final List<String> has;
  final bool? pinned;
  final String? authorType;
  final EntityRef? author;
  final EntityRef? mention;
  final DateTime? after;
  final DateTime? before;

  String get signature => jsonEncode(<Object?>[
        query,
        scope,
        scopeRef?.wire,
        sort,
        has,
        pinned,
        authorType,
        author?.wire,
        mention?.wire,
        after?.toUtc().toIso8601String(),
        before?.toUtc().toIso8601String(),
      ]);
}

/// The backend's `before` filter is exclusive, so a selected calendar date
/// maps to the following midnight in the device's local timezone.
DateTime messageSearchBeforeCutoff(DateTime selectedDay) => DateTime(
      selectedDay.year,
      selectedDay.month,
      selectedDay.day + 1,
    );

bool messageSearchResponseIsCurrent({
  required int requestGeneration,
  required int currentGeneration,
  required String requestSignature,
  required String currentSignature,
}) =>
    requestGeneration == currentGeneration &&
    requestSignature == currentSignature;

bool messageSearchCanLoadMore({
  required String? pageSignature,
  required String currentSignature,
  required String? nextCursor,
}) =>
    nextCursor != null && pageSignature == currentSignature;

/// Search snippets can be authority-provided and truncated mid-spoiler. Hide
/// both complete `||...||` spans and the remainder of an unmatched opening
/// span so search never becomes a side channel for concealed message text.
String messageSearchSafeSnippet(String snippet) {
  final output = StringBuffer();
  var cursor = 0;
  while (cursor < snippet.length) {
    final start = snippet.indexOf('||', cursor);
    if (start < 0) {
      output.write(snippet.substring(cursor));
      break;
    }
    output
      ..write(snippet.substring(cursor, start))
      ..write('Spoiler');
    final end = snippet.indexOf('||', start + 2);
    if (end < 0) break;
    cursor = end + 2;
  }
  return output.toString();
}

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
  final _queryFocus = FocusNode();
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
  String? _pageSignature;
  bool? _featureAvailable;
  var _loading = false;
  var _jumping = false;
  var _searchRequestGeneration = 0;
  var _loadingUsers = false;
  var _userLoadGeneration = 0;
  Timer? _userSearchDebounce;
  List<KaedeUser> _users = const <KaedeUser>[];
  String? _userLoadError;
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

  MessageSearchOperatorMatch? get _operatorMatch =>
      messageSearchOperator(_query.text);

  bool get _canSearch => _hasCriteria && _operatorMatch == null;

  MessageSearchCriteria get _criteria => MessageSearchCriteria(
        query: _query.text,
        scope: widget.scope,
        scopeRef: widget.scopeRef,
        sort: _sort,
        has: _has,
        pinned: _pinned,
        authorType: _authorType,
        author: _author,
        mention: _mention,
        after: _after,
        before: _before == null ? null : messageSearchBeforeCutoff(_before!),
      );

  String? get _historyKey => widget.accountRef == null
      ? null
      : 'kaede.message-search.history.${widget.accountRef!.wire}';

  @override
  void initState() {
    super.initState();
    _queryFocus.addListener(_queryFocusChanged);
    _users = messageSearchUserCandidates(<KaedeUser?>[
      ...widget.users,
      ...?widget.channel?.recipients,
    ]);
    _loadHistory();
    _loadAvailability();
    _loadScopeUsers();
  }

  @override
  void didUpdateWidget(covariant MessageSearchScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.repository, widget.repository) ||
        oldWidget.scope != widget.scope ||
        oldWidget.scopeRef != widget.scopeRef) {
      _invalidateSearch();
    }
    if (oldWidget.scope != widget.scope ||
        oldWidget.scopeRef != widget.scopeRef ||
        oldWidget.channel?.ref != widget.channel?.ref ||
        !identical(oldWidget.users, widget.users)) {
      _users = messageSearchUserCandidates(<KaedeUser?>[
        ..._users,
        ...widget.users,
        ...?widget.channel?.recipients,
      ]);
      if (oldWidget.scope != widget.scope ||
          oldWidget.scopeRef != widget.scopeRef) {
        _loadScopeUsers();
      }
    }
  }

  void _queryFocusChanged() {
    if (mounted) setState(() {});
  }

  void _invalidateSearch() {
    _searchRequestGeneration += 1;
    _loading = false;
    _page = null;
    _pageSignature = null;
    _error = null;
  }

  void _changeCriteria(VoidCallback change) {
    setState(() {
      change();
      _invalidateSearch();
    });
  }

  Future<void> _loadScopeUsers({String query = ''}) async {
    if (widget.scope != 'guild' || widget.scopeRef == null) return;
    final generation = ++_userLoadGeneration;
    setState(() {
      _loadingUsers = true;
      _userLoadError = null;
    });
    try {
      final members = await widget.repository.members(
        widget.scopeRef!,
        query: query.trim().isEmpty ? null : query.trim(),
      );
      if (!mounted || generation != _userLoadGeneration) return;
      setState(() {
        _users = messageSearchUserCandidates(<KaedeUser?>[
          ..._users,
          ...members.map((member) => member.user),
        ]);
      });
    } on Object catch (error) {
      if (!mounted || generation != _userLoadGeneration) return;
      setState(() {
        _userLoadError = userFacingError(
          error,
          summary: 'Could not load the member list.',
        );
      });
    } finally {
      if (mounted && generation == _userLoadGeneration) {
        setState(() => _loadingUsers = false);
      }
    }
  }

  void _queryChanged(String value) {
    _changeCriteria(() {});
    _userSearchDebounce?.cancel();
    final match = messageSearchOperator(value);
    if (widget.scope != 'guild' ||
        match == null ||
        (match.operator != MessageSearchOperator.from &&
            match.operator != MessageSearchOperator.mentions)) {
      return;
    }
    _userSearchDebounce = Timer(
      Duration(milliseconds: 250),
      () => _loadScopeUsers(query: match.needle),
    );
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

  Future<void> _rememberSearch(String rawQuery) async {
    final key = _historyKey;
    final query = rawQuery.trim();
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
    _changeCriteria(() {
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
    _userSearchDebounce?.cancel();
    _queryFocus
      ..removeListener(_queryFocusChanged)
      ..dispose();
    _query.dispose();
    super.dispose();
  }

  void _beginOperator(MessageSearchOperator operator) {
    _query.text = beginMessageSearchOperator(_query.text, operator);
    _query.selection = TextSelection.collapsed(offset: _query.text.length);
    _queryFocus.requestFocus();
    _queryChanged(_query.text);
  }

  void _selectOperatorUser(
    MessageSearchOperator operator,
    KaedeUser user,
  ) {
    _changeCriteria(() {
      _query.text = replaceMessageSearchOperator(_query.text);
      _query.selection = TextSelection.collapsed(offset: _query.text.length);
      if (operator == MessageSearchOperator.from) {
        _author = user.ref;
      } else {
        _mention = user.ref;
      }
    });
    _queryFocus.unfocus();
    unawaited(_search());
  }

  void _selectContentKind(String kind) {
    _changeCriteria(() {
      _query.text = replaceMessageSearchOperator(_query.text);
      _query.selection = TextSelection.collapsed(offset: _query.text.length);
      _has.add(kind);
    });
    _queryFocus.unfocus();
    unawaited(_search());
  }

  void _submitQuery() {
    final match = _operatorMatch;
    if (match?.operator == MessageSearchOperator.has) {
      final kinds = _matchingContentKinds(match!.needle);
      if (kinds.isNotEmpty) {
        _selectContentKind(kinds.first);
        return;
      }
    }
    if (match?.operator == MessageSearchOperator.from ||
        match?.operator == MessageSearchOperator.mentions) {
      final users = filterMessageSearchUsers(_users, match!.needle);
      if (users.isNotEmpty) {
        _selectOperatorUser(match.operator, users.first);
        return;
      }
    }
    _search();
  }

  static const _contentKinds = <String>[
    'image',
    'video',
    'audio',
    'file',
    'link',
    'embed',
  ];

  List<String> _matchingContentKinds(String needle) {
    final normalized = needle.toLowerCase();
    return _contentKinds
        .where((kind) => normalized.isEmpty || kind.contains(normalized))
        .toList(growable: false);
  }

  Future<void> _pickUser({required bool mentions}) async {
    final selected = await showModalBottomSheet<KaedeUser>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (context) => _MessageSearchUserPicker(
        title: mentions ? 'Mentioned member' : 'Message author',
        users: _users,
        remoteSearch: widget.scope == 'guild' && widget.scopeRef != null
            ? (query) async => (await widget.repository.members(
                  widget.scopeRef!,
                  query: query.trim().isEmpty ? null : query.trim(),
                ))
                    .map((member) => member.user)
                    .toList(growable: false)
            : null,
      ),
    );
    if (selected == null || !mounted) return;
    _changeCriteria(() {
      _users = messageSearchUserCandidates(<KaedeUser?>[
        ..._users,
        selected,
      ]);
      if (mentions) {
        _mention = selected.ref;
      } else {
        _author = selected.ref;
      }
    });
  }

  KaedeUser? _userFor(EntityRef? reference) {
    if (reference == null) return null;
    for (final user in _users) {
      if (user.ref == reference) return user;
    }
    return null;
  }

  Future<void> _search({bool more = false}) async {
    if (_loading ||
        _encrypted ||
        _featureAvailable == false ||
        (!more && !_canSearch)) {
      return;
    }

    final criteria = _criteria;
    final signature = criteria.signature;
    final previousPage = _page;
    if (more &&
        !messageSearchCanLoadMore(
          pageSignature: _pageSignature,
          currentSignature: signature,
          nextCursor: previousPage?.nextCursor,
        )) {
      return;
    }
    final generation = ++_searchRequestGeneration;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final next = await widget.repository.searchMessages(
        query: criteria.query,
        scope: criteria.scope,
        scopeRef: criteria.scopeRef,
        sort: criteria.sort,
        has: criteria.has,
        pinned: criteria.pinned,
        authorType: criteria.authorType,
        authors: criteria.author == null
            ? const <EntityRef>[]
            : <EntityRef>[criteria.author!],
        mentions: criteria.mention == null
            ? const <EntityRef>[]
            : <EntityRef>[criteria.mention!],
        after: criteria.after,
        before: criteria.before,
        cursor: more ? previousPage!.nextCursor : null,
      );
      if (!mounted ||
          !messageSearchResponseIsCurrent(
            requestGeneration: generation,
            currentGeneration: _searchRequestGeneration,
            requestSignature: signature,
            currentSignature: _criteria.signature,
          )) {
        return;
      }
      setState(() {
        _page = more
            ? MessageSearchPage(
                results: <MessageSearchResult>[
                  ...previousPage!.results,
                  ...next.results,
                ],
                localCoverage: next.localCoverage,
                authorityCoverage: next.authorityCoverage,
                nextCursor: next.nextCursor,
                encryptedChannelRefs: next.encryptedChannelRefs,
                indexing: next.indexing,
              )
            : next;
        _pageSignature = signature;
      });
      await _rememberSearch(criteria.query);
    } on Object catch (error) {
      if (!mounted ||
          !messageSearchResponseIsCurrent(
            requestGeneration: generation,
            currentGeneration: _searchRequestGeneration,
            requestSignature: signature,
            currentSignature: _criteria.signature,
          )) {
        return;
      }
      setState(() => _error = userFacingError(
            error,
            summary: 'Could not search messages.',
          ));
    } finally {
      if (mounted && generation == _searchRequestGeneration) {
        setState(() => _loading = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text('Search messages')),
      body: _encrypted
          ? Center(
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
              ? Center(
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
              : ListView(
                  keyboardDismissBehavior:
                      ScrollViewKeyboardDismissBehavior.onDrag,
                  padding: EdgeInsets.fromLTRB(16, 16, 16, 28),
                  children: [
                    SearchBar(
                      key: ValueKey('message-search-query'),
                      controller: _query,
                      focusNode: _queryFocus,
                      hintText: 'Search messages or type from:',
                      leading: Icon(Icons.search_rounded),
                      trailing: _query.text.isEmpty
                          ? null
                          : <Widget>[
                              IconButton(
                                tooltip: 'Clear search text',
                                onPressed: () {
                                  _query.clear();
                                  _queryChanged('');
                                },
                                icon: Icon(Icons.close_rounded),
                              ),
                            ],
                      onChanged: _queryChanged,
                      onSubmitted: (_) => _submitQuery(),
                    ),
                    if (_queryFocus.hasFocus) ...[
                      SizedBox(height: 8),
                      _operatorSuggestions(),
                    ],
                    SizedBox(height: 12),
                    Row(
                      children: [
                        Expanded(
                          child: DropdownButtonFormField<String>(
                            key: ValueKey('search-sort-$_sort'),
                            initialValue: _sort,
                            isExpanded: true,
                            decoration: InputDecoration(labelText: 'Sort'),
                            items: const [
                              DropdownMenuItem(
                                  value: 'relevance',
                                  child: Text('Most relevant')),
                              DropdownMenuItem(
                                  value: 'newest', child: Text('Newest')),
                              DropdownMenuItem(
                                  value: 'oldest', child: Text('Oldest')),
                            ],
                            onChanged: (value) => _changeCriteria(
                                () => _sort = value ?? 'relevance'),
                          ),
                        ),
                        SizedBox(width: 12),
                        Expanded(
                          child: DropdownButtonFormField<bool?>(
                            key: ValueKey('search-pinned-$_pinned'),
                            initialValue: _pinned,
                            isExpanded: true,
                            decoration: InputDecoration(labelText: 'Pinned'),
                            items: const [
                              DropdownMenuItem(
                                  value: null, child: Text('Either')),
                              DropdownMenuItem(
                                  value: true, child: Text('Pinned')),
                              DropdownMenuItem(
                                  value: false, child: Text('Not pinned')),
                            ],
                            onChanged: (value) =>
                                _changeCriteria(() => _pinned = value),
                          ),
                        ),
                      ],
                    ),
                    SizedBox(height: 10),
                    DropdownButtonFormField<String?>(
                      key: ValueKey('search-author-type-$_authorType'),
                      initialValue: _authorType,
                      isExpanded: true,
                      decoration: InputDecoration(labelText: 'Author type'),
                      items: const <DropdownMenuItem<String?>>[
                        DropdownMenuItem<String?>(
                            value: null, child: Text('Anyone')),
                        DropdownMenuItem<String?>(
                            value: 'user', child: Text('People')),
                        DropdownMenuItem<String?>(
                            value: 'bot', child: Text('Bots')),
                        DropdownMenuItem<String?>(
                            value: 'webhook', child: Text('Webhooks')),
                      ],
                      onChanged: (value) =>
                          _changeCriteria(() => _authorType = value),
                    ),
                    SizedBox(height: 10),
                    Row(
                      children: [
                        Expanded(
                          child: _UserFilterField(
                            key: ValueKey('search-author'),
                            label: 'From',
                            user: _userFor(_author),
                            loading: _loadingUsers,
                            onTap: () => _pickUser(mentions: false),
                            onClear: _author == null
                                ? null
                                : () => _changeCriteria(() => _author = null),
                          ),
                        ),
                        SizedBox(width: 12),
                        Expanded(
                          child: _UserFilterField(
                            key: ValueKey('search-mention'),
                            label: 'Mentions',
                            user: _userFor(_mention),
                            loading: _loadingUsers,
                            onTap: () => _pickUser(mentions: true),
                            onClear: _mention == null
                                ? null
                                : () => _changeCriteria(() => _mention = null),
                          ),
                        ),
                      ],
                    ),
                    if (_userLoadError != null && _users.isEmpty)
                      Padding(
                        padding: EdgeInsets.only(top: 6),
                        child: Text(
                          _userLoadError!,
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                      ),
                    SizedBox(height: 10),
                    Row(
                      children: [
                        Expanded(
                          child: OutlinedButton.icon(
                            icon: Icon(Icons.date_range_rounded),
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
                              if (value != null && mounted) {
                                _changeCriteria(() => _after = value);
                              }
                            },
                          ),
                        ),
                        SizedBox(width: 12),
                        Expanded(
                          child: OutlinedButton.icon(
                            icon: Icon(Icons.event_rounded),
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
                              if (value != null && mounted) {
                                _changeCriteria(() => _before = value);
                              }
                            },
                          ),
                        ),
                      ],
                    ),
                    SizedBox(height: 10),
                    Wrap(
                      spacing: 8,
                      runSpacing: 4,
                      children: [
                        for (final kind in _contentKinds)
                          FilterChip(
                            label: Text(kind),
                            selected: _has.contains(kind),
                            onSelected: (selected) => _changeCriteria(() {
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
                        child: Text('Clear filters'),
                      ),
                    ),
                    FilledButton.icon(
                      onPressed: _loading || !_canSearch ? null : _search,
                      icon: Icon(Icons.search_rounded),
                      label: Text(_loading ? 'Searching…' : 'Search'),
                    ),
                    if (_history.isNotEmpty) ...[
                      SizedBox(height: 10),
                      Row(
                        children: [
                          Expanded(
                            child: Text('Recent searches',
                                style: TextStyle(fontWeight: FontWeight.w700)),
                          ),
                          TextButton(
                              onPressed: _clearHistory, child: Text('Clear')),
                        ],
                      ),
                      Wrap(
                        spacing: 8,
                        children: [
                          for (final item in _history)
                            ActionChip(
                              label: Text(item),
                              onPressed: () {
                                _query.text = item;
                                _query.selection = TextSelection.collapsed(
                                    offset: _query.text.length);
                                _queryChanged(item);
                                _search();
                              },
                            ),
                        ],
                      ),
                    ],
                    if (_error case final error?)
                      Padding(
                        padding: EdgeInsets.only(top: 8),
                        child: Text(error,
                            style: TextStyle(
                                color: Theme.of(context).colorScheme.error)),
                      ),
                    if (_page?.authorityCoverage
                        case 'unavailable' || 'unsupported')
                      Padding(
                        padding: EdgeInsets.only(top: 8),
                        child: Text(
                            'Showing locally cached matches. The home instance could not provide complete results.'),
                      ),
                    if (_page?.localCoverage == 'cached' &&
                        _page?.authorityCoverage == 'not_queried')
                      Padding(
                        padding: EdgeInsets.only(top: 8),
                        child: Text(
                            'Account-wide direct-message search uses this home’s recent federated cache. Search inside a conversation for complete results from its authority.'),
                      ),
                    if (_page?.indexing == true)
                      Padding(
                        padding: EdgeInsets.only(top: 8),
                        child: Text(
                            'Search is catching up with recent messages. Results may be incomplete for a moment.'),
                      ),
                    if (_page?.encryptedChannelRefs.isNotEmpty == true)
                      Padding(
                        padding: EdgeInsets.only(top: 8),
                        child: Text(
                            'Encrypted conversations were excluded from these results.'),
                      ),
                    if (_page != null && _page!.results.isEmpty)
                      Padding(
                        padding: EdgeInsets.all(24),
                        child: Text(
                          'No messages matched those filters.',
                          textAlign: TextAlign.center,
                        ),
                      ),
                    for (final result
                        in _page?.results ?? const <MessageSearchResult>[])
                      _resultTile(context, result),
                    if (_page?.nextCursor != null)
                      TextButton(
                        onPressed: _loading ? null : () => _search(more: true),
                        child: Text('Load more'),
                      ),
                  ],
                ),
    );
  }

  Widget _operatorSuggestions() {
    final match = _operatorMatch;
    if (match == null) {
      return Wrap(
        spacing: 8,
        runSpacing: 6,
        children: [
          ActionChip(
            avatar: Icon(Icons.person_search_rounded, size: 18),
            label: Text('From'),
            onPressed: () => _beginOperator(MessageSearchOperator.from),
          ),
          ActionChip(
            avatar: Icon(Icons.alternate_email_rounded, size: 18),
            label: Text('Mentions'),
            onPressed: () => _beginOperator(MessageSearchOperator.mentions),
          ),
          ActionChip(
            avatar: Icon(Icons.attach_file_rounded, size: 18),
            label: Text('Has'),
            onPressed: () => _beginOperator(MessageSearchOperator.has),
          ),
        ],
      );
    }
    if (match.operator == MessageSearchOperator.has) {
      final kinds = _matchingContentKinds(match.needle);
      return _SuggestionCard(
        children: [
          for (final kind in kinds)
            ListTile(
              dense: true,
              leading: Icon(Icons.attach_file_rounded),
              title: Text('Has $kind'),
              onTap: () => _selectContentKind(kind),
            ),
          if (kinds.isEmpty)
            ListTile(
              dense: true,
              title: Text('No matching attachment type'),
            ),
        ],
      );
    }
    final users = filterMessageSearchUsers(_users, match.needle).take(8);
    return _SuggestionCard(
      children: [
        for (final user in users)
          ListTile(
            dense: true,
            leading: Icon(Icons.person_outline_rounded),
            title: Text(user.name),
            subtitle: Text(user.handle),
            onTap: () => _selectOperatorUser(match.operator, user),
          ),
        if (_loadingUsers)
          Padding(
            padding: EdgeInsets.all(12),
            child: Center(child: CircularProgressIndicator()),
          )
        else if (users.isEmpty)
          ListTile(
            dense: true,
            title: Text('No matching members'),
          ),
      ],
    );
  }

  Widget _resultTile(BuildContext context, MessageSearchResult result) {
    final recipients =
        result.channel.recipients.map((user) => user.name).join(', ');
    final contextLabel = result.guild == null
        ? (recipients.isEmpty ? 'Direct message' : recipients)
        : '${result.guild!.name} · #${result.channel.name ?? 'channel'}';
    final author = result.message.author;
    final localTime = result.message.createdAt.toLocal();
    return Padding(
      padding: EdgeInsets.only(bottom: 8),
      child: Material(
        color: context.kaede.panel,
        borderRadius: BorderRadius.circular(KaedeRadius.medium),
        child: InkWell(
          onTap: _jumping ? null : () => _jumpToResult(result),
          borderRadius: BorderRadius.circular(KaedeRadius.medium),
          child: Container(
            padding: EdgeInsets.fromLTRB(12, 11, 12, 12),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(KaedeRadius.medium),
              border: Border.all(color: context.kaede.border),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Text(
                      contextLabel,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: context.kaede.muted,
                        fontSize: 11.5,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ],
                ),
                SizedBox(height: 8),
                Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    if (author != null) ...[
                      UserAvatar(
                        user: author,
                        radius: 15,
                        ringColor: context.kaede.panel,
                      ),
                      SizedBox(width: 10),
                    ],
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Row(
                            crossAxisAlignment: CrossAxisAlignment.baseline,
                            textBaseline: TextBaseline.alphabetic,
                            children: [
                              Flexible(
                                child: Text(
                                  author?.name ?? 'Unknown sender',
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: TextStyle(
                                    fontWeight: FontWeight.w700,
                                    fontSize: 14,
                                  ),
                                ),
                              ),
                              SizedBox(width: 8),
                              Text(
                                '${MaterialLocalizations.of(context).formatShortDate(localTime)} '
                                '${MaterialLocalizations.of(context).formatTimeOfDay(TimeOfDay.fromDateTime(localTime))}',
                                style: TextStyle(
                                  color: context.kaede.muted,
                                  fontSize: 11,
                                ),
                              ),
                            ],
                          ),
                          SizedBox(height: 3),
                          Text(
                            messageSearchSafeSnippet(result.snippet),
                            maxLines: 3,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              fontSize: 13.5,
                              height: 1.35,
                              color: context.kaede.textSoft,
                            ),
                          ),
                        ],
                      ),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Future<void> _jumpToResult(MessageSearchResult result) async {
    if (_jumping) return;
    setState(() {
      _jumping = true;
      _error = null;
    });
    try {
      await widget.onJump(result);
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(
              error,
              summary: 'Could not open that message.',
            ));
      }
    } finally {
      if (mounted) setState(() => _jumping = false);
    }
  }
}

final class _SuggestionCard extends StatelessWidget {
  const _SuggestionCard({required this.children});

  final List<Widget> children;

  @override
  Widget build(BuildContext context) => Material(
        elevation: 3,
        clipBehavior: Clip.antiAlias,
        borderRadius: BorderRadius.circular(14),
        color: Theme.of(context).colorScheme.surfaceContainerHigh,
        child: ConstrainedBox(
          constraints: BoxConstraints(maxHeight: 260),
          child: SingleChildScrollView(child: Column(children: children)),
        ),
      );
}

final class _UserFilterField extends StatelessWidget {
  const _UserFilterField({
    required this.label,
    required this.user,
    required this.loading,
    required this.onTap,
    required this.onClear,
    super.key,
  });

  final String label;
  final KaedeUser? user;
  final bool loading;
  final VoidCallback onTap;
  final VoidCallback? onClear;

  @override
  Widget build(BuildContext context) => InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(12),
        child: InputDecorator(
          decoration: InputDecoration(
            labelText: label,
            suffixIcon: onClear == null
                ? loading
                    ? Padding(
                        padding: EdgeInsets.all(14),
                        child: SizedBox.square(
                          dimension: 18,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        ),
                      )
                    : Icon(Icons.arrow_drop_down_rounded)
                : IconButton(
                    tooltip: 'Clear $label filter',
                    onPressed: onClear,
                    icon: Icon(Icons.close_rounded),
                  ),
          ),
          child: Text(
            user?.name ?? 'Anyone',
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
          ),
        ),
      );
}

final class _MessageSearchUserPicker extends StatefulWidget {
  const _MessageSearchUserPicker({
    required this.title,
    required this.users,
    required this.remoteSearch,
  });

  final String title;
  final List<KaedeUser> users;
  final Future<List<KaedeUser>> Function(String query)? remoteSearch;

  @override
  State<_MessageSearchUserPicker> createState() =>
      _MessageSearchUserPickerState();
}

final class _MessageSearchUserPickerState
    extends State<_MessageSearchUserPicker> {
  final _query = TextEditingController();
  Timer? _debounce;
  late List<KaedeUser> _users;
  var _loading = false;
  var _generation = 0;
  String? _error;

  @override
  void initState() {
    super.initState();
    _users = messageSearchUserCandidates(widget.users);
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _query.dispose();
    super.dispose();
  }

  void _changed(String value) {
    setState(() {});
    final search = widget.remoteSearch;
    if (search == null) return;
    _debounce?.cancel();
    _debounce = Timer(Duration(milliseconds: 250), () async {
      final generation = ++_generation;
      setState(() {
        _loading = true;
        _error = null;
      });
      try {
        final users = await search(value);
        if (!mounted || generation != _generation) return;
        setState(() {
          _users = messageSearchUserCandidates(<KaedeUser?>[
            ..._users,
            ...users,
          ]);
        });
      } on Object catch (error) {
        if (!mounted || generation != _generation) return;
        setState(() => _error = userFacingError(
              error,
              summary: 'Could not search the member list.',
            ));
      } finally {
        if (mounted && generation == _generation) {
          setState(() => _loading = false);
        }
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final visible = filterMessageSearchUsers(_users, _query.text);
    return SafeArea(
      child: Padding(
        padding: EdgeInsets.fromLTRB(
          16,
          0,
          16,
          MediaQuery.viewInsetsOf(context).bottom + 16,
        ),
        child: FractionallySizedBox(
          heightFactor: .72,
          child: ListView(
            key: ValueKey('message-search-member-picker'),
            keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
            children: [
              Text(widget.title, style: Theme.of(context).textTheme.titleLarge),
              SizedBox(height: 12),
              SearchBar(
                key: ValueKey('message-search-member-query'),
                controller: _query,
                autoFocus: true,
                hintText: 'Search members',
                leading: Icon(Icons.search_rounded),
                onChanged: _changed,
              ),
              SizedBox(height: 8),
              if (_error case final error?)
                Text(error,
                    style:
                        TextStyle(color: Theme.of(context).colorScheme.error)),
              if (visible.isEmpty && _loading)
                Padding(
                  padding: EdgeInsets.all(24),
                  child: Center(child: CircularProgressIndicator()),
                )
              else if (visible.isEmpty)
                Padding(
                  padding: EdgeInsets.all(24),
                  child: Center(child: Text('No matching members.')),
                )
              else
                for (final user in visible)
                  ListTile(
                    leading: Icon(Icons.person_outline_rounded),
                    title: Text(user.name),
                    subtitle: Text(user.handle),
                    onTap: () => Navigator.pop(context, user),
                  ),
              if (visible.isNotEmpty && _loading)
                Padding(
                  padding: EdgeInsets.all(12),
                  child: Center(child: CircularProgressIndicator()),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
