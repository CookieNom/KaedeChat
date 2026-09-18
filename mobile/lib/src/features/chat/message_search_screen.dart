import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/shared/action_feedback.dart';
import 'package:kaede_mobile/src/features/shared/remote_media.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';
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
    this.historyAvailable = true,
    this.channels = const <KaedeChannel>[],
    this.users = const <KaedeUser>[],
    required this.onJump,
    super.key,
  });

  final KaedeRepository repository;
  final String scope;
  final EntityRef? scopeRef;
  final KaedeChannel? channel;
  final List<KaedeChannel> channels;
  final EntityRef? accountRef;
  final bool historyAvailable;
  final List<KaedeUser> users;
  final Future<void> Function(MessageSearchResult result) onJump;

  @override
  State<MessageSearchScreen> createState() => _MessageSearchScreenState();
}

final class _MessageSearchScreenState extends State<MessageSearchScreen> {
  final _query = TextEditingController();
  final _queryFocus = FocusNode();
  var _sort = 'relevance';
  EntityRef? _selectedChannelRef;

  List<KaedeChannel> get _channelOptions => {
        for (final channel in widget.channels) channel.ref: channel,
        if (widget.channel case final channel?) channel.ref: channel,
      }.values.toList();

  KaedeChannel? get _searchChannel => widget.scope == 'guild'
      ? _channelOptions
          .where((item) => item.ref == _selectedChannelRef)
          .firstOrNull
      : widget.channel;

  bool get _channelFiltered =>
      widget.scope == 'guild' && _selectedChannelRef != null;
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
      (_channelFiltered || widget.scope == 'channel') &&
      (_searchChannel?.encryptionMode == 'e2ee' ||
          _searchChannel?.searchAvailable == false);

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
        scope: _channelFiltered ? 'channel' : widget.scope,
        scopeRef: _channelFiltered ? _selectedChannelRef : widget.scopeRef,
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
    _selectedChannelRef = widget.channel?.ref;
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
    if (oldWidget.historyAvailable && !widget.historyAvailable) {
      _searchRequestGeneration += 1;
      _page = null;
      _pageSignature = null;
      _loading = false;
      _error = null;
    }
    if (!identical(oldWidget.repository, widget.repository) ||
        oldWidget.scope != widget.scope ||
        oldWidget.scopeRef != widget.scopeRef ||
        oldWidget.channel?.ref != widget.channel?.ref) {
      _selectedChannelRef = widget.channel?.ref;
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
          summary: L10n.of(context).ui_could_not_load_the_member_list_04caf38e,
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
      _selectedChannelRef = null;
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
        title: mentions
            ? L10n.of(context).ui_mentioned_member_95ec6306
            : L10n.of(context).ui_message_author_9f3bab67,
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
    if (!widget.historyAvailable ||
        _loading ||
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
          !widget.historyAvailable ||
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
            summary: L10n.of(context).ui_could_not_search_messages_590ccc07,
          ));
    } finally {
      if (mounted && generation == _searchRequestGeneration) {
        setState(() => _loading = false);
      }
    }
  }

  Future<void> _pickChannel() async {
    var query = '';
    final channels = _channelOptions;
    final selected = await showModalBottomSheet<String>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (context) => StatefulBuilder(builder: (context, update) {
        final matches = channels
            .where((channel) => (channel.name ?? '')
                .toLowerCase()
                .contains(query.trim().toLowerCase()))
            .toList();
        return SafeArea(
          child: Padding(
            padding: EdgeInsets.fromLTRB(
                16, 0, 16, MediaQuery.viewInsetsOf(context).bottom + 16),
            child: SizedBox(
              height: (MediaQuery.sizeOf(context).height -
                      MediaQuery.viewInsetsOf(context).bottom) *
                  .55,
              child: Column(children: [
                SearchBar(
                  key: ValueKey('search-channel-query'),
                  hintText: L10n.of(context).ui_search_channels_79842fc3,
                  leading: Icon(Icons.search_rounded),
                  onChanged: (value) => update(() => query = value),
                ),
                ListTile(
                  title: Text(L10n.of(context).ui_all_channels_ada7bc14),
                  leading: Icon(Icons.tag_rounded),
                  trailing: _selectedChannelRef == null
                      ? Icon(Icons.check_rounded)
                      : null,
                  onTap: () => Navigator.pop(context, ''),
                ),
                Divider(height: 1),
                Expanded(
                    child: matches.isEmpty
                        ? Center(
                            child: Text(L10n.of(context)
                                .ui_no_matching_channels_f908cc3b))
                        : ListView.builder(
                            itemCount: matches.length,
                            itemBuilder: (context, index) {
                              final channel = matches[index];
                              return ListTile(
                                title: Text(
                                    L10n.of(context).ui_value0_ea2f080f(
                                        (channel.name).toString()),
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis),
                                trailing: channel.ref == _selectedChannelRef
                                    ? Icon(Icons.check_rounded)
                                    : null,
                                onTap: () =>
                                    Navigator.pop(context, channel.ref.wire),
                              );
                            },
                          )),
              ]),
            ),
          ),
        );
      }),
    );
    if (!mounted || selected == null) return;
    _changeCriteria(() => _selectedChannelRef =
        selected.isEmpty ? null : EntityRef.parse(selected));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(L10n.of(context).ui_search_messages_7bf8a493),
        bottom: widget.scope == 'guild'
            ? PreferredSize(
                preferredSize: Size.fromHeight(80),
                child: Padding(
                  padding: EdgeInsets.fromLTRB(16, 0, 16, 12),
                  child: ListTile(
                    key: ValueKey('search-channel-picker'),
                    contentPadding: EdgeInsets.zero,
                    leading: Icon(Icons.tag_rounded),
                    title: Text(_searchChannel?.name ?? 'All channels',
                        maxLines: 1, overflow: TextOverflow.ellipsis),
                    trailing: Icon(Icons.expand_more_rounded),
                    onTap: _pickChannel,
                  ),
                ),
              )
            : null,
      ),
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
                        L10n.of(context)
                            .ui_search_is_unavailable_for_this_encrypted_conv_e89fb8a5,
                        textAlign: TextAlign.center,
                        style: TextStyle(
                            fontSize: 18, fontWeight: FontWeight.w800)),
                    SizedBox(height: 8),
                    Text(
                      L10n.of(context)
                          .ui_end_to_end_encrypted_message_bodies_never_lea_6f36cb3d,
                      textAlign: TextAlign.center,
                    ),
                  ],
                ),
              ),
            )
          : !widget.historyAvailable
              ? Center(
                  child: Padding(
                    padding: EdgeInsets.all(28),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Icon(Icons.history_toggle_off_rounded, size: 44),
                        SizedBox(height: 16),
                        Text(
                            L10n.of(context)
                                .ui_message_history_is_unavailable_in_this_channe_088d854e,
                            textAlign: TextAlign.center,
                            style: TextStyle(
                                fontSize: 18, fontWeight: FontWeight.w800)),
                        SizedBox(height: 8),
                        Text(
                          L10n.of(context)
                              .ui_new_messages_can_still_appear_live_but_retain_9d23966a,
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
                              L10n.of(context)
                                  .ui_message_search_is_disabled_on_this_instance_c04c06a8,
                              textAlign: TextAlign.center,
                              style: TextStyle(
                                  fontSize: 18, fontWeight: FontWeight.w800),
                            ),
                            SizedBox(height: 8),
                            Text(
                              L10n.of(context)
                                  .ui_your_instance_administrator_can_enable_the_pr_9de0470e,
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
                          hintText: L10n.of(context)
                              .ui_search_messages_or_type_from_2fbeaa36,
                          leading: Icon(Icons.search_rounded),
                          trailing: _query.text.isEmpty
                              ? null
                              : <Widget>[
                                  ActionButton(
                                    kind: ActionButtonKind.icon,
                                    tooltip: L10n.of(context)
                                        .ui_clear_search_text_43960e99,
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
                                decoration: InputDecoration(
                                    labelText:
                                        L10n.of(context).ui_sort_8459a7f1),
                                items: [
                                  DropdownMenuItem(
                                      value: 'relevance',
                                      child: Text(L10n.of(context)
                                          .ui_most_relevant_2b67deb3)),
                                  DropdownMenuItem(
                                      value: 'newest',
                                      child: Text(
                                          L10n.of(context).ui_newest_324b6d0b)),
                                  DropdownMenuItem(
                                      value: 'oldest',
                                      child: Text(
                                          L10n.of(context).ui_oldest_4aa405ee)),
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
                                decoration: InputDecoration(
                                    labelText:
                                        L10n.of(context).ui_pinned_6d0ba1af),
                                items: [
                                  DropdownMenuItem(
                                      value: null,
                                      child: Text(
                                          L10n.of(context).ui_either_8b582b34)),
                                  DropdownMenuItem(
                                      value: true,
                                      child: Text(
                                          L10n.of(context).ui_pinned_6d0ba1af)),
                                  DropdownMenuItem(
                                      value: false,
                                      child: Text(L10n.of(context)
                                          .ui_not_pinned_ee399178)),
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
                          decoration: InputDecoration(
                              labelText:
                                  L10n.of(context).ui_author_type_50fb73b2),
                          items: <DropdownMenuItem<String?>>[
                            DropdownMenuItem<String?>(
                                value: null,
                                child:
                                    Text(L10n.of(context).ui_anyone_6202c187)),
                            DropdownMenuItem<String?>(
                                value: 'user',
                                child:
                                    Text(L10n.of(context).ui_people_8390060e)),
                            DropdownMenuItem<String?>(
                                value: 'bot',
                                child: Text(L10n.of(context).ui_bots_48630a97)),
                            DropdownMenuItem<String?>(
                                value: 'webhook',
                                child: Text(
                                    L10n.of(context).ui_webhooks_b7853def)),
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
                                label: L10n.of(context).ui_from_18743595,
                                user: _userFor(_author),
                                loading: _loadingUsers,
                                onTap: () => _pickUser(mentions: false),
                                onClear: _author == null
                                    ? null
                                    : () =>
                                        _changeCriteria(() => _author = null),
                              ),
                            ),
                            SizedBox(width: 12),
                            Expanded(
                              child: _UserFilterField(
                                key: ValueKey('search-mention'),
                                label: L10n.of(context).ui_mentions_43e4fe52,
                                user: _userFor(_mention),
                                loading: _loadingUsers,
                                onTap: () => _pickUser(mentions: true),
                                onClear: _mention == null
                                    ? null
                                    : () =>
                                        _changeCriteria(() => _mention = null),
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
                              child: ActionButton(
                                kind: ActionButtonKind.outlined,
                                icon: Icon(Icons.date_range_rounded),
                                label: Text(_after == null
                                    ? L10n.of(context).ui_after_date_692015bf
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
                              child: ActionButton(
                                kind: ActionButtonKind.outlined,
                                icon: Icon(Icons.event_rounded),
                                label: Text(_before == null
                                    ? L10n.of(context).ui_before_date_45d5ab9e
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
                          child: ActionButton(
                            kind: ActionButtonKind.text,
                            onPressed: _clearFilters,
                            child: Text(
                                L10n.of(context).ui_clear_filters_240d53c9),
                          ),
                        ),
                        ActionButton(
                          onPressed: _loading || !_canSearch ? null : _search,
                          icon: Icon(Icons.search_rounded),
                          label: Text(_loading
                              ? L10n.of(context).ui_searching_c9153c41
                              : L10n.of(context).ui_search_c646a2c9),
                        ),
                        if (_history.isNotEmpty) ...[
                          SizedBox(height: 10),
                          Row(
                            children: [
                              Expanded(
                                child: Text(
                                    L10n.of(context)
                                        .ui_recent_searches_e8d581b2,
                                    style:
                                        TextStyle(fontWeight: FontWeight.w700)),
                              ),
                              ActionButton(
                                  kind: ActionButtonKind.text,
                                  onPressed: _clearHistory,
                                  child:
                                      Text(L10n.of(context).ui_clear_04a57fc2)),
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
                                    color:
                                        Theme.of(context).colorScheme.error)),
                          ),
                        if (_page?.authorityCoverage
                            case 'unavailable' || 'unsupported')
                          Padding(
                            padding: EdgeInsets.only(top: 8),
                            child: Text(L10n.of(context)
                                .ui_showing_locally_cached_matches_the_home_insta_40d98082),
                          ),
                        if (_page?.localCoverage == 'cached' &&
                            _page?.authorityCoverage == 'not_queried')
                          Padding(
                            padding: EdgeInsets.only(top: 8),
                            child: Text(L10n.of(context)
                                .ui_account_wide_direct_message_search_uses_this__9aca4dba),
                          ),
                        if (_page?.indexing == true)
                          Padding(
                            padding: EdgeInsets.only(top: 8),
                            child: Text(L10n.of(context)
                                .ui_search_is_catching_up_with_recent_messages_re_67c656fa),
                          ),
                        if (_page?.encryptedChannelRefs.isNotEmpty == true)
                          Padding(
                            padding: EdgeInsets.only(top: 8),
                            child: Text(L10n.of(context)
                                .ui_encrypted_conversations_were_excluded_from_th_c0f982fb),
                          ),
                        if (_page != null && _page!.results.isEmpty)
                          Padding(
                            padding: EdgeInsets.all(24),
                            child: Text(
                              L10n.of(context)
                                  .ui_no_messages_matched_those_filters_adfa3d9e,
                              textAlign: TextAlign.center,
                            ),
                          ),
                        for (final result
                            in _page?.results ?? const <MessageSearchResult>[])
                          _resultTile(context, result),
                        if (_page?.nextCursor != null)
                          ActionButton(
                            kind: ActionButtonKind.text,
                            onPressed:
                                _loading ? null : () => _search(more: true),
                            child: Text(L10n.of(context).ui_load_more_2b7b053e),
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
            label: Text(L10n.of(context).ui_from_18743595),
            onPressed: () => _beginOperator(MessageSearchOperator.from),
          ),
          ActionChip(
            avatar: Icon(Icons.alternate_email_rounded, size: 18),
            label: Text(L10n.of(context).ui_mentions_43e4fe52),
            onPressed: () => _beginOperator(MessageSearchOperator.mentions),
          ),
          ActionChip(
            avatar: Icon(Icons.attach_file_rounded, size: 18),
            label: Text(L10n.of(context).ui_has_5049a783),
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
              title: Text(
                  L10n.of(context).ui_has_value0_f2bffd98((kind).toString())),
              onTap: () => _selectContentKind(kind),
            ),
          if (kinds.isEmpty)
            ListTile(
              dense: true,
              title: Text(
                  L10n.of(context).ui_no_matching_attachment_type_b063e12a),
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
            title: Text(L10n.of(context).ui_no_matching_members_d9decbf6),
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
                                L10n.of(context).ui_value0_value1_c1a3651c(
                                    (MaterialLocalizations.of(context)
                                            .formatShortDate(localTime))
                                        .toString(),
                                    (MaterialLocalizations.of(context)
                                            .formatTimeOfDay(
                                                TimeOfDay.fromDateTime(
                                                    localTime)))
                                        .toString()),
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
              summary: L10n.of(context).ui_could_not_open_that_message_da9ab4c9,
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
                : ActionButton(
                    kind: ActionButtonKind.icon,
                    tooltip: L10n.of(context)
                        .ui_clear_value0_filter_99ce9e53((label).toString()),
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
              summary:
                  L10n.of(context).ui_could_not_search_the_member_list_cef2694e,
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
                hintText: L10n.of(context).ui_search_members_d6e1fdce,
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
                  child: Center(
                      child: Text(
                          L10n.of(context).ui_no_matching_members_d1bae508)),
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
