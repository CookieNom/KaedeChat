import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/thread_permissions.dart';
import 'package:kaede_mobile/src/e2ee/client.dart';
import 'package:kaede_mobile/src/e2ee/media.dart';
import 'package:kaede_mobile/src/features/chat/composer_pickers.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:uuid/uuid.dart';

enum ForumViewMode { list, gallery }

@visibleForTesting
bool canSubmitForumPost({
  required String title,
  required String content,
  required int attachmentCount,
  required bool requiresTag,
  required int selectedTagCount,
}) {
  final trimmedTitle = title.trim();
  final trimmedContent = content.trim();
  return trimmedTitle.isNotEmpty &&
      trimmedTitle.length <= 100 &&
      trimmedContent.length <= 2000 &&
      (trimmedContent.isNotEmpty || attachmentCount > 0) &&
      (!requiresTag || selectedTagCount > 0);
}

@visibleForTesting
List<KaedeChannel> mergeForumPostPages(
  Iterable<KaedeChannel> current,
  Iterable<KaedeChannel> next,
) {
  final seen = <EntityRef>{};
  return List.unmodifiable(<KaedeChannel>[
    for (final post in <KaedeChannel>[...current, ...next])
      if (seen.add(post.ref)) post,
  ]);
}

@visibleForTesting
String forumThreadFeedRevision(
  Iterable<KaedeChannel> threads,
  EntityRef forum,
) {
  final children = threads
      .where((thread) => thread.parentRef == forum)
      .toList(growable: false)
    ..sort((left, right) => left.ref.wire.compareTo(right.ref.wire));
  return children
      .map((thread) => [
            thread.ref.wire,
            thread.version ?? '',
            thread.messageCount,
            thread.flags,
            thread.archived,
            thread.locked,
            thread.name ?? '',
            thread.starterMessage?.content ?? '',
            thread.starterMessage?.attachments.length ?? 0,
            thread.starterMessage?.editedAt?.toIso8601String() ?? '',
            thread.starterMessage?.deletedAt?.toIso8601String() ?? '',
            thread.starterMessage?.pinned ?? false,
            thread.starterMessage?.reactionCounts.toString() ?? '',
          ].join(':'))
      .join('|');
}

final class ForumChannelView extends ConsumerStatefulWidget {
  const ForumChannelView({
    required this.channel,
    required this.onOpenThread,
    super.key,
  });

  final KaedeChannel channel;
  final ValueChanged<KaedeChannel> onOpenThread;

  @override
  ConsumerState<ForumChannelView> createState() => _ForumChannelViewState();
}

final class _ForumChannelViewState extends ConsumerState<ForumChannelView> {
  final _search = TextEditingController();
  final _scroll = ScrollController();
  final _selectedTags = <String>{};
  Timer? _searchDebounce;
  Timer? _liveReloadDebounce;
  List<KaedeChannel> _posts = const [];
  var _hasMore = false;
  String? _cursor;
  var _sortOrder = 0;
  var _viewMode = ForumViewMode.list;
  var _loading = true;
  var _loadingMore = false;
  var _requestGeneration = 0;
  String? _error;

  String get _preferenceKey => 'forum-view:${widget.channel.ref.wire}';

  @override
  void initState() {
    super.initState();
    _sortOrder = widget.channel.defaultSortOrder ?? 0;
    _viewMode = widget.channel.defaultForumLayout == 2
        ? ForumViewMode.gallery
        : ForumViewMode.list;
    _search.addListener(_searchChanged);
    _scroll.addListener(_scrollChanged);
    unawaited(_restorePreferencesAndLoad());
  }

  @override
  void didUpdateWidget(covariant ForumChannelView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.channel.ref != widget.channel.ref) {
      _selectedTags.clear();
      _search.clear();
      _sortOrder = widget.channel.defaultSortOrder ?? 0;
      _viewMode = widget.channel.defaultForumLayout == 2
          ? ForumViewMode.gallery
          : ForumViewMode.list;
      unawaited(_restorePreferencesAndLoad());
    }
  }

  @override
  void dispose() {
    _searchDebounce?.cancel();
    _liveReloadDebounce?.cancel();
    _scroll
      ..removeListener(_scrollChanged)
      ..dispose();
    _search
      ..removeListener(_searchChanged)
      ..dispose();
    super.dispose();
  }

  Future<void> _restorePreferencesAndLoad() async {
    final preferences = await SharedPreferences.getInstance();
    final saved = preferences.getStringList(_preferenceKey);
    if (saved != null && saved.length == 2 && mounted) {
      setState(() {
        _sortOrder = int.tryParse(saved[0]) ?? _sortOrder;
        _viewMode = saved[1] == ForumViewMode.gallery.name
            ? ForumViewMode.gallery
            : ForumViewMode.list;
      });
    }
    await _load();
  }

  void _searchChanged() {
    _searchDebounce?.cancel();
    _searchDebounce = Timer(Duration(milliseconds: 300), _load);
  }

  void _scrollChanged() {
    if (!_scroll.hasClients ||
        _scroll.position.extentAfter > 280 ||
        _loading ||
        _loadingMore) {
      return;
    }
    unawaited(_loadMore());
  }

  Future<void> _savePreferences() async {
    final preferences = await SharedPreferences.getInstance();
    await preferences.setStringList(
      _preferenceKey,
      <String>['$_sortOrder', _viewMode.name],
    );
  }

  void _scheduleLiveReload() {
    if (_loading) return;
    _liveReloadDebounce?.cancel();
    _liveReloadDebounce = Timer(
      Duration(milliseconds: 180),
      () => unawaited(_load(silent: true)),
    );
  }

  Future<void> _load({bool silent = false}) async {
    final generation = ++_requestGeneration;
    if (mounted && !silent) {
      setState(() {
        _loading = true;
        _error = null;
      });
    }
    try {
      final controller = ref.read(mobileControllerProvider.notifier);
      final page = await controller.loadThreads(
        widget.channel.ref,
        includeArchived: true,
        sortOrder: _sortOrder,
        tagIds: _selectedTags.toList(growable: false),
        query: _search.text,
      );
      if (!mounted || generation != _requestGeneration) return;
      setState(() {
        _posts = page.threads;
        _hasMore = page.hasMore;
        _cursor = page.nextCursor;
        _loading = false;
      });
    } on Object catch (error) {
      if (!mounted || generation != _requestGeneration) return;
      if (!silent) {
        setState(() {
          _loading = false;
          _error = userFacingError(error, summary: 'Could not load posts');
        });
      }
    }
  }

  Future<void> _loadMore() async {
    if (_loadingMore || !_hasMore || _cursor == null) return;
    final generation = _requestGeneration;
    setState(() => _loadingMore = true);
    try {
      final controller = ref.read(mobileControllerProvider.notifier);
      final page = await controller.loadThreads(
        widget.channel.ref,
        includeArchived: true,
        cursor: _cursor,
        sortOrder: _sortOrder,
        tagIds: _selectedTags.toList(growable: false),
        query: _search.text,
      );
      if (!mounted || generation != _requestGeneration) return;
      setState(() {
        _posts = mergeForumPostPages(_posts, page.threads);
        _hasMore = page.hasMore && page.threads.isNotEmpty;
        _cursor = page.nextCursor;
      });
    } on Object catch (error) {
      if (mounted && generation == _requestGeneration) {
        setState(() => _error = userFacingError(
              error,
              summary: 'Could not load more posts',
            ));
      }
    } finally {
      if (mounted && generation == _requestGeneration) {
        setState(() => _loadingMore = false);
      }
    }
  }

  Future<void> _showSortAndView() async {
    var sort = _sortOrder;
    var view = _viewMode;
    final result = await showModalBottomSheet<(int, ForumViewMode)>(
      context: context,
      showDragHandle: true,
      builder: (sheetContext) => StatefulBuilder(
        builder: (context, setSheetState) => SafeArea(
          child: SingleChildScrollView(
            padding: EdgeInsets.fromLTRB(20, 0, 20, 20),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text('Sort By',
                    style: TextStyle(
                      color: context.kaede.muted,
                      fontSize: 16,
                      fontWeight: FontWeight.w700,
                    )),
                RadioGroup<int>(
                  groupValue: sort,
                  onChanged: (value) =>
                      setSheetState(() => sort = value ?? sort),
                  child: Column(
                    children: [
                      RadioListTile<int>(
                        contentPadding: EdgeInsets.zero,
                        title: Text('Recently Active'),
                        value: 0,
                      ),
                      RadioListTile<int>(
                        contentPadding: EdgeInsets.zero,
                        title: Text('Date Posted'),
                        value: 1,
                      ),
                    ],
                  ),
                ),
                Divider(),
                SizedBox(height: 8),
                Text('View As',
                    style: TextStyle(
                      color: context.kaede.muted,
                      fontSize: 16,
                      fontWeight: FontWeight.w700,
                    )),
                RadioGroup<ForumViewMode>(
                  groupValue: view,
                  onChanged: (value) =>
                      setSheetState(() => view = value ?? view),
                  child: Column(
                    children: [
                      RadioListTile<ForumViewMode>(
                        contentPadding: EdgeInsets.zero,
                        title: Text('List'),
                        value: ForumViewMode.list,
                      ),
                      RadioListTile<ForumViewMode>(
                        contentPadding: EdgeInsets.zero,
                        title: Text('Gallery'),
                        value: ForumViewMode.gallery,
                      ),
                    ],
                  ),
                ),
                Divider(),
                TextButton(
                  style: TextButton.styleFrom(
                    alignment: Alignment.centerLeft,
                    foregroundColor: context.kaede.text,
                  ),
                  onPressed: () => Navigator.pop(
                    sheetContext,
                    (
                      widget.channel.defaultSortOrder ?? 0,
                      widget.channel.defaultForumLayout == 2
                          ? ForumViewMode.gallery
                          : ForumViewMode.list,
                    ),
                  ),
                  child: Text('Reset to default'),
                ),
                SizedBox(height: 4),
                FilledButton(
                  onPressed: () => Navigator.pop(sheetContext, (sort, view)),
                  child: Text('Done'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
    if (result == null || !mounted) return;
    setState(() {
      _sortOrder = result.$1;
      _viewMode = result.$2;
    });
    unawaited(_savePreferences());
    await _load();
  }

  Future<void> _newPost() async {
    final created = await showModalBottomSheet<KaedeChannel>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: context.kaede.canvas,
      builder: (_) => _NewForumPostSheet(channel: widget.channel),
    );
    if (created == null || !mounted) return;
    await _load();
    widget.onOpenThread(created);
  }

  void _toggleTag(String tag) {
    setState(() {
      if (!_selectedTags.remove(tag)) {
        if (_selectedTags.length == 5) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text('Select up to 5 tags.')),
          );
          return;
        }
        _selectedTags.add(tag);
      }
    });
    unawaited(_load());
  }

  @override
  Widget build(BuildContext context) {
    ref.listen<String>(
      mobileControllerProvider.select(
        (state) => forumThreadFeedRevision(state.threads, widget.channel.ref),
      ),
      (previous, next) {
        if (previous != next) _scheduleLiveReload();
      },
    );
    final canPost = canCreateForumPost(widget.channel);
    return Column(
      children: [
        Padding(
          padding: EdgeInsets.fromLTRB(12, 10, 12, 4),
          child: Row(
            children: [
              Expanded(
                child: TextField(
                  key: ValueKey('forum-search'),
                  controller: _search,
                  textInputAction: TextInputAction.search,
                  decoration: InputDecoration(
                    hintText: 'Search',
                    prefixIcon: Icon(Icons.search_rounded),
                  ),
                ),
              ),
              if (canPost) ...[
                SizedBox(width: 8),
                FilledButton.icon(
                  key: ValueKey('forum-new-post'),
                  onPressed: _newPost,
                  icon: Icon(Icons.chat_bubble_rounded, size: 17),
                  label: Text('New Post'),
                ),
              ],
            ],
          ),
        ),
        if (widget.channel.availableTags.isNotEmpty)
          SizedBox(
            height: 48,
            child: ListView.separated(
              scrollDirection: Axis.horizontal,
              padding: EdgeInsets.symmetric(horizontal: 12),
              itemCount: widget.channel.availableTags.length,
              separatorBuilder: (_, __) => SizedBox(width: 7),
              itemBuilder: (context, index) {
                final tag = widget.channel.availableTags[index];
                return FilterChip(
                  label: ForumTagLabel(
                    tag: tag,
                    originDomain: widget.channel.ref.domain,
                  ),
                  selected: _selectedTags.contains(tag.id),
                  onSelected: (_) => _toggleTag(tag.id),
                );
              },
            ),
          ),
        Align(
          alignment: Alignment.centerLeft,
          child: Padding(
            padding: EdgeInsets.fromLTRB(12, 4, 12, 8),
            child: OutlinedButton.icon(
              key: ValueKey('forum-sort-view'),
              onPressed: _showSortAndView,
              icon: Icon(Icons.swap_vert_rounded, size: 18),
              label: Text('Sort & View'),
            ),
          ),
        ),
        Divider(height: 1),
        if ((_loading && _posts.isNotEmpty) || _loadingMore)
          LinearProgressIndicator(minHeight: 2),
        Expanded(child: _buildPosts()),
      ],
    );
  }

  Widget _buildPosts() {
    if (_loading && _posts.isEmpty) {
      return Center(child: CircularProgressIndicator());
    }
    if (_error case final error? when _posts.isEmpty) {
      return Center(
        child: Padding(
          padding: EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(error, textAlign: TextAlign.center),
              SizedBox(height: 12),
              OutlinedButton(onPressed: _load, child: Text('Retry')),
            ],
          ),
        ),
      );
    }
    if (_posts.isEmpty) {
      return Center(
        child: Text(
          _search.text.trim().isNotEmpty || _selectedTags.isNotEmpty
              ? 'No posts match your search.'
              : 'No posts yet.',
          style: TextStyle(color: context.kaede.muted),
        ),
      );
    }
    if (_viewMode == ForumViewMode.gallery) {
      return LayoutBuilder(
        builder: (context, constraints) {
          final columns = constraints.maxWidth >= 700
              ? 3
              : constraints.maxWidth >= 430
                  ? 2
                  : 1;
          return GridView.builder(
            controller: _scroll,
            padding: EdgeInsets.all(12),
            gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
              crossAxisCount: columns,
              crossAxisSpacing: 10,
              mainAxisSpacing: 10,
              childAspectRatio: .9,
            ),
            itemCount: _posts.length,
            itemBuilder: (_, index) => _ForumPostCard(
              post: _posts[index],
              forum: widget.channel,
              gallery: true,
              onTap: () => widget.onOpenThread(_posts[index]),
            ),
          );
        },
      );
    }
    return RefreshIndicator(
      onRefresh: _load,
      child: ListView.separated(
        controller: _scroll,
        padding: EdgeInsets.all(12),
        itemCount: _posts.length,
        separatorBuilder: (_, __) => SizedBox(height: 10),
        itemBuilder: (_, index) => _ForumPostCard(
          post: _posts[index],
          forum: widget.channel,
          onTap: () => widget.onOpenThread(_posts[index]),
        ),
      ),
    );
  }
}

final class _ForumPostCard extends StatelessWidget {
  const _ForumPostCard({
    required this.post,
    required this.forum,
    required this.onTap,
    this.gallery = false,
  });

  final KaedeChannel post;
  final KaedeChannel forum;
  final VoidCallback onTap;
  final bool gallery;

  @override
  Widget build(BuildContext context) {
    final starter = post.starterMessage;
    final tags = <ForumTag>[
      for (final tag in forum.availableTags)
        if (post.appliedTagIds.contains(tag.id)) tag,
    ];
    final content = starter?.content?.trim() ?? '';
    final date = post.createdAt;
    return Material(
      color: context.kaede.panel,
      borderRadius: BorderRadius.circular(KaedeRadius.medium),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(KaedeRadius.medium),
        child: Container(
          padding: EdgeInsets.all(14),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(KaedeRadius.medium),
            border: Border.all(color: context.kaede.border),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: gallery ? MainAxisSize.max : MainAxisSize.min,
            children: [
              Row(
                children: [
                  if (post.pinned) ...[
                    Icon(Icons.push_pin_rounded,
                        size: 16, color: context.kaede.coralText),
                    SizedBox(width: 6),
                  ],
                  Expanded(
                    child: Text(
                      post.name ?? 'Untitled post',
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontSize: 16,
                        fontWeight: FontWeight.w800,
                      ),
                    ),
                  ),
                  if (post.encryptionMode == 'e2ee')
                    Icon(Icons.lock_rounded,
                        size: 14, color: context.kaede.muted),
                ],
              ),
              if (tags.isNotEmpty) ...[
                SizedBox(height: 8),
                Wrap(
                  spacing: 5,
                  runSpacing: 5,
                  children: [
                    for (final tag in tags)
                      Container(
                        padding:
                            EdgeInsets.symmetric(horizontal: 7, vertical: 3),
                        decoration: BoxDecoration(
                          color: context.kaede.raised,
                          borderRadius: BorderRadius.circular(20),
                        ),
                        child: ForumTagLabel(
                          tag: tag,
                          originDomain: forum.ref.domain,
                          fontSize: 11.5,
                          emojiSize: 14,
                        ),
                      ),
                  ],
                ),
              ],
              if (content.isNotEmpty) ...[
                SizedBox(height: 9),
                Text(
                  content,
                  maxLines: gallery ? 6 : 2,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(color: context.kaede.textSoft),
                ),
              ],
              if (gallery) Spacer(),
              SizedBox(height: 10),
              Row(
                children: [
                  Expanded(
                    child: Text(
                      starter?.author?.name ?? 'Unknown author',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: context.kaede.muted,
                        fontSize: 12,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                  Icon(Icons.chat_bubble_rounded,
                      size: 13, color: context.kaede.muted),
                  SizedBox(width: 4),
                  Text('${post.messageCount}',
                      style:
                          TextStyle(color: context.kaede.muted, fontSize: 12)),
                  if (date != null) ...[
                    SizedBox(width: 8),
                    Text(
                      DateFormat.MMMd().format(date.toLocal()),
                      style:
                          TextStyle(color: context.kaede.muted, fontSize: 12),
                    ),
                  ],
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

final class _NewForumPostSheet extends ConsumerStatefulWidget {
  const _NewForumPostSheet({required this.channel});

  final KaedeChannel channel;

  @override
  ConsumerState<_NewForumPostSheet> createState() => _NewForumPostSheetState();
}

final class _NewForumPostSheetState extends ConsumerState<_NewForumPostSheet> {
  final _title = TextEditingController();
  final _message = TextEditingController();
  final _selectedTags = <String>{};
  final _attachments = <_ForumAttachment>[];
  var _busy = false;
  String? _error;
  String? _encryptedDraftKey;
  EncryptedForumThreadReservation? _encryptedReservation;
  Map<String, Object?>? _encryptedClaimEnvelope;
  List<EntityRef> _encryptedClaimAttachments = const <EntityRef>[];
  List<EntityRef> _encryptedClaimMentions = const <EntityRef>[];

  bool get _requiresTag => widget.channel.flags & 16 != 0;

  @override
  void dispose() {
    _title.dispose();
    _message.dispose();
    super.dispose();
  }

  Future<void> _pickFiles() async {
    final result = await FilePicker.platform.pickFiles(
      allowMultiple: true,
      withData: false,
      withReadStream: false,
    );
    if (result == null || !mounted) return;
    final additions = <_ForumAttachment>[];
    for (final selected in result.files.take(10 - _attachments.length)) {
      if (selected.path case final path?) {
        additions.add(_ForumAttachment(
          name: selected.name,
          file: File(path),
          contentType: _contentType(selected.name),
        ));
      }
    }
    setState(() => _attachments.addAll(additions));
  }

  Future<void> _post() async {
    if (_busy) return;
    final title = _title.text.trim();
    final message = _message.text.trim();
    if (!canSubmitForumPost(
      title: title,
      content: message,
      attachmentCount: _attachments.length,
      requiresTag: _requiresTag,
      selectedTagCount: _selectedTags.length,
    )) {
      if (_requiresTag && _selectedTags.isEmpty) {
        setState(() => _error = 'Choose at least one tag.');
      }
      return;
    }
    final encryptedStarter = deferThreadStarterUntilE2eeActive(widget.channel);
    final sortedTags = _selectedTags.toList()..sort();
    final encryptedDraftKey = jsonEncode(<String, Object?>{
      'forum': widget.channel.ref.wire,
      'title': title,
      'message': message,
      'tags': sortedTags,
      'files': _attachments
          .map((item) => <String, Object?>{
                'path': item.file.path,
                'name': item.name,
                'content_type': item.contentType,
              })
          .toList(growable: false),
    });
    if (encryptedStarter &&
        _encryptedDraftKey != null &&
        _encryptedDraftKey != encryptedDraftKey) {
      setState(() {
        _error =
            'Finish retrying the pending encrypted post before changing its starter.';
      });
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final controller = ref.read(mobileControllerProvider.notifier);
      if (encryptedStarter) {
        _encryptedDraftKey ??= encryptedDraftKey;
        _encryptedReservation ??= await controller.reserveEncryptedForumThread(
          parent: widget.channel,
          name: title,
          clientNonce: const Uuid().v4(),
          appliedTagIds: sortedTags,
        );
        var thread = await controller.ensureRequiredThreadEncryption(
          _encryptedReservation!.channel,
        );
        _encryptedReservation = EncryptedForumThreadReservation(
          channel: thread,
          clientNonce: _encryptedReservation!.clientNonce,
          claimed: false,
        );
        if (channelEncryptionPaused(thread)) {
          throw StateError(
            'Encryption setup is required before the starter can be sent.',
          );
        }
        if (_encryptedClaimEnvelope == null) {
          final encryptedUploads = <EncryptedMobileUpload>[];
          for (final attachment in _attachments) {
            encryptedUploads.add(await uploadEncryptedFile(
              repository: controller.repository,
              channel: thread.ref,
              source: attachment.file,
              filename: attachment.name,
              contentType: attachment.contentType,
            ));
          }
          final allowedMentions = <String, Object?>{
            'parse': const <String>['everyone', 'roles', 'users'],
            'users': const <String>[],
            'roles': const <String>[],
            'replied_user': false,
          };
          final intent = mobileRichMessageMentionIntent(<String, Object?>{
            'content': message.isEmpty ? null : message,
            'components': const <Object?>[],
            'allowed_mentions': allowedMentions,
          });
          final state = ref.read(mobileControllerProvider);
          final guildRef = thread.guildRef;
          final guild = guildRef == null
              ? null
              : state.guilds.where((item) => item.ref == guildRef).firstOrNull;
          if (guild == null || guildRef == null) {
            throw StateError(
              'The current guild roster is unavailable for encrypted mentions.',
            );
          }
          _encryptedClaimMentions =
              expandedMobileEncryptedGuildMentionRecipients(
            userRefs: intent.userRefs,
            roleRefs: intent.roleRefs,
            everyone: intent.everyone,
            guild: guild,
            members: state.guildMembers[guildRef] ?? const <GuildMember>[],
            canMentionEveryone: thread.allows(Permission.administrator) ||
                thread.allows(Permission.mentionEveryone),
          );
          _encryptedClaimAttachments = encryptedUploads
              .map((item) => item.attachment)
              .toList(growable: false);
          _encryptedClaimEnvelope =
              await (await controller.e2eeClient()).encryptMessage(
            thread,
            message,
            attachments: encryptedUploads
                .map((item) => item.manifest)
                .toList(growable: false),
            mentionUserRefs: _encryptedClaimMentions,
            rich: MobileEncryptedRichMessageOptions(
              allowedMentions: allowedMentions,
            ),
          );
        }
        thread = await controller.claimEncryptedForumStarter(
          thread: thread,
          clientNonce: _encryptedReservation!.clientNonce,
          e2ee: _encryptedClaimEnvelope!,
          attachments: _encryptedClaimAttachments,
          mentionUsers: _encryptedClaimMentions,
        );
        if (mounted) Navigator.pop(context, thread);
        return;
      }
      final uploaded = <EntityRef>[];
      for (final attachment in _attachments) {
        uploaded.add(await controller.repository.uploadAttachmentFile(
          channel: widget.channel.ref,
          filename: attachment.name,
          contentType: attachment.contentType,
          file: attachment.file,
        ));
      }
      final created = await controller.createThread(
        parent: widget.channel,
        name: title,
        content: message,
        appliedTagIds: sortedTags,
        attachments: uploaded,
      );
      if (mounted) Navigator.pop(context, created);
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error =
            userFacingError(error, summary: 'Could not create the post'));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final canManage = canManageThreads(widget.channel);
    final guidelines = widget.channel.topic?.trim();
    final canAttach = widget.channel.allows(Permission.attachFiles);
    return PopScope(
      canPop: !_busy && _encryptedReservation == null,
      child: Padding(
        padding: EdgeInsets.fromLTRB(
          16,
          10,
          16,
          MediaQuery.viewInsetsOf(context).bottom + 16,
        ),
        child: SingleChildScrollView(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            mainAxisSize: MainAxisSize.min,
            children: [
              Row(
                children: [
                  IconButton(
                    tooltip: 'Close',
                    onPressed: _busy || _encryptedReservation != null
                        ? null
                        : () => Navigator.pop(context),
                    icon: Icon(Icons.close_rounded),
                  ),
                  Expanded(
                    child: Text('New Post',
                        style: TextStyle(
                            fontSize: 19, fontWeight: FontWeight.w800)),
                  ),
                  ValueListenableBuilder<TextEditingValue>(
                    valueListenable: _title,
                    builder: (context, _, __) =>
                        ValueListenableBuilder<TextEditingValue>(
                      valueListenable: _message,
                      builder: (context, _, __) => FilledButton(
                        onPressed: _busy ||
                                !canSubmitForumPost(
                                  title: _title.text,
                                  content: _message.text,
                                  attachmentCount: _attachments.length,
                                  requiresTag: _requiresTag,
                                  selectedTagCount: _selectedTags.length,
                                )
                            ? null
                            : _post,
                        child: _busy
                            ? SizedBox.square(
                                dimension: 16,
                                child:
                                    CircularProgressIndicator(strokeWidth: 2),
                              )
                            : Text('Post'),
                      ),
                    ),
                  ),
                ],
              ),
              if (guidelines?.isNotEmpty == true) ...[
                SizedBox(height: 12),
                Container(
                  padding: EdgeInsets.all(14),
                  decoration: BoxDecoration(
                    color: context.kaede.panel,
                    borderRadius: BorderRadius.circular(KaedeRadius.medium),
                    border: Border.all(color: context.kaede.border),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Icon(Icons.fact_check_outlined, size: 18),
                          SizedBox(width: 8),
                          Text('Post Guidelines',
                              style: TextStyle(fontWeight: FontWeight.w800)),
                        ],
                      ),
                      SizedBox(height: 8),
                      Text(guidelines!,
                          style: TextStyle(color: context.kaede.textSoft)),
                    ],
                  ),
                ),
              ],
              SizedBox(height: 14),
              TextField(
                key: ValueKey('forum-post-title'),
                controller: _title,
                autofocus: true,
                maxLength: 100,
                decoration: InputDecoration(
                  hintText: 'Title',
                  counterText: '',
                ),
              ),
              SizedBox(height: 10),
              TextField(
                key: ValueKey('forum-post-message'),
                controller: _message,
                minLines: 5,
                maxLines: 12,
                maxLength: 2000,
                decoration: InputDecoration(
                  hintText: 'Enter a message…',
                  counterText: '',
                ),
              ),
              if (widget.channel.availableTags.isNotEmpty) ...[
                SizedBox(height: 14),
                Text(_requiresTag ? 'Tags · Required' : 'Tags',
                    style: TextStyle(fontWeight: FontWeight.w700)),
                SizedBox(height: 8),
                Wrap(
                  spacing: 7,
                  runSpacing: 7,
                  children: [
                    for (final tag in widget.channel.availableTags)
                      FilterChip(
                        label: ForumTagLabel(
                          tag: tag,
                          originDomain: widget.channel.ref.domain,
                        ),
                        selected: _selectedTags.contains(tag.id),
                        onSelected: tag.moderated && !canManage
                            ? null
                            : (_) {
                                setState(() {
                                  if (!_selectedTags.remove(tag.id) &&
                                      _selectedTags.length < 5) {
                                    _selectedTags.add(tag.id);
                                  }
                                });
                              },
                      ),
                  ],
                ),
              ],
              if (_attachments.isNotEmpty) ...[
                SizedBox(height: 14),
                for (final attachment in _attachments)
                  ListTile(
                    dense: true,
                    contentPadding: EdgeInsets.zero,
                    leading: Icon(Icons.insert_drive_file_outlined),
                    title: Text(attachment.name),
                    trailing: IconButton(
                      tooltip: 'Remove attachment',
                      onPressed: _busy
                          ? null
                          : () =>
                              setState(() => _attachments.remove(attachment)),
                      icon: Icon(Icons.close_rounded),
                    ),
                  ),
              ],
              if (canAttach) ...[
                SizedBox(height: 10),
                OutlinedButton.icon(
                  onPressed:
                      _busy || _attachments.length >= 10 ? null : _pickFiles,
                  icon: Icon(Icons.add_photo_alternate_outlined),
                  label: Text('Add files'),
                ),
              ],
              if (_error case final error?) ...[
                SizedBox(height: 12),
                Text(error, style: TextStyle(color: context.kaede.danger)),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

final class _ForumAttachment {
  const _ForumAttachment({
    required this.name,
    required this.file,
    required this.contentType,
  });

  final String name;
  final File file;
  final String contentType;
}

String _contentType(String filename) {
  final extension = filename.toLowerCase().split('.').lastOrNull;
  return switch (extension) {
    'png' => 'image/png',
    'jpg' || 'jpeg' => 'image/jpeg',
    'gif' => 'image/gif',
    'webp' => 'image/webp',
    'mp4' => 'video/mp4',
    'webm' => 'video/webm',
    'pdf' => 'application/pdf',
    'txt' || 'md' => 'text/plain',
    _ => 'application/octet-stream',
  };
}
