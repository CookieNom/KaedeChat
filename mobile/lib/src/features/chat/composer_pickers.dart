import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:shared_preferences/shared_preferences.dart';

enum ComposerAction { attach, emoji, sticker, gif }

typedef ComposerEmojiLoader = Future<List<Map<String, Object?>>> Function();
typedef ComposerStickerLoader = Future<List<Map<String, Object?>>> Function();
typedef ComposerGifLoader = Future<Map<String, Object?>> Function({
  String? query,
  int page,
});

/// Replaces the current selection while retaining the native text cursor.
///
/// A null result means the replacement would exceed the message limit.
TextEditingValue? insertComposerText(
  TextEditingValue current,
  String insertion, {
  int maxLength = 4000,
}) {
  var start = current.selection.start;
  var end = current.selection.end;
  if (!current.selection.isValid ||
      start < 0 ||
      end < 0 ||
      start > current.text.length ||
      end > current.text.length) {
    start = current.text.length;
    end = start;
  }
  final nextText = current.text.replaceRange(start, end, insertion);
  if (nextText.length > maxLength) return null;
  return TextEditingValue(
    text: nextText,
    selection: TextSelection.collapsed(offset: start + insertion.length),
  );
}

bool composerAllowsGifs(KaedeChannel channel) =>
    channel.encryptionMode != 'e2ee';

final class ComposerCustomEmoji {
  const ComposerCustomEmoji({
    required this.ref,
    required this.name,
    required this.animated,
    required this.mediaHash,
    this.guildRef,
  });

  static final _namePattern = RegExp(r'^[A-Za-z0-9_]{2,32}$');

  static ComposerCustomEmoji? tryParse(Map<String, Object?> json) {
    try {
      final name = '${json['name'] ?? ''}'.trim();
      final mediaHash = '${json['media_hash'] ?? ''}'.trim();
      if (!_namePattern.hasMatch(name) || mediaHash.isEmpty) return null;
      final ref = EntityRef(
        Snowflake('${json['id']}'),
        Domain('${json['origin_domain']}'),
      );
      final guildId = json['guild_id'];
      final guildDomain = json['guild_domain'];
      EntityRef? guildRef;
      if (guildId != null || guildDomain != null) {
        if (guildId == null || guildDomain == null) return null;
        guildRef = EntityRef(
          Snowflake('$guildId'),
          Domain('$guildDomain'),
        );
      }
      return ComposerCustomEmoji(
        ref: ref,
        guildRef: guildRef,
        name: name,
        animated: json['animated'] == true,
        mediaHash: mediaHash,
      );
    } on FormatException {
      return null;
    }
  }

  final EntityRef ref;
  final EntityRef? guildRef;
  final String name;
  final bool animated;
  final String mediaHash;

  String get token => '<${animated ? 'a' : ''}:$name:${ref.wire}>';

  Uri get previewUri => Uri.https(
        ref.domain.value,
        '/media/emojis/${ref.id.value}/thumbnail_128',
      );
}

/// Shared renderer for the same canonical custom-emoji asset used by the
/// composer, forum tags, and forum default reactions.
final class CustomEmojiImage extends StatelessWidget {
  const CustomEmojiImage({
    required this.ref,
    required this.label,
    this.size = 20,
    super.key,
  });

  final EntityRef ref;
  final String label;
  final double size;

  @override
  Widget build(BuildContext context) => Semantics(
        image: true,
        label: label,
        child: ExcludeSemantics(
          child: CachedNetworkImage(
            imageUrl: Uri.https(
              ref.domain.value,
              '/media/emojis/${ref.id.value}/thumbnail_128',
            ).toString(),
            width: size,
            height: size,
            fit: BoxFit.contain,
            placeholder: (_, __) => SizedBox.square(dimension: size),
            errorWidget: (_, __, ___) => Text(
              label,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(fontSize: size <= 20 ? 10 : 12),
            ),
          ),
        ),
      );
}

EntityRef? customEmojiRef(String? rawId, Domain originDomain) {
  final id = rawId?.trim();
  if (id == null || id.isEmpty) return null;
  try {
    return EntityRef(Snowflake(id), originDomain);
  } on FormatException {
    return null;
  }
}

@visibleForTesting
EntityRef? forumTagCustomEmojiRef(ForumTag tag, Domain originDomain) =>
    customEmojiRef(tag.emojiId, originDomain);

final class ForumTagLabel extends StatelessWidget {
  const ForumTagLabel({
    required this.tag,
    required this.originDomain,
    this.fontSize,
    this.emojiSize = 16,
    super.key,
  });

  final ForumTag tag;
  final Domain originDomain;
  final double? fontSize;
  final double emojiSize;

  @override
  Widget build(BuildContext context) {
    final customRef = forumTagCustomEmojiRef(tag, originDomain);
    final unicode = tag.emojiName?.trim();
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (customRef != null) ...[
          CustomEmojiImage(
            ref: customRef,
            label: ':${tag.name}:',
            size: emojiSize,
          ),
          const SizedBox(width: 4),
        ] else if (unicode?.isNotEmpty == true) ...[
          Text(unicode!, style: TextStyle(fontSize: fontSize)),
          const SizedBox(width: 4),
        ],
        Text(tag.name, style: TextStyle(fontSize: fontSize)),
      ],
    );
  }
}

bool customEmojiAvailableInChannel(
  ComposerCustomEmoji emoji,
  KaedeChannel channel,
) {
  final guild = channel.guildRef;
  if (guild == null) return true;
  if (emoji.guildRef == guild) return true;
  return channel.allows(Permission.useExternalEmojis);
}

List<ComposerCustomEmoji> composerCustomEmojis(
  Iterable<Map<String, Object?>> response,
  KaedeChannel channel,
) {
  final items = response
      .map(ComposerCustomEmoji.tryParse)
      .whereType<ComposerCustomEmoji>()
      .where((emoji) => customEmojiAvailableInChannel(emoji, channel))
      .toList();
  items.sort((left, right) {
    final leftLocal = left.guildRef == channel.guildRef;
    final rightLocal = right.guildRef == channel.guildRef;
    if (leftLocal != rightLocal) return leftLocal ? -1 : 1;
    return left.name.toLowerCase().compareTo(right.name.toLowerCase());
  });
  return List.unmodifiable(items);
}

final class ComposerSticker {
  const ComposerSticker({
    required this.ref,
    required this.guildRef,
    required this.guildName,
    required this.name,
    required this.animated,
    required this.mediaHash,
    this.description,
  });

  static final _namePattern = RegExp(r'^[A-Za-z0-9_]{2,32}$');

  static ComposerSticker? tryParse(Map<String, Object?> json) {
    try {
      final name = '${json['name'] ?? ''}'.trim();
      final mediaHash = '${json['media_hash'] ?? ''}'.trim();
      final guildName = '${json['guild_name'] ?? ''}'.trim();
      if (!_namePattern.hasMatch(name) || mediaHash.isEmpty) return null;
      return ComposerSticker(
        ref: EntityRef(
          Snowflake('${json['id']}'),
          Domain('${json['origin_domain']}'),
        ),
        guildRef: EntityRef(
          Snowflake('${json['guild_id']}'),
          Domain('${json['guild_domain']}'),
        ),
        guildName: guildName.isEmpty ? '${json['guild_domain']}' : guildName,
        name: name,
        description: '${json['description'] ?? ''}'.trim().isEmpty
            ? null
            : '${json['description']}'.trim(),
        animated: json['animated'] == true,
        mediaHash: mediaHash,
      );
    } on FormatException {
      return null;
    }
  }

  final EntityRef ref;
  final EntityRef guildRef;
  final String guildName;
  final String name;
  final String? description;
  final bool animated;
  final String mediaHash;

  String get token => '<sticker:$name:${ref.wire}>';

  Uri uri({String variant = 'thumbnail_512'}) => Uri.https(
        ref.domain.value,
        '/media/stickers/${ref.id.value}/$variant',
      );
}

bool customStickerAvailableInChannel(
  ComposerSticker sticker,
  KaedeChannel channel,
) {
  final guild = channel.guildRef;
  if (guild == null || sticker.guildRef == guild) return true;
  return channel.allows(Permission.useExternalEmojis);
}

List<ComposerSticker> composerStickers(
  Iterable<Map<String, Object?>> response,
  KaedeChannel channel,
) {
  final items = response
      .map(ComposerSticker.tryParse)
      .whereType<ComposerSticker>()
      .where((sticker) => customStickerAvailableInChannel(sticker, channel))
      .toList();
  items.sort((left, right) {
    final leftLocal = left.guildRef == channel.guildRef;
    final rightLocal = right.guildRef == channel.guildRef;
    if (leftLocal != rightLocal) return leftLocal ? -1 : 1;
    final guildOrder =
        left.guildName.toLowerCase().compareTo(right.guildName.toLowerCase());
    if (guildOrder != 0) return guildOrder;
    return left.name.toLowerCase().compareTo(right.name.toLowerCase());
  });
  return List.unmodifiable(items);
}

final class StickerImage extends StatelessWidget {
  const StickerImage({
    required this.sticker,
    this.size = 96,
    super.key,
  });

  final ComposerSticker sticker;
  final double size;

  @override
  Widget build(BuildContext context) => Semantics(
        image: true,
        label: 'Sticker: ${sticker.name}',
        child: CachedNetworkImage(
          imageUrl: sticker.uri().toString(),
          width: size,
          height: size,
          fit: BoxFit.contain,
          placeholder: (_, __) => SizedBox.square(
            dimension: size,
            child:
                const Center(child: CircularProgressIndicator(strokeWidth: 2)),
          ),
          errorWidget: (_, __, ___) => SizedBox.square(
            dimension: size,
            child: Center(
              child: Text(
                ':${sticker.name}:',
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                textAlign: TextAlign.center,
              ),
            ),
          ),
        ),
      );
}

final class ComposerGif {
  const ComposerGif({
    required this.id,
    required this.title,
    required this.url,
    required this.previewUrl,
    this.width,
    this.height,
  });

  static const _allowedHosts = <String>{
    'media.klipy.com',
    'static.klipy.com',
  };

  static ComposerGif? tryParse(Map<String, Object?> json) {
    final id = '${json['id'] ?? ''}'.trim();
    final url = _safeUri(json['url']);
    if (id.isEmpty || url == null) return null;
    final previewUrl = _safeUri(json['preview_url']) ?? url;
    final rawTitle = '${json['title'] ?? ''}'.trim();
    return ComposerGif(
      id: id,
      title: rawTitle.isEmpty ? 'GIF' : rawTitle,
      url: url,
      previewUrl: previewUrl,
      width: _positiveInt(json['width']),
      height: _positiveInt(json['height']),
    );
  }

  static Uri? _safeUri(Object? value) {
    final uri = Uri.tryParse('${value ?? ''}'.trim());
    if (uri == null ||
        uri.scheme != 'https' ||
        !_allowedHosts.contains(uri.host.toLowerCase()) ||
        uri.userInfo.isNotEmpty ||
        (uri.hasPort && uri.port != 443)) {
      return null;
    }
    return uri;
  }

  static int? _positiveInt(Object? value) {
    final parsed = switch (value) {
      final int number => number,
      final num number => number.toInt(),
      _ => int.tryParse('$value'),
    };
    return parsed != null && parsed > 0 ? parsed : null;
  }

  final String id;
  final String title;
  final Uri url;
  final Uri previewUrl;
  final int? width;
  final int? height;

  Map<String, Object?> toJson() => <String, Object?>{
        'id': id,
        'title': title,
        'url': url.toString(),
        'preview_url': previewUrl.toString(),
        if (width != null) 'width': width,
        if (height != null) 'height': height,
      };
}

const composerGifFavoritesKey = 'kaede.gif-favorites.v1';

Future<List<ComposerGif>> loadComposerGifFavorites() async {
  final preferences = await SharedPreferences.getInstance();
  final raw = preferences.getStringList(composerGifFavoritesKey) ?? const [];
  final favorites = <ComposerGif>[];
  for (final encoded in raw) {
    try {
      final decoded = jsonDecode(encoded);
      if (decoded is Map) {
        final gif = ComposerGif.tryParse(
          decoded.map((key, value) => MapEntry('$key', value)),
        );
        if (gif != null) favorites.add(gif);
      }
    } on Object {
      // Ignore one corrupt local favorite without discarding the others.
    }
  }
  return List.unmodifiable(favorites.take(100));
}

Future<List<ComposerGif>> toggleComposerGifFavorite(ComposerGif gif) async {
  final current = [...await loadComposerGifFavorites()];
  final index = current.indexWhere(
      (item) => item.id == gif.id || item.url.toString() == gif.url.toString());
  if (index >= 0) {
    current.removeAt(index);
  } else {
    current.insert(0, gif);
  }
  final bounded = current.take(100).toList(growable: false);
  final preferences = await SharedPreferences.getInstance();
  await preferences.setStringList(
    composerGifFavoritesKey,
    bounded.map((item) => jsonEncode(item.toJson())).toList(growable: false),
  );
  return List.unmodifiable(bounded);
}

ComposerGif? composerGifFromMessage(String? content) {
  final uri = Uri.tryParse(content?.trim() ?? '');
  if (uri == null) return null;
  final parsed = ComposerGif.tryParse(<String, Object?>{
    'id': uri.toString(),
    'title': 'Saved GIF',
    'url': uri.toString(),
    'preview_url': uri.toString(),
  });
  return parsed;
}

final class ComposerGifPage {
  const ComposerGifPage({
    required this.items,
    required this.page,
    required this.nextPage,
  });

  factory ComposerGifPage.fromJson(Map<String, Object?> json) {
    final page = _integer(json['page']) ?? 1;
    final items = <ComposerGif>[];
    if (json['items'] case final List<Object?> rawItems) {
      for (final raw in rawItems) {
        if (raw is! Map) continue;
        final parsed = ComposerGif.tryParse(
          raw.map((key, value) => MapEntry('$key', value)),
        );
        if (parsed != null) items.add(parsed);
      }
    }
    final candidate = _integer(json['next_page']);
    return ComposerGifPage(
      items: List.unmodifiable(items),
      page: page,
      nextPage: candidate != null && candidate > page ? candidate : null,
    );
  }

  static int? _integer(Object? value) => switch (value) {
        final int number => number,
        final num number => number.toInt(),
        _ => int.tryParse('$value'),
      };

  final List<ComposerGif> items;
  final int page;
  final int? nextPage;
}

Future<ComposerAction?> showComposerActionPicker(
  BuildContext context, {
  required bool canAttach,
  required bool gifsAllowed,
}) =>
    showModalBottomSheet<ComposerAction>(
      context: context,
      showDragHandle: true,
      builder: (context) => SafeArea(
        top: false,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              ListTile(
                key: const ValueKey('composer-action-attach'),
                leading: const Icon(Icons.attach_file_rounded),
                title: const Text('Attach files'),
                subtitle: Text(canAttach
                    ? 'Upload images, video, audio, or documents'
                    : 'You do not have permission to attach files'),
                enabled: canAttach,
                onTap: canAttach
                    ? () => Navigator.pop(context, ComposerAction.attach)
                    : null,
              ),
              ListTile(
                key: const ValueKey('composer-action-emoji'),
                leading: const Icon(Icons.emoji_emotions_outlined),
                title: const Text('Emoji'),
                subtitle: const Text('Choose Unicode or guild emoji'),
                onTap: () => Navigator.pop(context, ComposerAction.emoji),
              ),
              ListTile(
                key: const ValueKey('composer-action-sticker'),
                leading: const Icon(Icons.sticky_note_2_outlined),
                title: const Text('Stickers'),
                subtitle: const Text('Choose a sticker from your guilds'),
                onTap: () => Navigator.pop(context, ComposerAction.sticker),
              ),
              ListTile(
                key: const ValueKey('composer-action-gif'),
                leading: const Icon(Icons.gif_box_outlined),
                title: const Text('GIF'),
                subtitle: Text(gifsAllowed
                    ? 'Search the GIF library'
                    : 'Unavailable in end-to-end encrypted conversations'),
                enabled: gifsAllowed,
                onTap: gifsAllowed
                    ? () => Navigator.pop(context, ComposerAction.gif)
                    : null,
              ),
              const SizedBox(height: 4),
            ],
          ),
        ),
      ),
    );

Future<String?> showComposerEmojiPicker(
  BuildContext context, {
  required KaedeRepository repository,
  required KaedeChannel channel,
  required Map<String, List<String>> categories,
  List<String> recent = const <String>[],
}) =>
    showModalBottomSheet<String>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      builder: (context) => ComposerEmojiPicker(
        loader: repository.emojis,
        channel: channel,
        categories: categories,
        recent: recent,
      ),
    );

Future<ComposerGif?> showComposerGifPicker(
  BuildContext context, {
  required KaedeRepository repository,
}) =>
    showModalBottomSheet<ComposerGif>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      builder: (context) => ComposerGifPicker(loader: repository.gifs),
    );

Future<ComposerSticker?> showComposerStickerPicker(
  BuildContext context, {
  required KaedeRepository repository,
  required KaedeChannel channel,
}) =>
    showModalBottomSheet<ComposerSticker>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      builder: (context) => ComposerStickerPicker(
        loader: repository.stickers,
        channel: channel,
      ),
    );

final class ComposerStickerPicker extends StatefulWidget {
  const ComposerStickerPicker({
    super.key,
    required this.loader,
    required this.channel,
  });

  final ComposerStickerLoader loader;
  final KaedeChannel channel;

  @override
  State<ComposerStickerPicker> createState() => _ComposerStickerPickerState();
}

final class _ComposerStickerPickerState extends State<ComposerStickerPicker> {
  final _search = TextEditingController();
  List<ComposerSticker> _items = const [];
  Object? _error;
  var _loading = true;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final response = await widget.loader();
      if (!mounted) return;
      setState(() {
        _items = composerStickers(response, widget.channel);
        _loading = false;
      });
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        _error = error;
        _loading = false;
      });
    }
  }

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final query = _search.text.trim().toLowerCase();
    final filtered = _items
        .where((item) =>
            query.isEmpty ||
            item.name.toLowerCase().contains(query) ||
            item.guildName.toLowerCase().contains(query) ||
            (item.description?.toLowerCase().contains(query) ?? false))
        .toList(growable: false);
    final groups = <EntityRef, List<ComposerSticker>>{};
    for (final item in filtered) {
      groups.putIfAbsent(item.guildRef, () => <ComposerSticker>[]).add(item);
    }
    return _KeyboardSafePickerSheet(
      maxHeight: 620,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 0, 12, 10),
        child: Column(
          children: [
            TextField(
              key: const ValueKey('composer-sticker-search'),
              controller: _search,
              textInputAction: TextInputAction.search,
              decoration: const InputDecoration(
                hintText: 'Search stickers',
                prefixIcon: Icon(Icons.search_rounded),
                isDense: true,
              ),
              onChanged: (_) => setState(() {}),
            ),
            const SizedBox(height: 8),
            Expanded(
              child: _loading
                  ? const _PickerStatus(
                      icon: CircularProgressIndicator(strokeWidth: 2),
                      message: 'Loading stickers…',
                    )
                  : _error != null
                      ? _PickerError(
                          message: userFacingError(_error!,
                              summary: 'Could not load stickers'),
                          onRetry: _load,
                        )
                      : groups.isEmpty
                          ? const _PickerStatus(
                              icon: Icon(Icons.sticky_note_2_outlined),
                              message: 'No stickers found.',
                            )
                          : ListView.builder(
                              key: const ValueKey('composer-sticker-groups'),
                              keyboardDismissBehavior:
                                  ScrollViewKeyboardDismissBehavior.onDrag,
                              itemCount: groups.length,
                              itemBuilder: (context, index) {
                                final stickers = groups.values.elementAt(index);
                                final guildName = stickers.first.guildName;
                                return Padding(
                                  padding: const EdgeInsets.only(bottom: 14),
                                  child: Column(
                                    crossAxisAlignment:
                                        CrossAxisAlignment.start,
                                    children: [
                                      Padding(
                                        padding: const EdgeInsets.symmetric(
                                            horizontal: 4, vertical: 6),
                                        child: Text(
                                          guildName,
                                          style: const TextStyle(
                                            color: KaedeColors.muted,
                                            fontWeight: FontWeight.w700,
                                          ),
                                        ),
                                      ),
                                      GridView.builder(
                                        shrinkWrap: true,
                                        physics:
                                            const NeverScrollableScrollPhysics(),
                                        gridDelegate:
                                            const SliverGridDelegateWithMaxCrossAxisExtent(
                                          maxCrossAxisExtent: 112,
                                          mainAxisExtent: 116,
                                          mainAxisSpacing: 6,
                                          crossAxisSpacing: 6,
                                        ),
                                        itemCount: stickers.length,
                                        itemBuilder: (context, stickerIndex) {
                                          final sticker =
                                              stickers[stickerIndex];
                                          return Tooltip(
                                            message: sticker.description ??
                                                sticker.name,
                                            child: InkWell(
                                              onTap: () => Navigator.pop(
                                                  context, sticker),
                                              borderRadius:
                                                  BorderRadius.circular(10),
                                              child: Column(
                                                mainAxisAlignment:
                                                    MainAxisAlignment.center,
                                                children: [
                                                  StickerImage(
                                                      sticker: sticker,
                                                      size: 82),
                                                  Text(
                                                    sticker.name,
                                                    maxLines: 1,
                                                    overflow:
                                                        TextOverflow.ellipsis,
                                                    style: const TextStyle(
                                                        fontSize: 11),
                                                  ),
                                                ],
                                              ),
                                            ),
                                          );
                                        },
                                      ),
                                    ],
                                  ),
                                );
                              },
                            ),
            ),
          ],
        ),
      ),
    );
  }
}

final class ComposerEmojiPicker extends StatefulWidget {
  const ComposerEmojiPicker({
    super.key,
    required this.loader,
    required this.channel,
    required this.categories,
    this.recent = const <String>[],
  });

  final ComposerEmojiLoader loader;
  final KaedeChannel channel;
  final Map<String, List<String>> categories;
  final List<String> recent;

  @override
  State<ComposerEmojiPicker> createState() => _ComposerEmojiPickerState();
}

final class _ComposerEmojiPickerState extends State<ComposerEmojiPicker> {
  final _search = TextEditingController();
  List<ComposerCustomEmoji> _custom = const [];
  Object? _error;
  var _loading = true;
  late String _category;

  @override
  void initState() {
    super.initState();
    _category = widget.recent.isNotEmpty
        ? 'Recent'
        : widget.categories.containsKey('Smileys')
            ? 'Smileys'
            : widget.categories.keys.firstOrNull ?? 'Custom';
    unawaited(_loadCustom());
  }

  Future<void> _loadCustom() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final response = await widget.loader();
      if (!mounted) return;
      setState(() {
        _custom = composerCustomEmojis(response, widget.channel);
        _loading = false;
      });
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        _error = error;
        _loading = false;
      });
    }
  }

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => _KeyboardSafePickerSheet(
        maxHeight: 560,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(12, 0, 12, 10),
          child: _KeyboardResponsivePickerBody(
            expandedBreakpoint: 170,
            compactBodyHeight: 140,
            header: [
              TextField(
                key: const ValueKey('composer-emoji-search'),
                controller: _search,
                textInputAction: TextInputAction.search,
                decoration: const InputDecoration(
                  hintText: 'Search emoji',
                  prefixIcon: Icon(Icons.search_rounded),
                  isDense: true,
                ),
                onChanged: (_) => setState(() {}),
              ),
              const SizedBox(height: 8),
              SizedBox(
                height: 38,
                child: ListView.separated(
                  scrollDirection: Axis.horizontal,
                  itemCount: widget.categories.length + 1,
                  separatorBuilder: (_, __) => const SizedBox(width: 6),
                  itemBuilder: (context, index) {
                    final name = index == widget.categories.length
                        ? 'Custom'
                        : widget.categories.keys.elementAt(index);
                    return ChoiceChip(
                      label: Text(name),
                      selected: _category == name && _search.text.isEmpty,
                      onSelected: (_) => setState(() {
                        _category = name;
                        _search.clear();
                      }),
                    );
                  },
                ),
              ),
              const SizedBox(height: 8),
            ],
            body: _buildChoices(context),
          ),
        ),
      );

  Widget _buildChoices(BuildContext context) {
    final query = _search.text.trim().toLowerCase();
    if (query.isEmpty && _category == 'Custom') {
      if (_loading) {
        return const _PickerStatus(
          icon: CircularProgressIndicator(strokeWidth: 2),
          message: 'Loading custom emoji…',
        );
      }
      if (_error case final error?) {
        return _PickerError(
          message: userFacingError(error, summary: 'Could not load emoji'),
          onRetry: _loadCustom,
        );
      }
    }

    final choices = <_ComposerEmojiChoice>[];
    if (query.isNotEmpty) {
      final unicode = <String>{
        ...widget.recent,
        ...widget.categories.values.expand((items) => items),
      };
      choices.addAll(unicode
          .where((emoji) => emoji.toLowerCase().contains(query))
          .map(_ComposerEmojiChoice.unicode));
      choices.addAll(_custom
          .where((emoji) => emoji.name.toLowerCase().contains(query))
          .map(_ComposerEmojiChoice.custom));
    } else if (_category == 'Custom') {
      choices.addAll(_custom.map(_ComposerEmojiChoice.custom));
    } else {
      final unicode = _category == 'Recent'
          ? widget.recent
          : widget.categories[_category] ?? const <String>[];
      choices.addAll(unicode.map(_ComposerEmojiChoice.unicode));
    }

    if (choices.isEmpty) {
      return _PickerStatus(
        icon: const Icon(Icons.emoji_emotions_outlined),
        message: query.isEmpty && _category == 'Recent'
            ? 'Recently used emoji will appear here.'
            : 'No emoji found.',
      );
    }
    return LayoutBuilder(builder: (context, constraints) {
      final columns = max(5, min(8, (constraints.maxWidth / 52).floor()));
      return GridView.builder(
        key: const ValueKey('composer-emoji-grid'),
        keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
        gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
          crossAxisCount: columns,
          mainAxisSpacing: 4,
          crossAxisSpacing: 4,
        ),
        itemCount: choices.length,
        itemBuilder: (context, index) {
          final choice = choices[index];
          return Semantics(
            button: true,
            label: 'Insert ${choice.label}',
            child: ExcludeSemantics(
              child: Tooltip(
                message: choice.label,
                child: InkWell(
                  onTap: () => Navigator.pop(context, choice.value),
                  borderRadius: BorderRadius.circular(10),
                  child: Center(child: choice.build()),
                ),
              ),
            ),
          );
        },
      );
    });
  }
}

final class _ComposerEmojiChoice {
  const _ComposerEmojiChoice._({
    required this.value,
    required this.label,
    this.emoji,
    this.customEmoji,
  });

  factory _ComposerEmojiChoice.unicode(String emoji) =>
      _ComposerEmojiChoice._(value: emoji, label: emoji, emoji: emoji);

  factory _ComposerEmojiChoice.custom(ComposerCustomEmoji emoji) =>
      _ComposerEmojiChoice._(
        value: emoji.token,
        label: ':${emoji.name}:',
        customEmoji: emoji,
      );

  final String value;
  final String label;
  final String? emoji;
  final ComposerCustomEmoji? customEmoji;

  Widget build() {
    if (customEmoji case final custom?) {
      return CustomEmojiImage(
        ref: custom.ref,
        label: label,
        size: 34,
      );
    }
    return Text(emoji!, style: const TextStyle(fontSize: 25));
  }
}

final class ComposerGifPicker extends StatefulWidget {
  const ComposerGifPicker({super.key, required this.loader});

  final ComposerGifLoader loader;

  @override
  State<ComposerGifPicker> createState() => _ComposerGifPickerState();
}

final class _ComposerGifPickerState extends State<ComposerGifPicker> {
  final _search = TextEditingController();
  final _items = <ComposerGif>[];
  List<ComposerGif> _favorites = const [];
  Timer? _debounce;
  Object? _error;
  int? _nextPage;
  var _generation = 0;
  var _loading = true;
  var _loadingMore = false;
  var _searchPending = false;

  @override
  void initState() {
    super.initState();
    unawaited(_loadFavorites());
    unawaited(_load(page: 1, append: false));
  }

  Future<void> _loadFavorites() async {
    final favorites = await loadComposerGifFavorites();
    if (mounted) setState(() => _favorites = favorites);
  }

  Future<void> _toggleFavorite(ComposerGif gif) async {
    final favorites = await toggleComposerGifFavorite(gif);
    if (mounted) setState(() => _favorites = favorites);
  }

  bool _favorite(ComposerGif gif) => _favorites.any(
      (item) => item.id == gif.id || item.url.toString() == gif.url.toString());

  List<ComposerGif> get _visibleItems {
    final query = _search.text.trim().toLowerCase();
    final preferred = query.isEmpty
        ? _favorites
        : _favorites.where((item) => item.title.toLowerCase().contains(query));
    final seen = <String>{};
    return <ComposerGif>[
      for (final gif in [...preferred, ..._items])
        if (seen.add(gif.url.toString())) gif,
    ];
  }

  void _queryChanged(String _) {
    _debounce?.cancel();
    _generation += 1;
    setState(() {
      _searchPending = true;
      _loadingMore = false;
      _error = null;
    });
    _debounce = Timer(const Duration(milliseconds: 300), () {
      if (mounted) unawaited(_load(page: 1, append: false));
    });
  }

  Future<void> _load({required int page, required bool append}) async {
    if (append && (_loading || _loadingMore || _searchPending)) return;
    final request = ++_generation;
    setState(() {
      _searchPending = false;
      if (append) {
        _loadingMore = true;
      } else {
        _loading = true;
        _items.clear();
        _nextPage = null;
      }
      _error = null;
    });
    try {
      final query = _search.text.trim();
      final response = await widget.loader(
        query: query.isEmpty ? null : query,
        page: page,
      );
      final result = ComposerGifPage.fromJson(response);
      if (!mounted || request != _generation) return;
      setState(() {
        if (!append) _items.clear();
        final known = _items.map((item) => item.id).toSet();
        _items.addAll(result.items.where((item) => known.add(item.id)));
        _nextPage = result.nextPage;
        _loading = false;
        _loadingMore = false;
      });
    } on Object catch (error) {
      if (!mounted || request != _generation) return;
      setState(() {
        _error = error;
        _loading = false;
        _loadingMore = false;
      });
    }
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _search.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => _KeyboardSafePickerSheet(
        maxHeight: 620,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
          child: _KeyboardResponsivePickerBody(
            expandedBreakpoint: 160,
            compactBodyHeight: 180,
            header: [
              TextField(
                key: const ValueKey('composer-gif-search'),
                controller: _search,
                textInputAction: TextInputAction.search,
                decoration: const InputDecoration(
                  hintText: 'Search GIFs',
                  prefixIcon: Icon(Icons.search_rounded),
                  isDense: true,
                ),
                onChanged: _queryChanged,
                onSubmitted: (_) {
                  _debounce?.cancel();
                  unawaited(_load(page: 1, append: false));
                },
              ),
              if (_searchPending || (_loading && _items.isNotEmpty))
                const LinearProgressIndicator(minHeight: 2),
              const SizedBox(height: 8),
            ],
            body: _buildResults(),
            footer: const [
              SizedBox(height: 5),
              Text(
                'Powered by KLIPY',
                textAlign: TextAlign.center,
                style: TextStyle(color: KaedeColors.muted, fontSize: 11),
              ),
            ],
          ),
        ),
      );

  Widget _buildResults() {
    if (_loading && _items.isEmpty && _favorites.isEmpty) {
      return const _PickerStatus(
        icon: CircularProgressIndicator(strokeWidth: 2),
        message: 'Loading GIFs…',
      );
    }
    if (_error case final error? when _items.isEmpty && _favorites.isEmpty) {
      return _PickerError(
        message: userFacingError(error, summary: 'Could not load GIFs'),
        onRetry: () => _load(page: 1, append: false),
      );
    }
    final visible = _visibleItems;
    if (visible.isEmpty) {
      return const _PickerStatus(
        icon: Icon(Icons.gif_box_outlined),
        message: 'No GIFs found. Try another search.',
      );
    }
    return Column(
      children: [
        Expanded(
          child: LayoutBuilder(builder: (context, constraints) {
            final columns = constraints.maxWidth >= 520 ? 3 : 2;
            return GridView.builder(
              key: const ValueKey('composer-gif-grid'),
              keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
              gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
                crossAxisCount: columns,
                mainAxisSpacing: 8,
                crossAxisSpacing: 8,
                childAspectRatio: 1.18,
              ),
              itemCount: visible.length,
              itemBuilder: (context, index) {
                final gif = visible[index];
                return Semantics(
                  button: true,
                  label: 'Send GIF: ${gif.title}',
                  child: ExcludeSemantics(
                    child: Tooltip(
                      message: gif.title,
                      child: InkWell(
                        onTap: () => Navigator.pop(context, gif),
                        borderRadius: BorderRadius.circular(12),
                        child: ClipRRect(
                          borderRadius: BorderRadius.circular(12),
                          child: ColoredBox(
                            color: KaedeColors.raised,
                            child: Stack(
                              fit: StackFit.expand,
                              children: [
                                CachedNetworkImage(
                                  imageUrl: gif.previewUrl.toString(),
                                  fit: BoxFit.cover,
                                  placeholder: (_, __) => const Center(
                                    child: CircularProgressIndicator(
                                        strokeWidth: 2),
                                  ),
                                  errorWidget: (_, __, ___) => const Center(
                                    child: Icon(Icons.broken_image_outlined),
                                  ),
                                ),
                                Positioned(
                                  right: 5,
                                  top: 5,
                                  child: IconButton.filledTonal(
                                    tooltip: _favorite(gif)
                                        ? 'Remove from favorites'
                                        : 'Add to favorites',
                                    onPressed: () => _toggleFavorite(gif),
                                    icon: Icon(_favorite(gif)
                                        ? Icons.star_rounded
                                        : Icons.star_border_rounded),
                                  ),
                                ),
                              ],
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                );
              },
            );
          }),
        ),
        if (_error case final error?) ...[
          const SizedBox(height: 6),
          Text(
            userFacingError(error, summary: 'Could not load more GIFs'),
            textAlign: TextAlign.center,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(color: KaedeColors.danger, fontSize: 12),
          ),
        ],
        if (_loadingMore)
          const Padding(
            padding: EdgeInsets.all(10),
            child: SizedBox.square(
              dimension: 22,
              child: CircularProgressIndicator(strokeWidth: 2),
            ),
          )
        else if (_nextPage case final page?)
          TextButton.icon(
            key: const ValueKey('composer-gif-load-more'),
            onPressed: () => _load(page: page, append: true),
            icon: const Icon(Icons.expand_more_rounded),
            label: Text(_error == null ? 'Load more' : 'Retry load more'),
          ),
      ],
    );
  }
}

final class _KeyboardResponsivePickerBody extends StatelessWidget {
  const _KeyboardResponsivePickerBody({
    required this.expandedBreakpoint,
    required this.compactBodyHeight,
    required this.header,
    required this.body,
    this.footer = const <Widget>[],
  });

  final double expandedBreakpoint;
  final double compactBodyHeight;
  final List<Widget> header;
  final Widget body;
  final List<Widget> footer;

  @override
  Widget build(BuildContext context) => LayoutBuilder(
        builder: (context, constraints) {
          if (constraints.maxHeight >= expandedBreakpoint) {
            return Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                ...header,
                Expanded(child: body),
                ...footer,
              ],
            );
          }
          return ListView(
            key: const ValueKey('composer-picker-compact-scroll'),
            keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
            children: [
              ...header,
              SizedBox(height: compactBodyHeight, child: body),
              ...footer,
            ],
          );
        },
      );
}

final class _KeyboardSafePickerSheet extends StatelessWidget {
  const _KeyboardSafePickerSheet({
    required this.maxHeight,
    required this.child,
  });

  final double maxHeight;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    final media = MediaQuery.of(context);
    final usableHeight = max(
      0.0,
      media.size.height -
          media.padding.top -
          media.padding.bottom -
          media.viewInsets.bottom -
          12,
    );
    return AnimatedPadding(
      duration: const Duration(milliseconds: 180),
      curve: Curves.easeOutCubic,
      padding: EdgeInsets.only(bottom: media.viewInsets.bottom),
      child: SizedBox(
        height: min(maxHeight, usableHeight),
        child: SafeArea(top: false, child: child),
      ),
    );
  }
}

final class _PickerStatus extends StatelessWidget {
  const _PickerStatus({required this.icon, required this.message});

  final Widget icon;
  final String message;

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(20),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              SizedBox.square(dimension: 28, child: Center(child: icon)),
              const SizedBox(height: 10),
              Text(
                message,
                textAlign: TextAlign.center,
                style: const TextStyle(color: KaedeColors.muted),
              ),
            ],
          ),
        ),
      );
}

final class _PickerError extends StatelessWidget {
  const _PickerError({required this.message, required this.onRetry});

  final String message;
  final Future<void> Function() onRetry;

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(20),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.cloud_off_outlined, color: KaedeColors.danger),
              const SizedBox(height: 10),
              Text(message, textAlign: TextAlign.center),
              const SizedBox(height: 8),
              OutlinedButton.icon(
                onPressed: () => unawaited(onRetry()),
                icon: const Icon(Icons.refresh_rounded),
                label: const Text('Retry'),
              ),
            ],
          ),
        ),
      );
}
