import 'dart:async';
import 'dart:io';
import 'dart:math';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/e2ee/media.dart';
import 'package:kaede_mobile/src/features/chat/composer_pickers.dart';
import 'package:kaede_mobile/src/features/shared/remote_media.dart';
import 'package:kaede_mobile/src/features/voice/voice_room.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/storage/local_database.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:markdown/markdown.dart' as md;
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:video_player/video_player.dart';

final _mentionPattern = RegExp(r'<@([1-9][0-9]{0,18}@[A-Za-z0-9.-]+)>');
final _urlPattern = RegExp(r'https?://[^\s<>"\u0027]+', caseSensitive: false);
const _messageSpoilerPattern = r'\|\|([^|](?:(?!\|\|)[\s\S])*?)\|\|';
const _messageTokenPattern =
    r'(<a?:[A-Za-z0-9_]{2,32}:[1-9][0-9]{0,18}@[A-Za-z0-9.-]{1,253}>|<@&[1-9][0-9]{0,18}@[A-Za-z0-9.-]+>|<@[1-9][0-9]{0,18}(?:@[A-Za-z0-9.-]+)?>|@[A-Za-z0-9_.-]{1,64}@[A-Za-z0-9.-]+|#[A-Za-z0-9_-]{1,100})';
final _messageSpoilerRegExp = RegExp(_messageSpoilerPattern);
final _messageTokenRegExp = RegExp(_messageTokenPattern);

/// Deep link to a message that the web and desktop clients can open. Guild
/// channels live under `/g/<guild>/<channel>` and conversations under
/// `/home/<channel>`, both selecting a message with `?around=`.
String messageLink({
  required String instance,
  required KaedeChannel channel,
  required EntityRef message,
}) {
  final guild = channel.guildRef;
  final route = guild == null
      ? '/home/${Uri.encodeComponent(channel.ref.wire)}'
      : '/g/${Uri.encodeComponent(guild.wire)}/'
          '${Uri.encodeComponent(channel.ref.wire)}';
  return 'https://$instance$route?around=${Uri.encodeComponent(message.wire)}';
}

int messageListItemIndex({
  required int messageCount,
  required int messageIndex,
  int pendingCount = 0,
}) =>
    pendingCount + messageCount - messageIndex - 1;

@visibleForTesting
bool messageJumpRevealIsCurrent({
  required MessageJumpRequest request,
  required EntityRef? renderedChannel,
  required int? handledGeneration,
}) =>
    request.channel == renderedChannel &&
    request.generation == handledGeneration;

final _explicitMentionToken = RegExp(
  r'^<@([1-9][0-9]{0,18})(?:@([A-Za-z0-9.-]+))?>$',
);
final _roleMentionToken = RegExp(
  r'^<@&([1-9][0-9]{0,18})@([A-Za-z0-9.-]+)>$',
);
final _customEmojiToken = RegExp(
  r'^<(?:(a)?):([A-Za-z0-9_]{2,32}):([1-9][0-9]{0,18})@([A-Za-z0-9.-]{1,253})>$',
);

final class MessageSpoilerSyntax extends md.InlineSyntax {
  MessageSpoilerSyntax()
      : super(
          _messageSpoilerPattern,
          startCharacter: 0x7c,
        );

  @override
  bool onMatch(md.InlineParser parser, Match match) {
    parser.addNode(md.Element.text('kaede-spoiler', match.group(1)!));
    return true;
  }
}

final class MessageTokenSyntax extends md.InlineSyntax {
  MessageTokenSyntax()
      : super(
          _messageTokenPattern,
        );

  @override
  bool onMatch(md.InlineParser parser, Match match) {
    final token = match.group(1)!;
    final tag = token.startsWith('<@&')
        ? 'kaede-role-mention'
        : token.startsWith('<:') || token.startsWith('<a:')
            ? 'kaede-custom-emoji'
            : token.startsWith('#')
                ? 'kaede-channel-token'
                : 'kaede-user-mention';
    final element = md.Element.text(tag, token);
    element.attributes['token'] = token;
    parser.addNode(element);
    return true;
  }
}

List<EntityRef> mentionReferences(String content) {
  final references = <EntityRef>{};
  for (final match in _mentionPattern.allMatches(content)) {
    try {
      references.add(EntityRef.parse(match.group(1)!));
    } on FormatException {
      // Leave malformed user-authored markup as ordinary text.
    }
  }
  return List.unmodifiable(references);
}

/// Null means the MLS application is unavailable on this device. An exact
/// empty string is successful decryption for an attachment-only message and
/// remains valid report evidence after explicit consent.
bool encryptedReportEvidenceAvailable(KaedeMessage message) =>
    message.e2ee != null && message.content != null;

bool canSubmitMessageReport(
  KaedeMessage message, {
  required bool disclosureAcknowledged,
}) =>
    message.e2ee == null ||
    (encryptedReportEvidenceAvailable(message) && disclosureAcknowledged);

const _automaticHistoryLoadThreshold = 320.0;
const _defaultRecentReactions = <String>['❤️', '😂', '👍', '🔥'];
const _reactionEmojiCategories = <String, List<String>>{
  'Recent': <String>[],
  'Smileys': <String>[
    '😀',
    '😃',
    '😄',
    '😁',
    '😂',
    '🤣',
    '😊',
    '😍',
    '🥰',
    '😘',
    '😎',
    '🤩',
    '🥳',
    '😏',
    '😢',
    '😭',
    '😤',
    '😡',
    '🤯',
    '😱',
    '🤔',
    '🫡',
    '🫠',
    '👀'
  ],
  'People': <String>[
    '👍',
    '👎',
    '👏',
    '🙌',
    '🙏',
    '🤝',
    '💪',
    '👌',
    '✌️',
    '🤞',
    '🤟',
    '🤘',
    '👋',
    '🫶',
    '💅',
    '🧠'
  ],
  'Nature': <String>[
    '🐶',
    '🐱',
    '🐭',
    '🐹',
    '🐰',
    '🦊',
    '🐻',
    '🐼',
    '🐸',
    '🐵',
    '🦄',
    '🐝',
    '🌸',
    '🌻',
    '🌈',
    '⭐'
  ],
  'Food': <String>[
    '🍎',
    '🍓',
    '🍉',
    '🍕',
    '🍔',
    '🍟',
    '🌮',
    '🍿',
    '🍪',
    '🎂',
    '☕',
    '🍺'
  ],
  'Activities': <String>[
    '⚽',
    '🏀',
    '🏈',
    '🎮',
    '🎲',
    '🎨',
    '🎵',
    '🎉',
    '🏆',
    '🚀',
    '💡',
    '📌'
  ],
  'Symbols': <String>[
    '❤️',
    '🧡',
    '💛',
    '💚',
    '💙',
    '💜',
    '🖤',
    '🤍',
    '💯',
    '🔥',
    '✨',
    '✅',
    '❌',
    '⚠️',
    '❓',
    '‼️'
  ],
};

List<String> rankRecentReactions(List<String> history, {int limit = 4}) {
  if (history.isEmpty) return _defaultRecentReactions.take(limit).toList();
  final counts = <String, int>{};
  final lastUsed = <String, int>{};
  for (var index = 0; index < history.length; index++) {
    final emoji = history[index];
    counts[emoji] = (counts[emoji] ?? 0) + 1;
    lastUsed[emoji] = index;
  }
  final ranked = counts.keys.toList()
    ..sort((left, right) {
      final byCount = counts[right]!.compareTo(counts[left]!);
      return byCount != 0
          ? byCount
          : lastUsed[right]!.compareTo(lastUsed[left]!);
    });
  return ranked.take(limit).toList();
}

@visibleForTesting
bool shouldAutomaticallyLoadEarlier({
  required double pixels,
  required double maxScrollExtent,
  required bool hasEarlier,
  required bool loading,
  required bool hasError,
}) =>
    hasEarlier &&
    !loading &&
    !hasError &&
    pixels >= maxScrollExtent - _automaticHistoryLoadThreshold;

String renderMentionLabels(String content, MobileState state) =>
    content.replaceAllMapped(_mentionPattern, (match) {
      final reference = EntityRef.parse(match.group(1)!);
      KaedeUser? user = state.userProfiles[reference];
      if (state.user?.ref == reference) user = state.user;
      for (final dm in state.dms) {
        for (final candidate in dm.recipients) {
          if (candidate.ref == reference && user == null) user = candidate;
        }
      }
      for (final messages in state.messageStore.values) {
        for (final message in messages) {
          if (message.author?.ref == reference && user == null) {
            user = message.author;
          }
        }
      }
      final label = (user?.name ?? reference.wire)
          .replaceAll(r'\', r'\\')
          .replaceAll(']', r'\]');
      return '[@$label](kaede-mention://${reference.wire})';
    });

Uri? previewMediaUrl(String content) {
  final visibleContent = content.replaceAll(_messageSpoilerRegExp, ' ');
  for (final match in _urlPattern.allMatches(visibleContent)) {
    final raw = match.group(0)!.replaceFirst(RegExp(r'[),.!?:;\]}]+$'), '');
    final uri = Uri.tryParse(raw);
    if (uri == null || !const <String>{'http', 'https'}.contains(uri.scheme)) {
      continue;
    }
    final path = uri.path.toLowerCase();
    if (path.endsWith('.gif') ||
        path.endsWith('.webp') ||
        path.endsWith('.png') ||
        path.endsWith('.jpg') ||
        path.endsWith('.jpeg') ||
        path.endsWith('.mp4')) {
      return uri;
    }
  }
  return null;
}

String spoilerSafeReplyPreview(String content) =>
    content.replaceAll(_messageSpoilerRegExp, 'Spoiler');

String _withoutVisibleMediaUrl(String content, Uri mediaUrl) {
  final target = mediaUrl.toString();
  var cursor = 0;
  for (final spoiler in _messageSpoilerRegExp.allMatches(content)) {
    final before = content.substring(cursor, spoiler.start);
    final relative = before.indexOf(target);
    if (relative >= 0) {
      final start = cursor + relative;
      return content.replaceRange(start, start + target.length, '');
    }
    cursor = spoiler.end;
  }
  final relative = content.substring(cursor).indexOf(target);
  if (relative < 0) return content;
  final start = cursor + relative;
  return content.replaceRange(start, start + target.length, '');
}

KaedeUser? _knownMessageUser(MobileState state, EntityRef reference) {
  if (state.user?.ref == reference) return state.user;
  final profile = state.userProfiles[reference];
  if (profile != null) return profile;
  for (final dm in state.dms) {
    for (final user in dm.recipients) {
      if (user.ref == reference) return user;
    }
  }
  for (final messages in state.messageStore.values) {
    for (final message in messages) {
      if (message.author?.ref == reference) return message.author;
    }
  }
  return null;
}

EntityRef? _mentionTokenReference(String token, MobileState state) {
  final match = _explicitMentionToken.firstMatch(token);
  if (match == null) return null;
  final domain = match.group(2) ??
      state.user?.ref.domain.value ??
      state.activeGuild?.ref.domain.value;
  if (domain == null) return null;
  try {
    return EntityRef(Snowflake(match.group(1)!), Domain(domain));
  } on FormatException {
    return null;
  }
}

KaedeUser? _mentionTokenUser(String token, MobileState state) {
  final reference = _mentionTokenReference(token, state);
  if (reference != null) return _knownMessageUser(state, reference);
  if (!token.startsWith('@')) return null;
  final handle = token.substring(1).toLowerCase();
  final candidates = <KaedeUser?>[
    state.user,
    ...state.userProfiles.values,
    for (final dm in state.dms) ...dm.recipients,
  ];
  for (final user in candidates) {
    if (user?.handle.toLowerCase() == handle) return user;
  }
  return null;
}

KaedeRole? _mentionTokenRole(String token, MobileState state) {
  final match = _roleMentionToken.firstMatch(token);
  if (match == null) return null;
  try {
    final reference =
        EntityRef(Snowflake(match.group(1)!), Domain(match.group(2)!));
    for (final role in state.activeGuild?.roles ?? const <KaedeRole>[]) {
      if (role.ref == reference) return role;
    }
  } on FormatException {
    // Malformed federated markup remains an unknown role token.
  }
  return null;
}

int _messageMarkdownRevision(String content, MobileState state) {
  final values = <Object?>[content];
  for (final match in _messageTokenRegExp.allMatches(content)) {
    final token = match.group(1)!;
    if (token.startsWith('<@&')) {
      final role = _mentionTokenRole(token, state);
      values.addAll(<Object?>[
        token,
        role?.ref.wire,
        role?.name,
        role?.color,
      ]);
    } else if (token.startsWith('@') || token.startsWith('<@')) {
      final user = _mentionTokenUser(token, state);
      values.addAll(<Object?>[
        token,
        user?.ref.wire,
        user?.name,
        user?.profileResolved,
      ]);
    }
  }
  return Object.hashAll(values);
}

final class KaedeMessageMarkdown extends StatelessWidget {
  const KaedeMessageMarkdown({
    required this.content,
    required this.state,
    this.omitMediaUrl,
    super.key,
  });

  final String content;
  final MobileState state;
  final Uri? omitMediaUrl;

  @override
  Widget build(BuildContext context) {
    final data = (omitMediaUrl == null
            ? content
            : _withoutVisibleMediaUrl(content, omitMediaUrl!))
        .trim();
    return MarkdownBody(
      key: ValueKey<int>(_messageMarkdownRevision(data, state)),
      data: data,
      inlineSyntaxes: <md.InlineSyntax>[
        MessageSpoilerSyntax(),
        MessageTokenSyntax(),
      ],
      builders: <String, MarkdownElementBuilder>{
        'kaede-spoiler': _SpoilerBuilder(),
        'kaede-user-mention': _MessageTokenBuilder(
          state: state,
          kind: _MessageTokenKind.user,
        ),
        'kaede-role-mention': _MessageTokenBuilder(
          state: state,
          kind: _MessageTokenKind.role,
        ),
        'kaede-channel-token': _MessageTokenBuilder(
          state: state,
          kind: _MessageTokenKind.channel,
        ),
        'kaede-custom-emoji': _MessageTokenBuilder(
          state: state,
          kind: _MessageTokenKind.emoji,
        ),
      },
      selectable: true,
      softLineBreak: true,
      styleSheet: MarkdownStyleSheet(
        p: const TextStyle(
          color: KaedeColors.text,
          fontSize: 16,
          height: 1.24,
        ),
        pPadding: EdgeInsets.zero,
        code: const TextStyle(
          color: KaedeColors.text,
          backgroundColor: KaedeColors.rail,
          fontSize: 14,
        ),
        codeblockDecoration: BoxDecoration(
          color: KaedeColors.rail,
          borderRadius: BorderRadius.circular(6),
          border: Border.all(color: KaedeColors.border),
        ),
        blockquoteDecoration: const BoxDecoration(
          border: Border(
            left: BorderSide(
              color: KaedeColors.muted,
              width: 3,
            ),
          ),
        ),
      ),
      onTapLink: (_, href, __) async {
        final uri = Uri.tryParse(href ?? '');
        if (uri != null && (uri.scheme == 'https' || uri.scheme == 'http')) {
          await launchUrl(uri, mode: LaunchMode.externalApplication);
        }
      },
    );
  }
}

final class ChannelView extends ConsumerStatefulWidget {
  const ChannelView({super.key});

  @override
  ConsumerState<ChannelView> createState() => _ChannelViewState();
}

final class _ChannelViewState extends ConsumerState<ChannelView> {
  final _composer = TextEditingController();
  final _composerFocus = FocusNode();
  final _scroll = ScrollController();
  var _showJumpToPresent = false;
  final _messageKeys = <String, GlobalKey>{};
  final _uploads = <_PendingUpload>[];
  EntityRef? _renderedChannel;
  EntityRef? _renderedLastMessage;
  final _savedOffsets = <EntityRef, double>{};
  var _initialScrollPending = false;
  KaedeMessage? _reply;
  DateTime? _lastTypingSent;
  EntityRef? _composerChannel;
  EntityRef? _pendingComposerChannel;
  Timer? _draftTimer;
  Timer? _slowModeTimer;
  final _slowModeUntil = <EntityRef, DateTime>{};
  var _updatingComposer = false;
  var _notifyReply = true;
  var _sending = false;
  var _automaticHistoryCheckScheduled = false;
  var _automaticHistoryLoadInFlight = false;
  String? _mentionQuery;
  int? _handledJumpGeneration;
  EntityRef? _highlightedMessage;
  Timer? _highlightTimer;

  @override
  void initState() {
    super.initState();
    _composer.addListener(_composerChanged);
    _scroll.addListener(_handleScroll);
  }

  @override
  void dispose() {
    _draftTimer?.cancel();
    _slowModeTimer?.cancel();
    _highlightTimer?.cancel();
    if (_composerChannel case final channel?) {
      ref.read(mobileControllerProvider.notifier).setDraft(
            channel,
            _composer.text,
          );
    }
    for (final upload in _uploads) {
      unawaited(upload.deleteIfTemporary());
    }
    _composer
      ..removeListener(_composerChanged)
      ..dispose();
    _composerFocus.dispose();
    _scroll.removeListener(_handleScroll);
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(mobileControllerProvider);
    final channel = state.activeChannel;
    if (channel == null) {
      return const Center(child: Text('Choose a conversation.'));
    }
    if (channel.type == ChannelType.voice) return VoiceRoom(channel: channel);
    final composerReady = _composerChannel == channel.ref;
    if (!composerReady && _pendingComposerChannel != channel.ref) {
      _pendingComposerChannel = channel.ref;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _switchComposer(channel.ref);
      });
    }
    final moderationStatus = state.activeModerationStatus;
    final encryptedPaused =
        channel.encryptionMode == 'e2ee' && channel.encryptionState != 'active';
    final canSend = !encryptedPaused &&
        moderationStatus == null &&
        (channel.type == ChannelType.dm ||
            channel.allows(Permission.sendMessages));
    final messages = state.messages;
    final pending = state.pendingMessages;
    final jump = state.messageJump;
    final channelChanged = _renderedChannel != channel.ref;
    final lastMessage = messages.isEmpty ? null : messages.last.ref;
    _renderedChannel = channel.ref;
    if (channelChanged) {
      _highlightedMessage = null;
      _showJumpToPresent = false;
      _initialScrollPending = !_savedOffsets.containsKey(channel.ref);
      _scheduleRestore(channel.ref);
    } else if (_initialScrollPending &&
        lastMessage != null &&
        !state.loadingMessages) {
      _initialScrollPending = false;
      _scheduleScrollToBottom();
    } else if (_renderedLastMessage != null &&
        _renderedLastMessage != lastMessage &&
        _isNearBottom) {
      _scheduleScrollToBottom(animated: true);
    }
    _renderedLastMessage = lastMessage;
    if (jump != null &&
        jump.channel == channel.ref &&
        jump.generation != _handledJumpGeneration &&
        !state.loadingMessages) {
      _handledJumpGeneration = jump.generation;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) unawaited(_revealRequestedMessage(jump));
      });
    }
    if (state.channelsWithOlderMessages.contains(channel.ref) &&
        !state.loadingMessages &&
        state.error == null) {
      _scheduleAutomaticHistoryCheck();
    }
    final historySyncWarning = _guildHistorySyncWarning(state.activeGuild);
    return Column(
      children: [
        if (state.activeGuild?.syncStatus == 'quota_paused')
          _FederationStatusStrip(
            title: 'Guild updates are paused on this instance.',
            message: switch (state.activeGuild?.syncErrorCode) {
              'FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED' =>
                'This instance cannot cache another remote account needed by the guild. Contact your instance administrator; you do not need to delete your own messages.',
              'FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED' =>
                'This instance cannot cache another remote server needed by the guild. Contact your instance administrator; you do not need to delete your own messages.',
              _ =>
                'Its remote-guild cache is full, so recent messages or changes may be missing. Contact your instance administrator; you do not need to delete your own messages.',
            },
          ),
        if (historySyncWarning != null &&
            state.activeGuild?.syncStatus != 'quota_paused')
          _FederationStatusStrip(
            title: historySyncWarning.$1,
            message: historySyncWarning.$2,
          ),
        if (moderationStatus != null)
          _FederationStatusStrip(
            title: moderationStatus.timeoutIndefinite
                ? 'You are timed out in this guild.'
                : 'You are timed out until ${_formatTimeout(context, moderationStatus.timeoutUntil!)}.',
            message: moderationStatus.reason?.isNotEmpty == true
                ? 'Reason: ${moderationStatus.reason}'
                : moderationStatus.detailsAvailable
                    ? 'The guild’s home instance did not provide a reason.'
                    : 'Kaede is retrieving the reason from the guild’s home instance.',
          ),
        if (state.error case final error?)
          _ChatErrorStrip(
            message: error,
            onRetry:
                error.startsWith('Older messages are temporarily unavailable')
                    ? () => ref
                        .read(mobileControllerProvider.notifier)
                        .loadMessages(older: true)
                    : ref.read(mobileControllerProvider.notifier).loadMessages,
          ),
        Expanded(
          child: Stack(
            children: [
              Positioned.fill(
                child: messages.isEmpty &&
                        pending.isEmpty &&
                        !state.loadingMessages &&
                        !channel.historyTruncated
                    ? _ConversationStart(channel: channel)
                    : ListView.builder(
                        controller: _scroll,
                        reverse: true,
                        padding: const EdgeInsets.fromLTRB(0, 6, 0, 12),
                        itemCount: messages.length +
                            pending.length +
                            (state.channelsWithOlderMessages
                                        .contains(channel.ref) ||
                                    (channel.historyTruncated &&
                                        messages.isEmpty) ||
                                    (channel.historyTruncated &&
                                        !channel.historyRemoteAvailable)
                                ? 1
                                : 0),
                        itemBuilder: (context, index) {
                          if (index < pending.length) {
                            final item = pending[pending.length - index - 1];
                            return _PendingMessageTile(
                              item: item,
                              onRetry: () => ref
                                  .read(mobileControllerProvider.notifier)
                                  .retrySend(item.nonce),
                              onDiscard: () => ref
                                  .read(mobileControllerProvider.notifier)
                                  .discardSend(item.nonce),
                            );
                          }
                          final messageIndex =
                              messages.length - (index - pending.length) - 1;
                          if (messageIndex < 0) {
                            if (!state.channelsWithOlderMessages
                                .contains(channel.ref)) {
                              return _HistoryBoundary(
                                complete: messages.isEmpty
                                    ? channel.historyRemoteAvailable &&
                                        !state.loadingMessages &&
                                        state.error == null
                                    : messages.first.historyPageComplete,
                              );
                            }
                            return Padding(
                              padding:
                                  const EdgeInsets.fromLTRB(52, 12, 52, 10),
                              child: OutlinedButton.icon(
                                style: OutlinedButton.styleFrom(
                                  minimumSize: const Size(0, 40),
                                  foregroundColor: KaedeColors.textSoft,
                                  side: const BorderSide(
                                      color: KaedeColors.border),
                                  backgroundColor: KaedeColors.panel,
                                  textStyle: const TextStyle(
                                    fontSize: 13,
                                    fontWeight: FontWeight.w600,
                                  ),
                                ),
                                onPressed:
                                    state.loadingMessages ? null : _loadEarlier,
                                icon: state.loadingMessages
                                    ? const SizedBox.square(
                                        dimension: 15,
                                        child: CircularProgressIndicator(
                                            strokeWidth: 2))
                                    : const Icon(Icons.history_rounded,
                                        size: 17),
                                label: Text(state.loadingMessages
                                    ? 'Loading earlier messages…'
                                    : 'Load earlier messages'),
                              ),
                            );
                          }
                          final message = messages[messageIndex];
                          final previous = messageIndex > 0
                              ? messages[messageIndex - 1]
                              : null;
                          final startsNewDay = previous == null ||
                              !sameCalendarDay(
                                previous.createdAt,
                                message.createdAt,
                              );
                          final compact = previous != null &&
                              !startsNewDay &&
                              previous.authorRef == message.authorRef &&
                              previous.reference == null &&
                              message.reference == null &&
                              message.createdAt
                                      .difference(previous.createdAt)
                                      .inMinutes <
                                  7;
                          final key = _messageKeys.putIfAbsent(
                              message.ref.wire, GlobalKey.new);
                          void reply() => setState(() {
                                _reply = message;
                                _notifyReply =
                                    message.authorRef != state.user?.ref &&
                                        channel.type != ChannelType.dm;
                              });
                          final tile = _MessageTile(
                            state: state,
                            message: message,
                            compact: compact,
                            referenced: message.reference == null
                                ? null
                                : messages
                                    .where((candidate) =>
                                        candidate.ref == message.reference)
                                    .firstOrNull,
                            onReply: reply,
                            onJump: message.reference == null
                                ? null
                                : () => _jumpTo(message.reference!),
                            onMenu: () => _showMessageActions(message),
                            onReaction: (emoji) =>
                                _toggleReaction(message, emoji),
                            onAddReaction: message.reactionCounts.isEmpty
                                ? null
                                : () => _addReactionFromPicker(message),
                            onAuthorTap: message.author == null
                                ? null
                                : () => showUserProfile(
                                      context,
                                      message.author!,
                                      ref
                                          .read(
                                              mobileControllerProvider.notifier)
                                          .presenceFor(message.author!),
                                    ),
                          );
                          return KeyedSubtree(
                            key: key,
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.stretch,
                              children: [
                                if (startsNewDay)
                                  _DayDivider(day: message.createdAt),
                                AnimatedContainer(
                                  duration: const Duration(milliseconds: 240),
                                  curve: Curves.easeOut,
                                  color: _highlightedMessage == message.ref
                                      ? KaedeColors.coral.withValues(alpha: .13)
                                      : Colors.transparent,
                                  child: _SwipeToReply(
                                    enabled:
                                        canSend && message.deletedAt == null,
                                    onReply: reply,
                                    child: tile,
                                  ),
                                ),
                              ],
                            ),
                          );
                        },
                      ),
              ),
              Positioned(
                right: 12,
                bottom: 10,
                child: _JumpToPresentButton(
                  visible: _showJumpToPresent,
                  unread: state.unreadCounts[channel.ref] ?? 0,
                  onTap: () {
                    setState(() => _showJumpToPresent = false);
                    _scheduleScrollToBottom(animated: true);
                  },
                ),
              ),
            ],
          ),
        ),
        if (state.typingByChannel[channel.ref] case final typing?
            when typing.isNotEmpty)
          _TypingIndicator(participants: typing),
        if (_mentionQuery != null)
          _MentionSuggestions(
            users: _mentionCandidates(state, _mentionQuery!),
            onSelected: _insertMention,
          ),
        if (!canSend)
          _PermissionNotice(
            message: encryptedPaused
                ? 'Encrypted messaging is paused while participant device keys are secured. No plaintext will be sent.'
                : moderationStatus == null
                    ? 'You do not have permission to send messages here.'
                    : 'You cannot send messages while timed out.',
          )
        else
          IgnorePointer(
            ignoring: !composerReady,
            child: _Composer(
              controller: _composer,
              focusNode: _composerFocus,
              hint: composerHint(channel),
              reply: _reply,
              notifyReply: _notifyReply,
              uploads: _uploads,
              sending: _sending,
              slowModeRemaining: _slowModeRemaining(channel.ref),
              compact: MediaQuery.sizeOf(context).width <= 360,
              canAttach: channel.type == ChannelType.dm ||
                  channel.allows(Permission.attachFiles),
              gifsAllowed: composerAllowsGifs(channel),
              onNotifyChanged: (value) => setState(() => _notifyReply = value),
              onCancelReply: () => setState(() => _reply = null),
              onRemoveUpload: (item) {
                setState(() => _uploads.remove(item));
                unawaited(item.deleteIfTemporary());
              },
              onMore: () => _showComposerActions(channel),
              onAttach: () =>
                  _runComposerAction(channel, ComposerAction.attach),
              onEmoji: () => _runComposerAction(channel, ComposerAction.emoji),
              onGif: () => _runComposerAction(channel, ComposerAction.gif),
              onSend: _send,
            ),
          ),
      ],
    );
  }

  String _formatTimeout(BuildContext context, DateTime value) {
    final local = value.toLocal();
    final material = MaterialLocalizations.of(context);
    return '${material.formatMediumDate(local)} at '
        '${material.formatTimeOfDay(TimeOfDay.fromDateTime(local))}';
  }

  bool get _isNearBottom =>
      !_scroll.hasClients ||
      _scroll.offset - _scroll.position.minScrollExtent < 160;

  void _composerChanged() {
    if (_updatingComposer) return;
    final selection = _composer.selection;
    final beforeCursor = selection.isValid
        ? _composer.text.substring(0, selection.extentOffset)
        : _composer.text;
    final match = RegExp(r'(?:^|\s)@([^\s@<>]*)$').firstMatch(beforeCursor);
    final nextMention = match?.group(1);
    if (_mentionQuery != nextMention && mounted) {
      setState(() => _mentionQuery = nextMention);
    }
    final channel = _composerChannel;
    if (channel == null) return;
    _draftTimer?.cancel();
    _draftTimer = Timer(const Duration(milliseconds: 300), () {
      if (!mounted || _composerChannel != channel) return;
      ref.read(mobileControllerProvider.notifier).setDraft(
            channel,
            _composer.text,
          );
    });
    _publishTyping(channel);
  }

  List<KaedeUser> _mentionCandidates(MobileState state, String query) {
    final users = <EntityRef, KaedeUser>{...state.userProfiles};
    if (state.user case final user?) users[user.ref] = user;
    for (final dm in state.dms) {
      for (final user in dm.recipients) {
        users[user.ref] = user;
      }
    }
    for (final messages in state.messageStore.values) {
      for (final message in messages) {
        if (message.author case final user?) users[user.ref] = user;
      }
    }
    final needle = query.toLowerCase();
    return users.values
        .where((user) => user.profileResolved)
        .where((user) =>
            user.name.toLowerCase().contains(needle) ||
            user.username.toLowerCase().contains(needle) ||
            user.handle.toLowerCase().contains(needle))
        .take(6)
        .toList(growable: false);
  }

  void _insertMention(KaedeUser user) {
    final selection = _composer.selection;
    if (!selection.isValid) return;
    final before = _composer.text.substring(0, selection.extentOffset);
    final match = RegExp(r'(?:^|\s)@([^\s@<>]*)$').firstMatch(before);
    if (match == null) return;
    final at = before.lastIndexOf('@', selection.extentOffset - 1);
    final replacement = '<@${user.ref.wire}> ';
    _composer.value = TextEditingValue(
      text:
          _composer.text.replaceRange(at, selection.extentOffset, replacement),
      selection: TextSelection.collapsed(offset: at + replacement.length),
    );
    setState(() => _mentionQuery = null);
  }

  void _publishTyping(EntityRef channel) {
    final text = _composer.text.trim();
    if (text.isEmpty) return;
    final now = DateTime.now();
    if (_lastTypingSent case final last?
        when now.difference(last) < const Duration(seconds: 8)) {
      return;
    }
    _lastTypingSent = now;
    unawaited(
      ref.read(mobileControllerProvider.notifier).publishTyping(channel),
    );
  }

  void _switchComposer(EntityRef channel) {
    final previous = _composerChannel;
    if (previous != null) {
      ref.read(mobileControllerProvider.notifier).setDraft(
            previous,
            _composer.text,
          );
    }
    _draftTimer?.cancel();
    final draft = ref.read(mobileControllerProvider).drafts[channel] ?? '';
    _updatingComposer = true;
    _composer.value = TextEditingValue(
      text: draft,
      selection: TextSelection.collapsed(offset: draft.length),
    );
    _updatingComposer = false;
    setState(() {
      _composerChannel = channel;
      _pendingComposerChannel = null;
      _reply = null;
      _lastTypingSent = null;
    });
  }

  void _rememberOffset() {
    final channel = _renderedChannel;
    if (channel != null && _scroll.hasClients) {
      _savedOffsets[channel] = _scroll.offset;
    }
  }

  void _handleScroll() {
    _rememberOffset();
    _maybeAutomaticallyLoadEarlier();
    // The reversed list starts at the newest message, so being scrolled away
    // from the minimum extent means older history is on screen.
    final scrolledBack = _scroll.hasClients &&
        _scroll.offset - _scroll.position.minScrollExtent > 420;
    if (scrolledBack != _showJumpToPresent && mounted) {
      setState(() => _showJumpToPresent = scrolledBack);
    }
  }

  void _scheduleAutomaticHistoryCheck() {
    if (_automaticHistoryCheckScheduled) return;
    _automaticHistoryCheckScheduled = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _automaticHistoryCheckScheduled = false;
      if (mounted) _maybeAutomaticallyLoadEarlier();
    });
  }

  void _maybeAutomaticallyLoadEarlier() {
    if (!_scroll.hasClients || _automaticHistoryLoadInFlight) return;
    final state = ref.read(mobileControllerProvider);
    final channel = state.activeChannel;
    if (channel == null ||
        !shouldAutomaticallyLoadEarlier(
          pixels: _scroll.position.pixels,
          maxScrollExtent: _scroll.position.maxScrollExtent,
          hasEarlier: state.channelsWithOlderMessages.contains(channel.ref),
          loading: state.loadingMessages,
          hasError: state.error != null,
        )) {
      return;
    }
    _automaticHistoryLoadInFlight = true;
    unawaited(() async {
      try {
        await _loadEarlier();
      } finally {
        _automaticHistoryLoadInFlight = false;
        if (mounted) _scheduleAutomaticHistoryCheck();
      }
    }());
  }

  void _scheduleRestore(EntityRef channel) {
    final offset = _savedOffsets[channel];
    if (offset == null) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || _renderedChannel != channel || !_scroll.hasClients) {
        return;
      }
      _scroll.jumpTo(offset.clamp(0.0, _scroll.position.maxScrollExtent));
    });
  }

  void _scheduleScrollToBottom({bool animated = false}) {
    final channel = _renderedChannel;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || channel != _renderedChannel || !_scroll.hasClients) {
        return;
      }
      final target = _scroll.position.minScrollExtent;
      if (animated) {
        unawaited(_scroll.animateTo(target,
            duration: const Duration(milliseconds: 180),
            curve: Curves.easeOutCubic));
      } else {
        _scroll.jumpTo(target);
      }
    });
  }

  Future<void> _loadEarlier() async {
    await ref.read(mobileControllerProvider.notifier).loadMessages(older: true);
    // A reversed list appends older rows at its far edge, so Flutter retains
    // the visible content and offset without a compensating jump.
  }

  Future<void> _jumpTo(EntityRef reference) async {
    await ref.read(mobileControllerProvider.notifier).jumpToMessage(
          reference,
          expectedChannel: _renderedChannel,
        );
  }

  Future<void> _revealRequestedMessage(MessageJumpRequest request) async {
    if (!mounted ||
        !messageJumpRevealIsCurrent(
          request: request,
          renderedChannel: _renderedChannel,
          handledGeneration: _handledJumpGeneration,
        )) {
      return;
    }
    ref
        .read(mobileControllerProvider.notifier)
        .consumeMessageJump(request.generation);
    // The reversed lazy list may not have built an around-page's middle row.
    // Start with a proportional estimate, then walk by viewports until the
    // requested row has a BuildContext that ensureVisible can target exactly.
    for (var attempt = 0; attempt < 60; attempt++) {
      final targetContext = _messageKeys[request.message.wire]?.currentContext;
      if (targetContext != null && targetContext.mounted) {
        await Scrollable.ensureVisible(
          targetContext,
          duration: const Duration(milliseconds: 320),
          alignment: .35,
        );
        if (!mounted ||
            !messageJumpRevealIsCurrent(
              request: request,
              renderedChannel: _renderedChannel,
              handledGeneration: _handledJumpGeneration,
            )) {
          return;
        }
        _highlightTimer?.cancel();
        setState(() => _highlightedMessage = request.message);
        _highlightTimer = Timer(const Duration(seconds: 2), () {
          if (mounted && _highlightedMessage == request.message) {
            setState(() => _highlightedMessage = null);
          }
        });
        return;
      }
      if (!_scroll.hasClients) {
        await WidgetsBinding.instance.endOfFrame;
        continue;
      }
      final state = ref.read(mobileControllerProvider);
      final targetIndex = state.messages.indexWhere(
        (message) => message.ref == request.message,
      );
      if (targetIndex < 0) return;
      final targetItem = messageListItemIndex(
        messageCount: state.messages.length,
        messageIndex: targetIndex,
        pendingCount: state.pendingMessages.length,
      );
      final builtItems = <int>[];
      for (var index = 0; index < state.messages.length; index++) {
        final message = state.messages[index];
        if (_messageKeys[message.ref.wire]?.currentContext != null) {
          builtItems.add(messageListItemIndex(
            messageCount: state.messages.length,
            messageIndex: index,
            pendingCount: state.pendingMessages.length,
          ));
        }
      }
      final position = _scroll.position;
      double next;
      if (builtItems.isEmpty || attempt == 0) {
        final totalItems = state.messages.length + state.pendingMessages.length;
        next = totalItems <= 1
            ? position.minScrollExtent
            : position.maxScrollExtent * targetItem / (totalItems - 1);
      } else if (targetItem > builtItems.reduce((a, b) => a > b ? a : b)) {
        next = position.pixels + position.viewportDimension * .8;
      } else {
        next = position.pixels - position.viewportDimension * .8;
      }
      _scroll.jumpTo(
        next
            .clamp(position.minScrollExtent, position.maxScrollExtent)
            .toDouble(),
      );
      await WidgetsBinding.instance.endOfFrame;
      if (!mounted ||
          !messageJumpRevealIsCurrent(
            request: request,
            renderedChannel: _renderedChannel,
            handledGeneration: _handledJumpGeneration,
          )) {
        return;
      }
    }
  }

  Future<void> _showComposerActions(KaedeChannel channel) async {
    if (_sending || _composerChannel != channel.ref) return;
    final canAttach = channel.type == ChannelType.dm ||
        channel.allows(Permission.attachFiles);
    _composerFocus.unfocus();
    final action = await showComposerActionPicker(
      context,
      canAttach: canAttach,
      gifsAllowed: composerAllowsGifs(channel),
    );
    if (!mounted || action == null) return;
    await _runComposerAction(channel, action);
  }

  Future<void> _runComposerAction(
    KaedeChannel channel,
    ComposerAction action,
  ) async {
    if (_sending || _composerChannel != channel.ref) return;
    if (ref.read(mobileControllerProvider).activeChannel?.ref != channel.ref) {
      return;
    }
    switch (action) {
      case ComposerAction.attach:
        if (channel.type != ChannelType.dm &&
            !channel.allows(Permission.attachFiles)) {
          ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
            content: Text('You cannot attach files in this channel.'),
          ));
          return;
        }
        await _pickFiles();
        break;
      case ComposerAction.emoji:
        final state = ref.read(mobileControllerProvider);
        final recent = await _recentReactions(state.user?.ref);
        if (!mounted ||
            ref.read(mobileControllerProvider).activeChannel?.ref !=
                channel.ref) {
          return;
        }
        final emoji = await showComposerEmojiPicker(
          context,
          repository: ref.read(mobileControllerProvider.notifier).repository,
          channel: channel,
          categories: _reactionEmojiCategories,
          recent: recent,
        );
        if (!mounted || emoji == null) return;
        if (ref.read(mobileControllerProvider).activeChannel?.ref !=
            channel.ref) {
          return;
        }
        _insertComposerText(emoji);
        break;
      case ComposerAction.gif:
        if (!composerAllowsGifs(channel)) {
          _showGifUnavailable();
          return;
        }
        final gif = await showComposerGifPicker(
          context,
          repository: ref.read(mobileControllerProvider.notifier).repository,
        );
        if (!mounted || gif == null) return;
        await _sendGif(channel, gif);
        break;
    }
  }

  void _insertComposerText(String insertion) {
    final next = insertComposerText(_composer.value, insertion);
    if (next == null) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('Messages can contain at most 4,000 characters.'),
      ));
      return;
    }
    _composer.value = next;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) _composerFocus.requestFocus();
    });
  }

  void _showGifUnavailable() {
    ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
      content: Text(
        'GIF search is unavailable in end-to-end encrypted conversations.',
      ),
    ));
  }

  Future<void> _sendGif(KaedeChannel channel, ComposerGif gif) async {
    final active = ref.read(mobileControllerProvider).activeChannel;
    if (_sending ||
        active?.ref != channel.ref ||
        _composerChannel != channel.ref) {
      return;
    }
    if (!composerAllowsGifs(active!)) {
      _showGifUnavailable();
      return;
    }
    final remaining = _slowModeRemaining(channel.ref);
    if (remaining > Duration.zero) {
      final seconds = (remaining.inMilliseconds / 1000).ceil();
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text('Slow mode is active. Try again in $seconds seconds.'),
      ));
      return;
    }
    setState(() => _sending = true);
    try {
      await ref
          .read(mobileControllerProvider.notifier)
          .send(channel.ref, gif.url.toString());
      if (channel.slowModeSeconds > 0) {
        _startSlowMode(
          channel.ref,
          Duration(seconds: channel.slowModeSeconds),
        );
      }
      await WidgetsBinding.instance.endOfFrame;
      if (_composerChannel == channel.ref && _scroll.hasClients) {
        await _scroll.animateTo(
          _scroll.position.minScrollExtent,
          duration: const Duration(milliseconds: 250),
          curve: Curves.easeOut,
        );
      }
    } on Object catch (error) {
      if (error is KaedeException && error.retryAfter != null) {
        _startSlowMode(channel.ref, error.retryAfter!);
      }
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(userFacingError(
            error,
            summary: 'Could not send that GIF',
          )),
        ));
      }
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  Future<void> _pickFiles() async {
    final result = await FilePicker.platform.pickFiles(
      allowMultiple: true,
      withData: false,
      withReadStream: true,
    );
    if (result == null || !mounted) return;
    final additions = <_PendingUpload>[];
    for (final file in result.files.take(10 - _uploads.length)) {
      File source;
      var temporary = false;
      if (file.path case final path?) {
        source = File(path);
      } else if (file.readStream case final stream?) {
        final directory = await getTemporaryDirectory();
        source = File(
          '${directory.path}/kaede-upload-${DateTime.now().microsecondsSinceEpoch}-${_safeName(file.name)}',
        );
        final sink = source.openWrite();
        await sink.addStream(stream);
        await sink.close();
        temporary = true;
      } else {
        continue;
      }
      additions.add(_PendingUpload(
        name: file.name,
        file: source,
        size: await source.length(),
        contentType: _contentType(file.name),
        temporary: temporary,
      ));
    }
    setState(() => _uploads.addAll(additions));
  }

  Future<void> _send() async {
    if (_sending) return;
    final state = ref.read(mobileControllerProvider);
    final channel = state.activeChannel;
    if (channel == null || _composerChannel != channel.ref) return;
    if (_slowModeRemaining(channel.ref) > Duration.zero) return;
    unawaited(HapticFeedback.lightImpact());
    final content = _composer.text;
    final reply = _reply;
    final notifyReply = _notifyReply;
    final pendingUploads = List<_PendingUpload>.of(_uploads);
    setState(() => _sending = true);
    try {
      final controller = ref.read(mobileControllerProvider.notifier);
      final uploaded = <EntityRef>[];
      final encryptedAttachments = <Map<String, Object?>>[];
      for (final item in pendingUploads) {
        if (channel.encryptionMode == 'e2ee') {
          final encrypted = await uploadEncryptedFile(
            repository: controller.repository,
            channel: channel.ref,
            source: item.file,
            filename: item.name,
            contentType: item.contentType,
          );
          uploaded.add(encrypted.attachment);
          encryptedAttachments.add(encrypted.manifest);
        } else {
          uploaded.add(await controller.repository.uploadAttachmentFile(
            channel: channel.ref,
            filename: item.name,
            contentType: item.contentType,
            file: item.file,
          ));
        }
      }
      await controller.send(
        channel.ref,
        content,
        attachments: uploaded,
        encryptedAttachments: encryptedAttachments,
        mentionUsers: mentionReferences(content),
        replyTo: reply?.ref,
        replyAuthor: reply?.authorRef,
        notify: notifyReply,
      );
      controller.setDraft(channel.ref, '');
      if (_composerChannel == channel.ref) {
        _updatingComposer = true;
        _composer.clear();
        _updatingComposer = false;
      }
      setState(() {
        _uploads.removeWhere(pendingUploads.contains);
        if (_composerChannel == channel.ref && _reply == reply) _reply = null;
      });
      for (final upload in pendingUploads) {
        unawaited(upload.deleteIfTemporary());
      }
      if (channel.slowModeSeconds > 0) {
        _startSlowMode(
          channel.ref,
          Duration(seconds: channel.slowModeSeconds),
        );
      }
      await WidgetsBinding.instance.endOfFrame;
      if (_composerChannel == channel.ref && _scroll.hasClients) {
        await _scroll.animateTo(_scroll.position.minScrollExtent,
            duration: const Duration(milliseconds: 250), curve: Curves.easeOut);
      }
    } on Object catch (error) {
      if (error is KaedeException && error.retryAfter != null) {
        _startSlowMode(channel.ref, error.retryAfter!);
      }
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(userFacingError(
            error,
            summary: 'Could not send the message',
          )),
        ));
      }
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  Duration _slowModeRemaining(EntityRef channel) {
    final deadline = _slowModeUntil[channel];
    if (deadline == null) return Duration.zero;
    final remaining = deadline.difference(DateTime.now());
    return remaining.isNegative ? Duration.zero : remaining;
  }

  void _startSlowMode(EntityRef channel, Duration duration) {
    if (duration <= Duration.zero || !mounted) return;
    _slowModeUntil[channel] = DateTime.now().add(duration);
    _slowModeTimer?.cancel();
    _slowModeTimer = Timer.periodic(const Duration(milliseconds: 250), (timer) {
      if (!mounted) {
        timer.cancel();
        return;
      }
      final now = DateTime.now();
      _slowModeUntil.removeWhere((_, deadline) => !deadline.isAfter(now));
      setState(() {});
      if (_slowModeUntil.isEmpty) {
        timer.cancel();
        _slowModeTimer = null;
      }
    });
    setState(() {});
  }

  String _reactionHistoryKey(EntityRef? user) =>
      'message-reaction-history:${user?.wire ?? 'anonymous'}';

  Future<List<String>> _recentReactions(EntityRef? user) async {
    final preferences = await SharedPreferences.getInstance();
    return rankRecentReactions(
      preferences.getStringList(_reactionHistoryKey(user)) ?? const <String>[],
    );
  }

  Future<void> _rememberReaction(EntityRef? user, String emoji) async {
    final preferences = await SharedPreferences.getInstance();
    final key = _reactionHistoryKey(user);
    final history = preferences.getStringList(key) ?? <String>[];
    history.add(emoji);
    if (history.length > 100) history.removeRange(0, history.length - 100);
    await preferences.setStringList(key, history);
  }

  Future<void> _toggleReaction(KaedeMessage message, String emoji) async {
    final state = ref.read(mobileControllerProvider);
    final channel = state.activeChannel;
    if (channel == null) return;
    final removing = message.reactedEmoji.contains(emoji);
    final canAdd = channel.type == ChannelType.dm ||
        channel.allows(Permission.addReactions);
    if (!removing && !canAdd) return;
    unawaited(HapticFeedback.selectionClick());
    final controller = ref.read(mobileControllerProvider.notifier);
    try {
      if (removing) {
        await controller.repository
            .removeReaction(message.channelRef, message.ref, emoji);
      } else {
        await controller.repository
            .react(message.channelRef, message.ref, emoji);
        await _rememberReaction(state.user?.ref, emoji);
      }
    } on Object catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(
            userFacingError(error, summary: 'Could not update that reaction')),
      ));
    }
  }

  Future<void> _addReactionFromPicker(KaedeMessage message) async {
    final emoji =
        await _showReactionPicker(ref.read(mobileControllerProvider).user?.ref);
    if (!mounted || emoji == null) return;
    await _toggleReaction(message, emoji);
  }

  Future<String?> _showReactionPicker(EntityRef? user) async {
    final recent = await _recentReactions(user);
    if (!mounted) return null;
    var category = 'Recent';
    var query = '';
    return showModalBottomSheet<String>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (context) => StatefulBuilder(
        builder: (context, setPickerState) {
          final all = _reactionEmojiCategories.values.expand((items) => items);
          final items = query.isNotEmpty
              ? all.toSet().where((emoji) => emoji.contains(query)).toList()
              : category == 'Recent'
                  ? recent
                  : _reactionEmojiCategories[category]!;
          return SafeArea(
            child: ConstrainedBox(
              constraints: BoxConstraints(
                maxHeight: min(MediaQuery.sizeOf(context).height * .62, 430),
              ),
              child: Padding(
                padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
                child: Column(
                  children: [
                    TextField(
                      autofocus: true,
                      decoration: const InputDecoration(
                        hintText: 'Search emoji',
                        prefixIcon: Icon(Icons.search_rounded),
                        isDense: true,
                      ),
                      onChanged: (value) =>
                          setPickerState(() => query = value.trim()),
                    ),
                    const SizedBox(height: 8),
                    SizedBox(
                      height: 38,
                      child: ListView.separated(
                        scrollDirection: Axis.horizontal,
                        itemCount: _reactionEmojiCategories.length,
                        separatorBuilder: (_, __) => const SizedBox(width: 6),
                        itemBuilder: (context, index) {
                          final name =
                              _reactionEmojiCategories.keys.elementAt(index);
                          return ChoiceChip(
                            label: Text(name),
                            selected: category == name && query.isEmpty,
                            onSelected: (_) => setPickerState(() {
                              category = name;
                              query = '';
                            }),
                          );
                        },
                      ),
                    ),
                    const SizedBox(height: 8),
                    Expanded(
                      child: GridView.builder(
                        gridDelegate:
                            const SliverGridDelegateWithFixedCrossAxisCount(
                          crossAxisCount: 7,
                          mainAxisSpacing: 4,
                          crossAxisSpacing: 4,
                        ),
                        itemCount: items.length,
                        itemBuilder: (context, index) => InkWell(
                          onTap: () => Navigator.pop(context, items[index]),
                          borderRadius: BorderRadius.circular(10),
                          child: Center(
                              child: Text(items[index],
                                  style: const TextStyle(fontSize: 25))),
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          );
        },
      ),
    );
  }

  Future<void> _showMessageActions(KaedeMessage message) async {
    if (message.deletedAt != null) return;
    final me = ref.read(mobileControllerProvider).user?.ref;
    final channel = ref.read(mobileControllerProvider).activeChannel!;
    final canReact = channel.type == ChannelType.dm ||
        channel.allows(Permission.addReactions);
    final canManage = channel.type == ChannelType.dm ||
        channel.allows(Permission.manageMessages);
    final recent = canReact ? await _recentReactions(me) : const <String>[];
    if (!mounted) return;
    final action = await showModalBottomSheet<String>(
      context: context,
      showDragHandle: true,
      builder: (context) => SafeArea(
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              _MessageActionsHeader(message: message),
              if (canReact && recent.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.fromLTRB(12, 4, 12, 8),
                  child: Row(
                    children: [
                      for (final emoji in recent)
                        Expanded(
                          child: Padding(
                            padding: const EdgeInsets.symmetric(horizontal: 3),
                            child: Material(
                              color: KaedeColors.raised,
                              borderRadius:
                                  BorderRadius.circular(KaedeRadius.medium),
                              child: InkWell(
                                borderRadius:
                                    BorderRadius.circular(KaedeRadius.medium),
                                onTap: () =>
                                    Navigator.pop(context, 'reaction:$emoji'),
                                child: Container(
                                  height: 46,
                                  alignment: Alignment.center,
                                  decoration: BoxDecoration(
                                    borderRadius: BorderRadius.circular(
                                        KaedeRadius.medium),
                                    border:
                                        Border.all(color: KaedeColors.border),
                                  ),
                                  child: Text(emoji,
                                      style: const TextStyle(fontSize: 22)),
                                ),
                              ),
                            ),
                          ),
                        ),
                      Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 3),
                        child: IconButton(
                          tooltip: 'More emoji',
                          onPressed: () =>
                              Navigator.pop(context, 'react-picker'),
                          icon: const Icon(Icons.add_reaction_outlined),
                        ),
                      ),
                    ],
                  ),
                ),
              ListTile(
                  leading: const Icon(Icons.reply_rounded),
                  title: const Text('Reply'),
                  onTap: () => Navigator.pop(context, 'reply')),
              if (message.content?.isNotEmpty == true)
                ListTile(
                    leading: const Icon(Icons.copy_rounded),
                    title: const Text('Copy text'),
                    onTap: () => Navigator.pop(context, 'copy')),
              ListTile(
                  leading: const Icon(Icons.link_rounded),
                  title: const Text('Copy message link'),
                  onTap: () => Navigator.pop(context, 'copy-link')),
              if (canReact)
                ListTile(
                    leading: const Icon(Icons.add_reaction_outlined),
                    title: const Text('Add reaction'),
                    trailing: const Icon(Icons.chevron_right_rounded),
                    onTap: () => Navigator.pop(context, 'react-picker')),
              if (message.reactionCounts.isNotEmpty)
                ListTile(
                    leading: const Icon(Icons.people_outline_rounded),
                    title: const Text('View reactions'),
                    trailing: const Icon(Icons.chevron_right_rounded),
                    onTap: () => Navigator.pop(context, 'view-reactions')),
              if (canManage)
                ListTile(
                    leading: Icon(message.pinned
                        ? Icons.push_pin_rounded
                        : Icons.push_pin_outlined),
                    title:
                        Text(message.pinned ? 'Unpin message' : 'Pin message'),
                    onTap: () => Navigator.pop(context, 'pin')),
              if (message.authorRef == me &&
                  (message.e2ee == null || message.content != null))
                ListTile(
                    leading: const Icon(Icons.edit_outlined),
                    title: const Text('Edit message'),
                    onTap: () => Navigator.pop(context, 'edit')),
              if (message.authorRef != me)
                ListTile(
                  leading: const Icon(Icons.flag_outlined),
                  title: const Text('Report message'),
                  onTap: () => Navigator.pop(context, 'report'),
                ),
              if (message.authorRef == me || canManage)
                ListTile(
                  leading: const Icon(Icons.delete_outline_rounded,
                      color: KaedeColors.danger),
                  title: const Text('Delete message',
                      style: TextStyle(color: KaedeColors.danger)),
                  onTap: () => Navigator.pop(context, 'delete'),
                ),
              const SizedBox(height: 6),
            ],
          ),
        ),
      ),
    );
    if (!mounted || action == null) return;
    if (action.startsWith('reaction:')) {
      await _toggleReaction(message, action.substring('reaction:'.length));
      return;
    }
    final controller = ref.read(mobileControllerProvider.notifier);
    try {
      switch (action) {
        case 'reply':
          setState(() {
            _reply = message;
            _notifyReply =
                message.authorRef != me && channel.type != ChannelType.dm;
          });
          break;
        case 'copy':
          await Clipboard.setData(ClipboardData(text: message.content ?? ''));
          break;
        case 'copy-link':
          final instance = controller.api.tokens?.instance.value;
          if (instance != null) {
            await Clipboard.setData(ClipboardData(
              text: messageLink(
                instance: instance,
                channel: channel,
                message: message.ref,
              ),
            ));
            if (mounted) {
              ScaffoldMessenger.of(context).showSnackBar(
                const SnackBar(content: Text('Message link copied.')),
              );
            }
          }
          break;
        case 'react-picker':
          final emoji = await _showReactionPicker(me);
          if (emoji != null) await _toggleReaction(message, emoji);
          break;
        case 'view-reactions':
          await showModalBottomSheet<void>(
            context: context,
            isScrollControlled: true,
            showDragHandle: true,
            builder: (context) => _ReactionViewerSheet(
              message: message,
              repository: controller.repository,
            ),
          );
          break;
        case 'pin':
          await controller.setMessagePinned(message, !message.pinned);
          break;
        case 'edit':
          final edited = await _editDialog(message.content ?? '');
          if (edited != null) await controller.replaceMessage(message, edited);
          break;
        case 'delete':
          await controller.removeMessage(message);
          break;
        case 'report':
          await _reportMessageDialog(message);
          break;
      }
    } on Object catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(userFacingError(
            error,
            summary: 'Could not complete that message action',
          )),
        ),
      );
    }
  }

  Future<String?> _editDialog(String original) {
    final input = TextEditingController(text: original);
    return showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Edit message'),
        content: TextField(
            controller: input, autofocus: true, minLines: 2, maxLines: 8),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('Cancel')),
          FilledButton(
              onPressed: () => Navigator.pop(context, input.text.trim()),
              child: const Text('Save')),
        ],
      ),
    );
  }

  Future<void> _reportMessageDialog(KaedeMessage message) async {
    const categories = <String, String>{
      'spam': 'Spam',
      'harassment': 'Harassment',
      'hate': 'Hate',
      'sexual_content': 'Sexual content',
      'violence': 'Violence',
      'self_harm': 'Self-harm',
      'impersonation': 'Impersonation',
      'privacy': 'Privacy',
      'malware': 'Malware',
      'illegal_content': 'Illegal content',
      'other': 'Other',
    };
    var category = 'spam';
    var disclose = false;
    var submitting = false;
    final encryptedEvidenceAvailable =
        encryptedReportEvidenceAvailable(message);
    final description = TextEditingController();
    try {
      await showDialog<void>(
        context: context,
        barrierDismissible: !submitting,
        builder: (dialogContext) => StatefulBuilder(
          builder: (context, setDialogState) => AlertDialog(
            title: const Text('Report message'),
            content: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (message.e2ee != null) ...[
                    Text(
                      encryptedEvidenceAvailable
                          ? 'This message is end-to-end encrypted. Reporting shares the decrypted message evidence shown on this device with this instance’s Trust & Safety team. Attachment-only messages have empty disclosed text and can still be reported. Encryption keys, decrypted file contents, and other messages are not sent.'
                          : 'This encrypted message has not decrypted on this device. Wait for its authenticated message evidence to decrypt, then try again.',
                    ),
                    const SizedBox(height: 12),
                    CheckboxListTile(
                      contentPadding: EdgeInsets.zero,
                      value: disclose,
                      onChanged: submitting || !encryptedEvidenceAvailable
                          ? null
                          : (value) =>
                              setDialogState(() => disclose = value == true),
                      title: const Text(
                        'I understand the decrypted message evidence will be disclosed.',
                      ),
                    ),
                  ] else
                    const Text(
                      'The message text and basic context will be sent to this instance’s Trust & Safety team.',
                    ),
                  const SizedBox(height: 12),
                  DropdownButtonFormField<String>(
                    initialValue: category,
                    decoration: const InputDecoration(labelText: 'Reason'),
                    items: categories.entries
                        .map((entry) => DropdownMenuItem(
                              value: entry.key,
                              child: Text(entry.value),
                            ))
                        .toList(growable: false),
                    onChanged: submitting
                        ? null
                        : (value) => setDialogState(
                              () => category = value ?? category,
                            ),
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: description,
                    enabled: !submitting,
                    maxLength: 2000,
                    maxLines: 4,
                    decoration: const InputDecoration(
                      labelText: 'Additional details (optional)',
                    ),
                  ),
                ],
              ),
            ),
            actions: [
              TextButton(
                onPressed:
                    submitting ? null : () => Navigator.pop(dialogContext),
                child: const Text('Cancel'),
              ),
              FilledButton(
                onPressed: submitting ||
                        !canSubmitMessageReport(
                          message,
                          disclosureAcknowledged: disclose,
                        )
                    ? null
                    : () async {
                        setDialogState(() => submitting = true);
                        try {
                          await ref
                              .read(mobileControllerProvider.notifier)
                              .repository
                              .reportMessage(
                                message.ref,
                                category: category,
                                description: description.text,
                                disclosedContent: message.e2ee == null
                                    ? null
                                    : message.content,
                                disclosureAcknowledged: disclose,
                              );
                          if (dialogContext.mounted) {
                            Navigator.pop(dialogContext);
                          }
                          if (mounted) {
                            ScaffoldMessenger.of(this.context).showSnackBar(
                              const SnackBar(
                                  content: Text('Report submitted.')),
                            );
                          }
                        } on Object catch (error) {
                          if (dialogContext.mounted) {
                            setDialogState(() => submitting = false);
                            ScaffoldMessenger.of(dialogContext).showSnackBar(
                              SnackBar(
                                content: Text(userFacingError(
                                  error,
                                  summary: 'Could not submit the report',
                                )),
                              ),
                            );
                          }
                        }
                      },
                child: Text(submitting ? 'Submitting…' : 'Submit report'),
              ),
            ],
          ),
        ),
      );
    } finally {
      description.dispose();
    }
  }
}

(String, String)? _guildHistorySyncWarning(KaedeGuild? guild) {
  if (guild?.historySyncStatus == 'retrying') {
    final milliseconds = guild!.historySyncRetryAfterMs ?? 2000;
    final seconds = max(1, (milliseconds + 999) ~/ 1000);
    final title = guild.historySyncErrorCode == 'KAED_FED_HISTORY_CAPACITY'
        ? 'Older guild history is waiting for capacity.'
        : 'Older guild history is temporarily delayed.';
    return (
      title,
      'Recent messages remain available. Kaede will retry automatically in about $seconds second${seconds == 1 ? '' : 's'}; no action is needed.',
    );
  }
  if (guild?.historySyncStatus != 'failed') return null;
  if (guild?.historySyncErrorCode == 'FEDERATED_GUILD_HISTORY_LIMIT_REACHED') {
    return (
      'Older guild history stopped at this instance’s safety limit.',
      'Recent and new messages still work. Ask your instance administrator to raise the federation history limit if more retained history is needed.',
    );
  }
  if (guild?.historySyncErrorCode == 'FEDERATED_GUILD_HISTORY_REJECTED') {
    return (
      'Older guild history could not be safely imported.',
      'The remote instance returned history Kaede could not accept. Recent and new messages still work; contact your instance administrator if older history is required.',
    );
  }
  return (
    'Older guild history could not be imported.',
    'Recent and new messages still work. Contact your instance administrator if older history remains unavailable.',
  );
}

/// Whether two instants fall on the same local calendar day.
bool sameCalendarDay(DateTime left, DateTime right) {
  final first = left.toLocal();
  final second = right.toLocal();
  return first.year == second.year &&
      first.month == second.month &&
      first.day == second.day;
}

/// "Today", "Yesterday", a weekday for the past week, then a full date.
String transcriptDayLabel(DateTime value, {DateTime? now}) {
  final today = now?.toLocal() ?? DateTime.now();
  final start = DateTime(today.year, today.month, today.day);
  final local = value.toLocal();
  final day = DateTime(local.year, local.month, local.day);
  final elapsed = start.difference(day).inDays;
  if (elapsed == 0) return 'Today';
  if (elapsed == 1) return 'Yesterday';
  if (elapsed > 1 && elapsed < 7) return DateFormat.EEEE().format(day);
  return day.year == start.year
      ? DateFormat.MMMMd().format(day)
      : DateFormat.yMMMMd().format(day);
}

/// Day boundary between messages, matching the separator on web.
final class _DayDivider extends StatelessWidget {
  const _DayDivider({required this.day});

  final DateTime day;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(14, 20, 14, 8),
        child: Row(
          children: [
            const Expanded(child: Divider(color: KaedeColors.border)),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 10),
              child: Text(
                transcriptDayLabel(day).toUpperCase(),
                style: const TextStyle(
                  color: KaedeColors.muted,
                  fontSize: 11,
                  fontWeight: FontWeight.w700,
                  letterSpacing: .7,
                ),
              ),
            ),
            const Expanded(child: Divider(color: KaedeColors.border)),
          ],
        ),
      );
}

/// Drag a message to the right to reply to it, the way every other mobile
/// chat client behaves.
///
/// Drags that begin within [_edgeWidth] of the screen's left edge are left
/// alone so the shell's page swipe can still take the reader back to the
/// channel list, matching the edge-drawer convention on other clients.
final class _SwipeToReply extends StatefulWidget {
  const _SwipeToReply({
    required this.enabled,
    required this.onReply,
    required this.child,
  });

  final bool enabled;
  final VoidCallback onReply;
  final Widget child;

  @override
  State<_SwipeToReply> createState() => _SwipeToReplyState();
}

final class _SwipeToReplyState extends State<_SwipeToReply>
    with SingleTickerProviderStateMixin {
  static const _trigger = 58.0;
  static const _limit = 84.0;
  static const _edgeWidth = 32.0;

  late final AnimationController _settleController = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 190),
  );
  Animation<double>? _settle;
  var _offset = 0.0;
  var _armed = false;

  @override
  void initState() {
    super.initState();
    _settleController.addListener(() {
      final value = _settle?.value;
      if (value != null && mounted) setState(() => _offset = value);
    });
  }

  @override
  void dispose() {
    _settleController.dispose();
    super.dispose();
  }

  void _start() {
    _settleController.stop();
    _settle = null;
  }

  void _update(DragUpdateDetails details) {
    // Resistance past the trigger keeps the gesture from feeling loose.
    final raw = _offset + details.delta.dx;
    final next = raw <= _trigger
        ? raw.clamp(0.0, _trigger)
        : (_trigger + (raw - _trigger) * .35).clamp(0.0, _limit);
    final armed = next >= _trigger;
    if (armed && !_armed) HapticFeedback.selectionClick();
    _armed = armed;
    setState(() => _offset = next);
  }

  void _release() {
    final fire = _armed;
    _armed = false;
    if (fire) widget.onReply();
    if (_offset == 0) return;
    _settle = Tween<double>(begin: _offset, end: 0).animate(
      CurvedAnimation(parent: _settleController, curve: Curves.easeOutCubic),
    );
    _settleController
      ..value = 0
      ..forward();
  }

  @override
  Widget build(BuildContext context) {
    if (!widget.enabled) return widget.child;
    final progress = (_offset / _trigger).clamp(0.0, 1.0);
    return RawGestureDetector(
      gestures: <Type, GestureRecognizerFactory>{
        _MessageDragRecognizer:
            GestureRecognizerFactoryWithHandlers<_MessageDragRecognizer>(
          () => _MessageDragRecognizer(edgeWidth: _edgeWidth),
          (recognizer) {
            recognizer.onStart = (_) => _start();
            recognizer.onUpdate = _update;
            recognizer.onEnd = (_) => _release();
            recognizer.onCancel = _release;
          },
        ),
      },
      child: Stack(
        children: [
          if (_offset > 0)
            Positioned.fill(
              child: Align(
                alignment: Alignment.centerLeft,
                child: Padding(
                  padding: const EdgeInsets.only(left: 18),
                  child: Opacity(
                    opacity: progress,
                    child: Transform.scale(
                      scale: .72 + .28 * progress,
                      child: Icon(
                        Icons.reply_rounded,
                        size: 21,
                        color: progress == 1
                            ? KaedeColors.coralText
                            : KaedeColors.muted,
                      ),
                    ),
                  ),
                ),
              ),
            ),
          Transform.translate(
            offset: Offset(_offset, 0),
            child: widget.child,
          ),
        ],
      ),
    );
  }
}

/// Horizontal drag recognizer that declines pointers starting at the screen's
/// left edge, so the surrounding page view keeps its back swipe.
final class _MessageDragRecognizer extends HorizontalDragGestureRecognizer {
  _MessageDragRecognizer({required this.edgeWidth});

  final double edgeWidth;

  @override
  void addAllowedPointer(PointerDownEvent event) {
    if (event.position.dx <= edgeWidth) return;
    super.addAllowedPointer(event);
  }
}

/// Compact reminder of which message an action sheet applies to.
final class _MessageActionsHeader extends StatelessWidget {
  const _MessageActionsHeader({required this.message});

  final KaedeMessage message;

  @override
  Widget build(BuildContext context) {
    final author = message.author;
    final content = message.content?.trim();
    final preview = content?.isNotEmpty == true
        ? spoilerSafeReplyPreview(content!)
        : message.attachments.isNotEmpty
            ? '${message.attachments.length} attachment'
                '${message.attachments.length == 1 ? '' : 's'}'
            : 'Message';
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 0, 20, 10),
      child: Row(
        children: [
          if (author != null) ...[
            UserAvatar(user: author, radius: 15, ringColor: KaedeColors.panel),
            const SizedBox(width: 10),
          ],
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  author?.name ?? 'Unknown author',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    fontWeight: FontWeight.w700,
                    fontSize: 14,
                  ),
                ),
                Text(
                  preview,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: KaedeColors.muted,
                    fontSize: 12.5,
                  ),
                ),
              ],
            ),
          ),
          Text(
            DateFormat.jm().format(message.createdAt.toLocal()),
            style: const TextStyle(color: KaedeColors.muted, fontSize: 11.5),
          ),
        ],
      ),
    );
  }
}

/// Floating shortcut back to the newest message, shown once the reader has
/// scrolled a screenful or more into history.
final class _JumpToPresentButton extends StatelessWidget {
  const _JumpToPresentButton({
    required this.visible,
    required this.unread,
    required this.onTap,
  });

  final bool visible;
  final int unread;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => IgnorePointer(
        ignoring: !visible,
        child: AnimatedOpacity(
          opacity: visible ? 1 : 0,
          duration: const Duration(milliseconds: 160),
          child: AnimatedSlide(
            offset: Offset(0, visible ? 0 : .35),
            duration: const Duration(milliseconds: 180),
            curve: Curves.easeOutCubic,
            child: Material(
              color: KaedeColors.raised,
              borderRadius: BorderRadius.circular(KaedeRadius.pill),
              elevation: 4,
              shadowColor: Colors.black45,
              child: InkWell(
                onTap: onTap,
                borderRadius: BorderRadius.circular(KaedeRadius.pill),
                child: Container(
                  padding: const EdgeInsets.fromLTRB(12, 8, 14, 8),
                  decoration: BoxDecoration(
                    borderRadius: BorderRadius.circular(KaedeRadius.pill),
                    border: Border.all(color: KaedeColors.border),
                  ),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const Icon(Icons.arrow_downward_rounded,
                          size: 16, color: KaedeColors.coralText),
                      const SizedBox(width: 7),
                      Text(
                        unread > 0
                            ? '$unread new message${unread == 1 ? '' : 's'}'
                            : 'Jump to present',
                        style: const TextStyle(
                          fontSize: 12.5,
                          fontWeight: FontWeight.w700,
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
}

final class _FederationStatusStrip extends StatelessWidget {
  const _FederationStatusStrip({
    required this.title,
    required this.message,
  });

  final String title;
  final String message;

  @override
  Widget build(BuildContext context) => Container(
        width: double.infinity,
        padding: const EdgeInsets.fromLTRB(14, 10, 14, 11),
        decoration: const BoxDecoration(
          color: KaedeColors.warningSoft,
          border: Border(bottom: BorderSide(color: KaedeColors.border)),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Padding(
              padding: EdgeInsets.only(top: 1, right: 10),
              child: Icon(Icons.info_outline_rounded,
                  size: 17, color: KaedeColors.warning),
            ),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    title,
                    style: const TextStyle(
                      color: KaedeColors.warning,
                      fontWeight: FontWeight.w700,
                      fontSize: 13,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    message,
                    style: const TextStyle(
                      color: KaedeColors.textSoft,
                      fontSize: 12,
                      height: 1.35,
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      );
}

final class _HistoryBoundary extends StatelessWidget {
  const _HistoryBoundary({required this.complete});

  final bool complete;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(24, 20, 24, 12),
        child: Column(
          children: [
            Icon(
              complete ? Icons.flag_outlined : Icons.cloud_download_outlined,
              size: 20,
              color: KaedeColors.muted,
            ),
            const SizedBox(height: 8),
            Text(
              complete
                  ? 'Beginning of conversation'
                  : 'Recent history starts here',
              textAlign: TextAlign.center,
              style: const TextStyle(
                fontWeight: FontWeight.w700,
                fontSize: 13.5,
              ),
            ),
            const SizedBox(height: 4),
            Text(
              complete
                  ? 'You have reached the oldest message available from the '
                      'conversation home.'
                  : 'This instance keeps a rolling cache of this remote '
                      'conversation. Older messages load on demand from its '
                      'home instance; retry if that instance is temporarily '
                      'unavailable.',
              textAlign: TextAlign.center,
              style: const TextStyle(
                color: KaedeColors.muted,
                fontSize: 12,
                height: 1.35,
              ),
            ),
          ],
        ),
      );
}

final class _PendingMessageTile extends StatelessWidget {
  const _PendingMessageTile({
    required this.item,
    required this.onRetry,
    required this.onDiscard,
  });

  final OutboxItem item;
  final VoidCallback onRetry;
  final VoidCallback onDiscard;

  @override
  Widget build(BuildContext context) {
    final failed = item.state == 'failed';
    final content = '${item.payload['content'] ?? ''}'.trim();
    return Padding(
      padding: const EdgeInsets.fromLTRB(_messageGutter, 4, 12, 4),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (content.isNotEmpty)
            Opacity(
              opacity: failed ? .85 : .55,
              child: Text(content, style: const TextStyle(fontSize: 15.5)),
            ),
          Padding(
            padding: const EdgeInsets.only(top: 2),
            child: Row(
              children: [
                if (failed)
                  const Icon(Icons.error_outline_rounded,
                      size: 14, color: KaedeColors.danger)
                else
                  const SizedBox.square(
                    dimension: 11,
                    child: CircularProgressIndicator(
                      strokeWidth: 1.6,
                      color: KaedeColors.muted,
                    ),
                  ),
                const SizedBox(width: 7),
                Expanded(
                  child: Text(
                    failed
                        ? userFacingError(
                            item.lastError ?? 'Message could not be sent.',
                          )
                        : 'Sending…',
                    style: TextStyle(
                      color: failed ? KaedeColors.danger : KaedeColors.muted,
                      fontSize: 12,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
                if (failed) ...[
                  TextButton(
                    onPressed: onRetry,
                    style: TextButton.styleFrom(
                      minimumSize: const Size(0, 32),
                      padding: const EdgeInsets.symmetric(horizontal: 10),
                    ),
                    child: const Text('Retry'),
                  ),
                  IconButton(
                    onPressed: onDiscard,
                    tooltip: 'Discard message',
                    visualDensity: VisualDensity.compact,
                    icon: const Icon(Icons.close_rounded, size: 17),
                  ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }
}

final class _ChatErrorStrip extends StatelessWidget {
  const _ChatErrorStrip({required this.message, required this.onRetry});

  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) => DecoratedBox(
        decoration: const BoxDecoration(
          color: KaedeColors.dangerSoft,
          border: Border(bottom: BorderSide(color: KaedeColors.border)),
        ),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(14, 6, 6, 6),
          child: Row(
            children: [
              const Icon(Icons.error_outline_rounded,
                  size: 17, color: KaedeColors.danger),
              const SizedBox(width: 10),
              Expanded(
                child: Text(
                  message,
                  maxLines: 3,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: KaedeColors.danger,
                    fontSize: 12.5,
                    fontWeight: FontWeight.w600,
                    height: 1.3,
                  ),
                ),
              ),
              TextButton(
                onPressed: onRetry,
                style: TextButton.styleFrom(
                  minimumSize: const Size(0, 34),
                  padding: const EdgeInsets.symmetric(horizontal: 12),
                ),
                child: const Text('Retry'),
              ),
            ],
          ),
        ),
      );
}

final class _ReactionViewerSheet extends StatefulWidget {
  const _ReactionViewerSheet({
    required this.message,
    required this.repository,
  });

  final KaedeMessage message;
  final KaedeRepository repository;

  @override
  State<_ReactionViewerSheet> createState() => _ReactionViewerSheetState();
}

final class _ReactionViewerSheetState extends State<_ReactionViewerSheet> {
  late String selectedEmoji;
  final Map<String, List<KaedeUser>> users = <String, List<KaedeUser>>{};
  final Map<String, EntityRef?> nextAfter = <String, EntityRef?>{};
  final Map<String, String> errors = <String, String>{};
  final Set<String> loading = <String>{};

  List<MapEntry<String, int>> get reactions =>
      widget.message.reactionCounts.entries
          .where((entry) => entry.value > 0)
          .toList(growable: false);

  @override
  void initState() {
    super.initState();
    selectedEmoji = reactions.first.key;
    _load(selectedEmoji);
  }

  Future<void> _load(String emoji, {bool append = false}) async {
    if (loading.contains(emoji)) return;
    setState(() {
      loading.add(emoji);
      errors.remove(emoji);
    });
    try {
      final page = await widget.repository.reactionUsers(
        widget.message.channelRef,
        widget.message.ref,
        emoji,
        after: append ? nextAfter[emoji] : null,
      );
      if (!mounted) return;
      setState(() {
        final merged = <EntityRef, KaedeUser>{
          if (append)
            for (final user in users[emoji] ?? const <KaedeUser>[])
              user.ref: user,
          for (final user in page.items) user.ref: user,
        };
        users[emoji] = merged.values.toList(growable: false);
        nextAfter[emoji] = page.nextAfter;
      });
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        errors[emoji] = userFacingError(
          error,
          summary: 'Could not load the people who reacted',
        );
      });
    } finally {
      if (mounted) setState(() => loading.remove(emoji));
    }
  }

  void _select(String emoji) {
    if (emoji == selectedEmoji) return;
    setState(() => selectedEmoji = emoji);
    if (!users.containsKey(emoji) && !loading.contains(emoji)) _load(emoji);
  }

  @override
  Widget build(BuildContext context) {
    final selectedUsers = users[selectedEmoji] ?? const <KaedeUser>[];
    final error = errors[selectedEmoji];
    final isLoading = loading.contains(selectedEmoji);
    return SafeArea(
      child: FractionallySizedBox(
        heightFactor: .62,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                children: [
                  const Expanded(
                    child: Text(
                      'Reactions',
                      style:
                          TextStyle(fontSize: 20, fontWeight: FontWeight.w800),
                    ),
                  ),
                  IconButton(
                    tooltip: 'Close reactions',
                    onPressed: () => Navigator.pop(context),
                    icon: const Icon(Icons.close_rounded),
                  ),
                ],
              ),
              SizedBox(
                height: 44,
                child: ListView.separated(
                  scrollDirection: Axis.horizontal,
                  itemCount: reactions.length,
                  separatorBuilder: (_, __) => const SizedBox(width: 6),
                  itemBuilder: (context, index) {
                    final reaction = reactions[index];
                    return ChoiceChip(
                      selected: selectedEmoji == reaction.key,
                      label: Text('${reaction.key}  ${reaction.value}'),
                      onSelected: (_) => _select(reaction.key),
                    );
                  },
                ),
              ),
              const Divider(height: 20),
              Expanded(
                child: error != null && selectedUsers.isEmpty
                    ? Center(
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Text(error, textAlign: TextAlign.center),
                            const SizedBox(height: 8),
                            TextButton(
                              onPressed: () => _load(selectedEmoji),
                              child: const Text('Try again'),
                            ),
                          ],
                        ),
                      )
                    : selectedUsers.isEmpty && isLoading
                        ? const Center(child: CircularProgressIndicator())
                        : selectedUsers.isEmpty
                            ? const Center(
                                child: Text('No reactions to show.'),
                              )
                            : ListView.builder(
                                itemCount: selectedUsers.length +
                                    (nextAfter[selectedEmoji] == null ? 0 : 1),
                                itemBuilder: (context, index) {
                                  if (index == selectedUsers.length) {
                                    return Padding(
                                      padding: const EdgeInsets.only(top: 8),
                                      child: OutlinedButton(
                                        onPressed: isLoading
                                            ? null
                                            : () => _load(
                                                  selectedEmoji,
                                                  append: true,
                                                ),
                                        child: Text(isLoading
                                            ? 'Loading…'
                                            : 'Load more'),
                                      ),
                                    );
                                  }
                                  final user = selectedUsers[index];
                                  return ListTile(
                                    contentPadding: const EdgeInsets.symmetric(
                                        horizontal: 4),
                                    leading: UserAvatar(user: user, radius: 19),
                                    title: Text(user.name),
                                    subtitle: user.profileResolved
                                        ? Text(user.handle)
                                        : const Text(
                                            'Profile unavailable · refreshes automatically'),
                                  );
                                },
                              ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

final class _ReactionChip extends StatelessWidget {
  const _ReactionChip({
    required this.emoji,
    required this.count,
    required this.mine,
    required this.onTap,
  });

  final String emoji;
  final int count;
  final bool mine;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => Material(
        color: mine ? KaedeColors.coralSoft : KaedeColors.raised,
        borderRadius: BorderRadius.circular(KaedeRadius.small),
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(KaedeRadius.small),
          child: Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(KaedeRadius.small),
              border: Border.all(
                color: mine ? KaedeColors.coral : KaedeColors.border,
              ),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(emoji, style: const TextStyle(fontSize: 14)),
                const SizedBox(width: 5),
                Text(
                  '$count',
                  style: TextStyle(
                    fontSize: 12.5,
                    fontWeight: FontWeight.w700,
                    color: mine ? KaedeColors.coralText : KaedeColors.textSoft,
                  ),
                ),
              ],
            ),
          ),
        ),
      );
}

final class _AddReactionChip extends StatelessWidget {
  const _AddReactionChip({required this.onTap});

  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => Tooltip(
        message: 'Add reaction',
        child: Material(
          color: KaedeColors.raised,
          borderRadius: BorderRadius.circular(KaedeRadius.small),
          child: InkWell(
            onTap: onTap,
            borderRadius: BorderRadius.circular(KaedeRadius.small),
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(KaedeRadius.small),
                border: Border.all(color: KaedeColors.border),
              ),
              child: const Icon(Icons.add_reaction_outlined,
                  size: 15, color: KaedeColors.muted),
            ),
          ),
        ),
      );
}

final class _TypingDots extends StatefulWidget {
  const _TypingDots();

  @override
  State<_TypingDots> createState() => _TypingDotsState();
}

final class _TypingDotsState extends State<_TypingDots>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1100),
  )..repeat();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
        animation: _controller,
        builder: (context, _) => Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            for (var index = 0; index < 3; index++) ...[
              if (index > 0) const SizedBox(width: 3),
              Opacity(
                opacity: .35 +
                    .65 *
                        (1 -
                            ((_controller.value * 3 - index) % 3)
                                .clamp(0.0, 1.0)),
                child: Container(
                  width: 5,
                  height: 5,
                  decoration: const BoxDecoration(
                    color: KaedeColors.muted,
                    shape: BoxShape.circle,
                  ),
                ),
              ),
            ],
          ],
        ),
      );
}

final class _ReplyingBar extends StatelessWidget {
  const _ReplyingBar({
    required this.author,
    required this.preview,
    required this.notify,
    required this.onNotifyChanged,
    required this.onClose,
  });

  final String author;
  final String preview;
  final bool notify;
  final ValueChanged<bool> onNotifyChanged;
  final VoidCallback onClose;

  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.fromLTRB(14, 8, 6, 8),
        decoration: const BoxDecoration(
          border: Border(bottom: BorderSide(color: KaedeColors.border)),
        ),
        child: Row(
          children: [
            const Icon(Icons.reply_rounded, size: 15, color: KaedeColors.muted),
            const SizedBox(width: 8),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text.rich(
                    TextSpan(
                      text: 'Replying to ',
                      style: const TextStyle(
                        color: KaedeColors.muted,
                        fontSize: 12.5,
                      ),
                      children: [
                        TextSpan(
                          text: author,
                          style: const TextStyle(
                            color: KaedeColors.text,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ],
                    ),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                  ),
                  if (preview.trim().isNotEmpty)
                    Text(
                      preview,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: KaedeColors.muted,
                        fontSize: 11.5,
                      ),
                    ),
                ],
              ),
            ),
            Tooltip(
              message: notify
                  ? 'The author will be notified'
                  : 'Reply without notifying the author',
              child: TextButton.icon(
                onPressed: () => onNotifyChanged(!notify),
                icon: Icon(
                  notify
                      ? Icons.alternate_email_rounded
                      : Icons.notifications_off_rounded,
                  size: 15,
                ),
                label: Text(notify ? 'ON' : 'OFF'),
                style: TextButton.styleFrom(
                  minimumSize: const Size(0, 34),
                  visualDensity: VisualDensity.compact,
                  padding: const EdgeInsets.symmetric(horizontal: 8),
                  foregroundColor:
                      notify ? KaedeColors.coralText : KaedeColors.muted,
                  textStyle: const TextStyle(
                    fontSize: 11,
                    fontWeight: FontWeight.w800,
                    letterSpacing: .6,
                  ),
                ),
              ),
            ),
            IconButton(
              onPressed: onClose,
              tooltip: 'Cancel reply',
              visualDensity: VisualDensity.compact,
              icon: const Icon(Icons.close_rounded, size: 18),
            ),
          ],
        ),
      );
}

final class _UploadChip extends StatelessWidget {
  const _UploadChip({required this.item, required this.onRemove});

  final _PendingUpload item;
  final VoidCallback onRemove;

  @override
  Widget build(BuildContext context) {
    final isImage = item.contentType.startsWith('image/');
    return Container(
      width: isImage ? 64 : 172,
      clipBehavior: Clip.antiAlias,
      decoration: BoxDecoration(
        color: KaedeColors.panel,
        borderRadius: BorderRadius.circular(KaedeRadius.medium),
        border: Border.all(color: KaedeColors.border),
      ),
      child: Stack(
        fit: StackFit.expand,
        children: [
          if (isImage)
            Image.file(
              item.file,
              fit: BoxFit.cover,
              errorBuilder: (_, __, ___) => const ColoredBox(
                color: KaedeColors.raised,
                child: Icon(Icons.image_outlined,
                    size: 20, color: KaedeColors.muted),
              ),
            )
          else
            Padding(
              padding: const EdgeInsets.fromLTRB(10, 8, 30, 8),
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Icon(
                        item.contentType.startsWith('video/')
                            ? Icons.movie_outlined
                            : item.contentType.startsWith('audio/')
                                ? Icons.audiotrack_rounded
                                : Icons.description_outlined,
                        size: 16,
                        color: KaedeColors.muted,
                      ),
                      const SizedBox(width: 6),
                      Expanded(
                        child: Text(
                          item.name,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            fontSize: 12,
                            fontWeight: FontWeight.w600,
                            height: 1.25,
                          ),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 3),
                  Text(
                    formatAttachmentSize(item.size),
                    style: const TextStyle(
                      fontSize: 11,
                      color: KaedeColors.muted,
                    ),
                  ),
                ],
              ),
            ),
          Positioned(
            top: 2,
            right: 2,
            child: Material(
              color: Colors.black54,
              shape: const CircleBorder(),
              child: InkWell(
                onTap: onRemove,
                customBorder: const CircleBorder(),
                child: const Padding(
                  padding: EdgeInsets.all(4),
                  child:
                      Icon(Icons.close_rounded, size: 14, color: Colors.white),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Left inset shared by message content, system rows and pending messages so
/// every line in the transcript starts on the same vertical rule.
const double _messageGutter = 60;

final class _MessageTile extends StatelessWidget {
  const _MessageTile(
      {required this.message,
      required this.state,
      required this.compact,
      required this.onReply,
      required this.onMenu,
      required this.onReaction,
      this.onAddReaction,
      this.onAuthorTap,
      this.referenced,
      this.onJump});
  final KaedeMessage message;
  final MobileState state;
  final KaedeMessage? referenced;
  final bool compact;
  final VoidCallback onReply;
  final VoidCallback onMenu;
  final ValueChanged<String> onReaction;
  final VoidCallback? onAddReaction;
  final VoidCallback? onAuthorTap;
  final VoidCallback? onJump;

  void _openMenu() {
    HapticFeedback.mediumImpact();
    onMenu();
  }

  @override
  Widget build(BuildContext context) {
    if (message.messageType >= 3 && message.messageType <= 5) {
      return _SystemMessageRow(message: message);
    }
    final author = message.author;
    final deleted = message.deletedAt != null;
    final mediaPreview = previewMediaUrl(message.content ?? '');
    final failed = message.deliveryStatus == 'failed';
    return InkWell(
      onLongPress: _openMenu,
      onDoubleTap: deleted ? null : onReply,
      child: Padding(
        padding: EdgeInsets.fromLTRB(12, compact ? 1 : 8, 12, 1),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              width: 40,
              child: compact
                  ? null
                  : author == null
                      ? const CircleAvatar(
                          radius: 20,
                          backgroundColor: KaedeColors.raised,
                          foregroundColor: KaedeColors.muted,
                          child: Text('?',
                              style: TextStyle(fontWeight: FontWeight.w700)),
                        )
                      : GestureDetector(
                          onTap: onAuthorTap,
                          child: UserAvatar(user: author, radius: 20),
                        ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (message.reference != null)
                    _ReplyReference(
                      referenced: referenced,
                      onTap: onJump,
                    ),
                  if (!compact)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 2),
                      child: Row(
                        crossAxisAlignment: CrossAxisAlignment.baseline,
                        textBaseline: TextBaseline.alphabetic,
                        children: [
                          Flexible(
                            child: GestureDetector(
                              onTap: onAuthorTap,
                              child: Text(
                                author?.name ?? 'Unknown author',
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                  fontWeight: FontWeight.w700,
                                  fontSize: 15,
                                  height: 1.2,
                                  letterSpacing: -.1,
                                ),
                              ),
                            ),
                          ),
                          const SizedBox(width: 8),
                          Text(
                            DateFormat.jm().format(message.createdAt.toLocal()),
                            style: const TextStyle(
                              color: KaedeColors.muted,
                              fontSize: 11.5,
                              fontWeight: FontWeight.w500,
                            ),
                          ),
                          if (message.editedAt != null) ...[
                            const SizedBox(width: 6),
                            const Text(
                              '(edited)',
                              style: TextStyle(
                                color: KaedeColors.muted,
                                fontSize: 11,
                              ),
                            ),
                          ],
                          if (message.pinned) ...[
                            const SizedBox(width: 6),
                            const Icon(Icons.push_pin_rounded,
                                size: 11, color: KaedeColors.muted),
                          ],
                        ],
                      ),
                    ),
                  if (deleted)
                    const Text(
                      'Message deleted',
                      style: TextStyle(
                        color: KaedeColors.muted,
                        fontStyle: FontStyle.italic,
                      ),
                    )
                  else if (message.e2ee != null && message.content == null)
                    const _UndecryptableNotice()
                  else if (message.content case final content?
                      when content.isNotEmpty)
                    KaedeMessageMarkdown(
                      content: content,
                      state: state,
                      omitMediaUrl: mediaPreview,
                    ),
                  if (!deleted && mediaPreview != null)
                    _RemoteMediaPreview(uri: mediaPreview),
                  for (final attachment in deleted
                      ? const <KaedeAttachment>[]
                      : message.attachments)
                    _AttachmentCard(
                      attachment: attachment,
                      encryptedManifest:
                          _encryptedManifestFor(message, attachment),
                    ),
                  if (!deleted && message.reactionCounts.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(top: 6, bottom: 2),
                      child: Wrap(
                        spacing: 5,
                        runSpacing: 5,
                        children: [
                          for (final reaction in message.reactionCounts.entries)
                            _ReactionChip(
                              emoji: reaction.key,
                              count: reaction.value,
                              mine: message.reactedEmoji.contains(reaction.key),
                              onTap: () => onReaction(reaction.key),
                            ),
                          if (onAddReaction != null)
                            _AddReactionChip(onTap: onAddReaction!),
                        ],
                      ),
                    ),
                  if (failed || message.deliveryStatus == 'retrying')
                    Padding(
                      padding: const EdgeInsets.only(top: 4),
                      child: Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Icon(
                            failed
                                ? Icons.error_outline_rounded
                                : Icons.sync_rounded,
                            size: 14,
                            color:
                                failed ? KaedeColors.danger : KaedeColors.muted,
                          ),
                          const SizedBox(width: 6),
                          Expanded(
                            child: Text(
                              failed
                                  ? message.failureReason ??
                                      'Message not delivered.'
                                  : message.failureReason ??
                                      'The receiving instance is temporarily at '
                                          'capacity. Kaede is retrying automatically.',
                              style: TextStyle(
                                color: failed
                                    ? KaedeColors.danger
                                    : KaedeColors.muted,
                                fontSize: 12,
                                fontWeight: FontWeight.w600,
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Join, leave and removal notices, kept visually quieter than real messages.
final class _SystemMessageRow extends StatelessWidget {
  const _SystemMessageRow({required this.message});

  final KaedeMessage message;

  @override
  Widget build(BuildContext context) {
    final icon = switch (message.messageType) {
      3 => Icons.person_add_alt_1_rounded,
      4 => Icons.logout_rounded,
      _ => Icons.person_remove_alt_1_rounded,
    };
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 6, 14, 6),
      child: Row(
        children: [
          SizedBox(
            width: 40,
            child: Center(
              child: Container(
                width: 26,
                height: 26,
                decoration: const BoxDecoration(
                  shape: BoxShape.circle,
                  color: KaedeColors.coralSoft,
                ),
                child: Icon(icon, size: 14, color: KaedeColors.coralText),
              ),
            ),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              message.content ?? 'Group membership changed.',
              style: const TextStyle(color: KaedeColors.muted, fontSize: 13),
            ),
          ),
          const SizedBox(width: 8),
          Text(
            DateFormat.jm().format(message.createdAt.toLocal()),
            style: const TextStyle(color: KaedeColors.muted, fontSize: 11),
          ),
        ],
      ),
    );
  }
}

/// The quoted message above a reply.
final class _ReplyReference extends StatelessWidget {
  const _ReplyReference({required this.referenced, required this.onTap});

  final KaedeMessage? referenced;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final author = referenced?.author;
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(KaedeRadius.small),
      child: Padding(
        padding: const EdgeInsets.only(top: 2, bottom: 3, right: 4),
        child: Row(
          children: [
            const Padding(
              padding: EdgeInsets.only(right: 6),
              child:
                  Icon(Icons.reply_rounded, size: 13, color: KaedeColors.muted),
            ),
            if (author != null) ...[
              UserAvatar(user: author, radius: 8),
              const SizedBox(width: 5),
            ],
            Text(
              author?.name ?? 'Original message',
              style: const TextStyle(
                fontWeight: FontWeight.w600,
                fontSize: 12.5,
                color: KaedeColors.textSoft,
              ),
            ),
            const SizedBox(width: 6),
            Expanded(
              child: Text(
                referenced == null
                    ? 'Tap to load message'
                    : spoilerSafeReplyPreview(
                        referenced!.content ?? 'Attachment',
                      ),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(
                  color: KaedeColors.muted,
                  fontSize: 12.5,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

final class _UndecryptableNotice extends StatelessWidget {
  const _UndecryptableNotice();

  @override
  Widget build(BuildContext context) => Container(
        margin: const EdgeInsets.only(top: 2, bottom: 2),
        padding: const EdgeInsets.fromLTRB(10, 9, 12, 9),
        decoration: BoxDecoration(
          color: KaedeColors.raised,
          borderRadius: BorderRadius.circular(KaedeRadius.medium),
          border: Border.all(color: KaedeColors.border),
        ),
        child: const Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(Icons.lock_outline_rounded,
                size: 16, color: KaedeColors.muted),
            SizedBox(width: 8),
            Expanded(
              child: Text(
                'Can\u2019t decrypt this message on this device. Verify, recover, or '
                'update this device\u2019s encryption support.',
                style: TextStyle(
                  color: KaedeColors.muted,
                  fontSize: 13,
                  height: 1.3,
                ),
              ),
            ),
          ],
        ),
      );
}

Map<String, Object?>? _encryptedManifestFor(
  KaedeMessage message,
  KaedeAttachment attachment,
) {
  for (final manifest in message.decryptedAttachments) {
    if ('${manifest['attachment_id']}' == attachment.ref.id.value &&
        '${manifest['attachment_domain']}' == attachment.ref.domain.value) {
      return manifest;
    }
  }
  return null;
}

KaedeAttachment _manifestAttachment(
  KaedeAttachment attachment,
  Map<String, Object?>? manifest,
) {
  if (manifest == null) return attachment;
  return KaedeAttachment(
    ref: attachment.ref,
    filename: '${manifest['filename'] ?? 'file'}',
    contentType: '${manifest['content_type'] ?? 'application/octet-stream'}',
    size: (manifest['plaintext_size'] as num?)?.toInt() ?? 0,
    scanStatus: attachment.scanStatus == 'pending' ||
            attachment.scanStatus == 'processing'
        ? attachment.scanStatus
        : 'encrypted',
    historyMediaUrl: attachment.historyMediaUrl,
  );
}

final class _AttachmentCard extends ConsumerStatefulWidget {
  const _AttachmentCard({
    required this.attachment,
    this.encryptedManifest,
  });
  final KaedeAttachment attachment;
  final Map<String, Object?>? encryptedManifest;

  @override
  ConsumerState<_AttachmentCard> createState() => _AttachmentCardState();
}

final class _AttachmentStatusCard extends StatelessWidget {
  const _AttachmentStatusCard({
    required this.attachment,
    required this.icon,
    required this.message,
    this.error = false,
    this.onRetry,
  });

  final KaedeAttachment attachment;
  final IconData icon;
  final String message;
  final bool error;
  final VoidCallback? onRetry;

  @override
  Widget build(BuildContext context) {
    final card = Container(
      margin: const EdgeInsets.only(top: 7),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: KaedeColors.raised,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(
          color: error ? KaedeColors.danger : KaedeColors.border,
        ),
      ),
      child: Row(
        children: [
          Icon(icon, color: error ? KaedeColors.danger : KaedeColors.muted),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              message,
              maxLines: error ? 3 : 1,
              overflow: TextOverflow.ellipsis,
            ),
          ),
          if (onRetry != null)
            TextButton(onPressed: onRetry, child: const Text('Retry')),
        ],
      ),
    );
    if (error || !attachment.contentType.startsWith('image/')) return card;
    final width = attachment.width;
    final height = attachment.height;
    final ratio = width != null && height != null && width > 0 && height > 0
        ? (width / height).clamp(.65, 2.4)
        : 16 / 9;
    return AspectRatio(aspectRatio: ratio, child: card);
  }
}

final class _RemoteMediaPreview extends StatefulWidget {
  const _RemoteMediaPreview({required this.uri});
  final Uri uri;

  @override
  State<_RemoteMediaPreview> createState() => _RemoteMediaPreviewState();
}

final class _RemoteMediaPreviewState extends State<_RemoteMediaPreview> {
  Future<File>? _videoFile;
  HttpClient? _downloadClient;
  var _previewGeneration = 0;

  bool get _isVideo => widget.uri.path.toLowerCase().endsWith('.mp4');

  @override
  void initState() {
    super.initState();
    _initializeVideo();
  }

  @override
  void didUpdateWidget(covariant _RemoteMediaPreview oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.uri != widget.uri) {
      _downloadClient?.close(force: true);
      _videoFile = null;
      _initializeVideo();
    }
  }

  void _initializeVideo() {
    if (!_isVideo) return;
    _videoFile = _cacheVideo(widget.uri);
  }

  Future<void> _retryPreview() async {
    _downloadClient?.close(force: true);
    if (!_isVideo) {
      await CachedNetworkImage.evictFromCache(widget.uri.toString());
    }
    if (!mounted) return;
    setState(() {
      _previewGeneration += 1;
      if (_isVideo) _videoFile = _cacheVideo(widget.uri);
    });
  }

  @override
  void dispose() {
    _downloadClient?.close(force: true);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(top: 7),
        child: GestureDetector(
          onLongPress: () => _copyLink(context),
          child: ClipRRect(
            borderRadius: BorderRadius.circular(12),
            child: _isVideo
                ? FutureBuilder<File>(
                    future: _videoFile,
                    builder: (context, snapshot) {
                      if (snapshot.hasError) {
                        return _MediaPreviewError(
                          error: snapshot.error!,
                          onRetry: _retryPreview,
                        );
                      }
                      if (!snapshot.hasData) {
                        return const AspectRatio(
                          aspectRatio: 16 / 9,
                          child: ColoredBox(
                            color: KaedeColors.raised,
                            child: Center(child: CircularProgressIndicator()),
                          ),
                        );
                      }
                      return _FileVideo(file: snapshot.data!);
                    },
                  )
                : CachedNetworkImage(
                    key: ValueKey('${widget.uri}#$_previewGeneration'),
                    imageUrl: widget.uri.toString(),
                    fit: BoxFit.contain,
                    fadeInDuration: Duration.zero,
                    placeholder: (_, __) => const AspectRatio(
                      aspectRatio: 16 / 9,
                      child: ColoredBox(
                        color: KaedeColors.raised,
                        child: Center(child: CircularProgressIndicator()),
                      ),
                    ),
                    errorWidget: (_, __, error) => _MediaPreviewError(
                      error: error,
                      onRetry: _retryPreview,
                    ),
                  ),
          ),
        ),
      );

  Future<File> _cacheVideo(Uri original) async {
    final directory = await getTemporaryDirectory();
    final destination = File(
      '${directory.path}/kaede-link-video-${stableMediaCacheKey(original)}.mp4',
    );
    if (await destination.exists() && await destination.length() > 0) {
      return destination;
    }
    final client = HttpClient()
      ..connectionTimeout = const Duration(seconds: 12);
    _downloadClient = client;
    var uri = original;
    try {
      for (var redirects = 0; redirects <= 5; redirects += 1) {
        final request = await client.getUrl(uri);
        request.followRedirects = false;
        final response = await request.close();
        if (<int>{301, 302, 303, 307, 308}.contains(response.statusCode)) {
          final location = response.headers.value(HttpHeaders.locationHeader);
          await response.drain<void>();
          if (location == null || redirects == 5) {
            throw const HttpException('Invalid media redirect');
          }
          final next = uri.resolve(location);
          if (next.scheme != 'https') {
            throw const HttpException('Unsafe media redirect');
          }
          uri = next;
          continue;
        }
        if (response.statusCode < 200 || response.statusCode >= 300) {
          await response.drain<void>();
          throw HttpException('Media request failed', uri: uri);
        }
        const maximum = 100 * 1024 * 1024;
        if (response.contentLength > maximum) {
          await response.drain<void>();
          throw const HttpException('Media is too large');
        }
        final temporary = File('${destination.path}.part');
        final sink = temporary.openWrite();
        var received = 0;
        try {
          await for (final chunk in response) {
            received += chunk.length;
            if (received > maximum) {
              throw const HttpException('Media is too large');
            }
            sink.add(chunk);
          }
          await sink.close();
        } on Object {
          await sink.close();
          if (await temporary.exists()) await temporary.delete();
          rethrow;
        }
        return temporary.rename(destination.path);
      }
      throw const HttpException('Too many media redirects');
    } finally {
      if (identical(_downloadClient, client)) _downloadClient = null;
      client.close();
    }
  }

  Future<void> _copyLink(BuildContext context) async {
    final copy = await showModalBottomSheet<bool>(
      context: context,
      showDragHandle: true,
      builder: (context) => SafeArea(
        child: ListTile(
          leading: const Icon(Icons.link_rounded),
          title: const Text('Copy media link'),
          onTap: () => Navigator.pop(context, true),
        ),
      ),
    );
    if (copy == true) {
      await Clipboard.setData(ClipboardData(text: widget.uri.toString()));
    }
  }
}

final class _MediaPreviewError extends StatelessWidget {
  const _MediaPreviewError({required this.error, required this.onRetry});

  final Object error;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) => AspectRatio(
        aspectRatio: 16 / 9,
        child: ColoredBox(
          color: KaedeColors.raised,
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                const Icon(
                  Icons.broken_image_outlined,
                  color: KaedeColors.danger,
                ),
                const SizedBox(height: 8),
                Text(
                  userFacingError(
                    error,
                    summary: 'Could not load the media preview',
                  ),
                  maxLines: 3,
                  overflow: TextOverflow.ellipsis,
                  textAlign: TextAlign.center,
                  style: const TextStyle(fontSize: 12),
                ),
                TextButton(onPressed: onRetry, child: const Text('Retry')),
              ],
            ),
          ),
        ),
      );
}

String stableMediaCacheKey(Uri uri) {
  var hash = 0xcbf29ce484222325;
  for (final unit in uri.toString().codeUnits) {
    hash ^= unit;
    hash = (hash * 0x100000001b3) & 0x7fffffffffffffff;
  }
  return hash.toRadixString(16);
}

final class _SpoilerBuilder extends MarkdownElementBuilder {
  @override
  Widget? visitElementAfterWithContext(
    BuildContext context,
    md.Element element,
    TextStyle? preferredStyle,
    TextStyle? parentStyle,
  ) =>
      _SpoilerText(
        text: element.textContent,
        style: parentStyle ?? preferredStyle ?? const TextStyle(),
      );
}

final class _SpoilerText extends StatefulWidget {
  const _SpoilerText({required this.text, required this.style});

  final String text;
  final TextStyle style;

  @override
  State<_SpoilerText> createState() => _SpoilerTextState();
}

final class _SpoilerTextState extends State<_SpoilerText> {
  var _revealed = false;

  @override
  Widget build(BuildContext context) => Semantics(
        button: true,
        label: _revealed
            ? 'Spoiler: ${widget.text}. Hide spoiler'
            : 'Reveal spoiler',
        child: ExcludeSemantics(
          child: InkWell(
            key: ValueKey('message-spoiler-${widget.text}'),
            onTap: () => setState(() => _revealed = !_revealed),
            borderRadius: BorderRadius.circular(4),
            child: AnimatedContainer(
              duration: const Duration(milliseconds: 120),
              padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 1),
              decoration: BoxDecoration(
                // Covered spoilers are a dark block, not a bright slab: a
                // light fill reads as a broken image in a dark transcript.
                color: _revealed ? KaedeColors.raised : KaedeColors.hover,
                borderRadius: BorderRadius.circular(4),
                border: Border.all(
                  color: _revealed
                      ? KaedeColors.border
                      : KaedeColors.borderStrong,
                ),
              ),
              child: Text(
                widget.text,
                style: widget.style.copyWith(
                  color: _revealed ? KaedeColors.text : Colors.transparent,
                ),
              ),
            ),
          ),
        ),
      );
}

enum _MessageTokenKind { user, role, channel, emoji }

final class _MessageTokenBuilder extends MarkdownElementBuilder {
  _MessageTokenBuilder({required this.state, required this.kind});

  final MobileState state;
  final _MessageTokenKind kind;

  @override
  Widget? visitElementAfterWithContext(
    BuildContext context,
    md.Element element,
    TextStyle? preferredStyle,
    TextStyle? parentStyle,
  ) {
    final token = element.attributes['token'] ?? element.textContent;
    final style = parentStyle ?? preferredStyle ?? const TextStyle();
    if (kind == _MessageTokenKind.emoji) {
      final match = _customEmojiToken.firstMatch(token);
      if (match == null) return Text(token, style: style);
      final label = ':${match.group(2)}:';
      late final Uri uri;
      try {
        final emoji = Snowflake(match.group(3)!);
        final domain = Domain(match.group(4)!);
        uri = Uri.https(
          domain.value,
          '/media/emojis/${emoji.value}/thumbnail_128',
        );
      } on FormatException {
        return Text(label, style: style);
      }
      return Semantics(
        image: true,
        label: label,
        child: CachedNetworkImage(
          imageUrl: uri.toString(),
          width: 22,
          height: 22,
          fit: BoxFit.contain,
          placeholder: (_, __) => const SizedBox.square(
            dimension: 22,
            child: Padding(
              padding: EdgeInsets.all(4),
              child: CircularProgressIndicator(strokeWidth: 1.5),
            ),
          ),
          errorWidget: (_, __, ___) => Text(label, style: style),
        ),
      );
    }

    var label = token;
    var foreground = KaedeColors.coral;
    var background = KaedeColors.coral.withValues(alpha: .14);
    if (kind == _MessageTokenKind.user) {
      final user = _mentionTokenUser(token, state);
      label = user == null
          ? token.startsWith('@') && !token.startsWith('<@')
              ? '@${token.substring(1)}'
              : '@unknown-user'
          : '@${user.name}';
    } else if (kind == _MessageTokenKind.role) {
      final role = _mentionTokenRole(token, state);
      label = role == null ? '@unknown-role' : '@${role.name}';
      if (role != null && role.color != 0) {
        foreground = Color(0xff000000 | role.color);
        background = foreground.withValues(alpha: .16);
      }
    } else if (kind == _MessageTokenKind.channel) {
      foreground = KaedeColors.text;
      background = KaedeColors.selected;
    }
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 3, vertical: 1),
      decoration: BoxDecoration(
        color: background,
        borderRadius: BorderRadius.circular(4),
      ),
      child: Text(
        label,
        style: style.copyWith(
          color: foreground,
          fontWeight: FontWeight.w700,
        ),
      ),
    );
  }
}

final class _AttachmentCardState extends ConsumerState<_AttachmentCard> {
  late Future<File> _future;
  late KaedeAttachment _displayAttachment;
  File? _file;
  Timer? _statusTimer;
  Object? _statusError;
  var _statusFailures = 0;
  var _loadGeneration = 0;
  var _disposed = false;

  @override
  void initState() {
    super.initState();
    _displayAttachment = _manifestAttachment(
      widget.attachment,
      widget.encryptedManifest,
    );
    _future = _initialFuture();
    _scheduleStatusPoll();
  }

  @override
  void didUpdateWidget(covariant _AttachmentCard oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.attachment.ref != widget.attachment.ref ||
        oldWidget.attachment.scanStatus != widget.attachment.scanStatus ||
        oldWidget.encryptedManifest != widget.encryptedManifest ||
        oldWidget.attachment.historyMediaUrl !=
            widget.attachment.historyMediaUrl) {
      _loadGeneration += 1;
      _takeFile();
      _displayAttachment = _manifestAttachment(
        widget.attachment,
        widget.encryptedManifest,
      );
      _statusError = null;
      _statusFailures = 0;
      _future = _initialFuture();
      _scheduleStatusPoll();
    }
  }

  Future<File> _initialFuture() => (_displayAttachment.scanStatus == 'clean' ||
          _displayAttachment.scanStatus == 'encrypted')
      ? _load()
      : Completer<File>().future;

  void _scheduleStatusPoll() {
    _statusTimer?.cancel();
    if (_displayAttachment.scanStatus == 'clean' ||
        _displayAttachment.scanStatus == 'encrypted' ||
        _displayAttachment.scanStatus == 'rejected' ||
        _displayAttachment.scanStatus == 'infected' ||
        _displayAttachment.scanStatus == 'failed') {
      return;
    }
    _statusTimer = Timer(const Duration(seconds: 1), _pollStatus);
  }

  Future<void> _pollStatus() async {
    try {
      final attachment = await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .attachmentStatus(_displayAttachment);
      if (!mounted || attachment.ref != widget.attachment.ref) return;
      setState(() {
        _displayAttachment = _manifestAttachment(
          attachment,
          widget.encryptedManifest,
        );
        _statusError = null;
        _statusFailures = 0;
        if (attachment.scanStatus == 'clean' ||
            attachment.scanStatus == 'encrypted') {
          _future = _load(_displayAttachment);
        }
      });
    } on Object catch (error) {
      // Gateway updates are the primary path. Polling is a recovery path for
      // uploads whose scan completion event was missed while backgrounded.
      _statusFailures += 1;
      if (mounted && _statusFailures >= 3) {
        setState(() => _statusError = error);
      }
    }
    if (mounted) _scheduleStatusPoll();
  }

  void _retryStatus() {
    _statusTimer?.cancel();
    setState(() {
      _statusError = null;
      _statusFailures = 0;
    });
    unawaited(_pollStatus());
  }

  Future<File> _load([KaedeAttachment? resolved]) async {
    final generation = ++_loadGeneration;
    final directory = await getTemporaryDirectory();
    final attachment = resolved ?? _displayAttachment;
    final safeDomain = attachment.ref.domain.value.replaceAll(
      RegExp('[^a-z0-9.-]'),
      '_',
    );
    final destination = File(
      '${directory.path}/kaede-media-${attachment.ref.id.value}-$safeDomain-${_safeName(attachment.filename)}',
    );
    if (await destination.exists() && await destination.length() > 0) {
      _file = destination;
      return destination;
    }
    final manifest = widget.encryptedManifest;
    final downloaded = manifest == null
        ? await _downloadWithCapacityRetry(
            attachment,
            destination,
            generation,
          )
        : await downloadEncryptedFile(
            repository: ref.read(mobileControllerProvider.notifier).repository,
            manifest: manifest,
            destination: destination,
            historyMediaUrl: attachment.historyMediaUrl,
          );
    if (_disposed || generation != _loadGeneration) {
      throw const FileSystemException('Attachment load was cancelled.');
    }
    _file = downloaded;
    return downloaded;
  }

  Future<File> _downloadWithCapacityRetry(
    KaedeAttachment attachment,
    File destination,
    int generation,
  ) async {
    final repository = ref.read(mobileControllerProvider.notifier).repository;
    for (var attempt = 0;; attempt += 1) {
      try {
        return await repository.downloadAttachment(attachment, destination);
      } on KaedeException catch (error) {
        final transientCapacity = error.code == 'REMOTE_MEDIA_BUSY' ||
            error.code == 'REMOTE_MEDIA_CACHE_FULL';
        if (!transientCapacity || attempt >= 2) rethrow;
        final serverDelay =
            error.retryAfter ?? Duration(milliseconds: 1000 * (attempt + 1));
        final boundedMilliseconds =
            serverDelay.inMilliseconds.clamp(500, 5000) +
                (attachment.ref.hashCode.abs() % 251);
        await Future<void>.delayed(Duration(milliseconds: boundedMilliseconds));
        if (_disposed || generation != _loadGeneration) {
          throw const FileSystemException('Attachment load was cancelled.');
        }
      }
    }
  }

  File? _takeFile() {
    final file = _file;
    _file = null;
    return file;
  }

  Future<void> _deleteFile(File? file) async {
    if (file != null && await file.exists()) {
      try {
        await file.delete();
      } on FileSystemException {
        // A native decoder may briefly retain the file on Windows.
        await Future<void>.delayed(const Duration(seconds: 1));
        if (await file.exists()) {
          try {
            await file.delete();
          } on FileSystemException {
            // The operating system will remove the temporary directory later.
          }
        }
      }
    }
  }

  @override
  void dispose() {
    _disposed = true;
    _statusTimer?.cancel();
    _loadGeneration += 1;
    _takeFile();
    super.dispose();
  }

  void _retry() {
    _loadGeneration += 1;
    final oldFile = _takeFile();
    unawaited(_deleteFile(oldFile));
    setState(() => _future = _load());
  }

  Future<void> _showMediaActions() async {
    final action = await showModalBottomSheet<String>(
      context: context,
      showDragHandle: true,
      builder: (context) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: const Icon(Icons.link_rounded),
              title: const Text('Copy media link'),
              onTap: () => Navigator.pop(context, 'copy'),
            ),
            ListTile(
              leading: const Icon(Icons.info_outline_rounded),
              title: Text(widget.attachment.filename),
              subtitle: Text(
                '${widget.attachment.contentType} · '
                '${formatAttachmentSize(widget.attachment.size)}',
              ),
            ),
          ],
        ),
      ),
    );
    if (action != 'copy' || !mounted) return;
    final controller = ref.read(mobileControllerProvider.notifier);
    final instance = controller.api.tokens?.instance.value;
    if (instance == null) return;
    await Clipboard.setData(ClipboardData(
      text: 'https://$instance${attachmentMediaPath(
        widget.attachment.ref,
        historyMediaUrl: widget.attachment.historyMediaUrl,
      )}',
    ));
  }

  @override
  Widget build(BuildContext context) {
    final attachment = _displayAttachment;
    if (attachment.scanStatus == 'pending' ||
        attachment.scanStatus == 'processing') {
      if (_statusError case final error?) {
        return _AttachmentStatusCard(
          attachment: attachment,
          icon: Icons.warning_amber_rounded,
          message: userFacingError(
            error,
            summary: 'Could not check the attachment status',
          ),
          error: true,
          onRetry: _retryStatus,
        );
      }
      return _AttachmentStatusCard(
        attachment: attachment,
        icon: Icons.hourglass_top_rounded,
        message: 'Preparing ${attachment.filename}…',
      );
    }
    if (attachment.scanStatus != 'clean' &&
        attachment.scanStatus != 'encrypted') {
      final rejected = attachment.scanStatus == 'rejected' ||
          attachment.scanStatus == 'infected';
      return _AttachmentStatusCard(
        attachment: attachment,
        icon: Icons.warning_amber_rounded,
        message: rejected
            ? '${attachment.filename} was rejected during server processing.'
            : '${attachment.filename} could not be processed by the server. Upload the file again later.',
        error: true,
      );
    }
    final imageWidth = attachment.width;
    final imageHeight = attachment.height;
    final imageRatio = imageWidth != null &&
            imageHeight != null &&
            imageWidth > 0 &&
            imageHeight > 0
        ? (imageWidth / imageHeight).clamp(.65, 2.4)
        : 16 / 9;
    return FutureBuilder<File>(
      future: _future,
      builder: (context, snapshot) {
        if (snapshot.hasError) {
          return Container(
            margin: const EdgeInsets.only(top: 7),
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: KaedeColors.raised,
              borderRadius: BorderRadius.circular(14),
              border: Border.all(color: KaedeColors.danger),
            ),
            child: Row(
              children: [
                const Icon(Icons.broken_image_outlined,
                    color: KaedeColors.danger),
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        attachment.filename,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                      ),
                      const SizedBox(height: 2),
                      Text(
                        userFacingError(
                          snapshot.error!,
                          summary: 'Could not load the attachment',
                        ),
                        maxLines: 3,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(fontSize: 12),
                      ),
                    ],
                  ),
                ),
                TextButton(
                  onPressed: _retry,
                  child: const Text('Retry'),
                ),
              ],
            ),
          );
        }
        if (!snapshot.hasData) {
          final loading = Container(
            height: attachment.contentType.startsWith('image/') ? null : 62,
            margin: const EdgeInsets.only(top: 7),
            decoration: BoxDecoration(
                color: KaedeColors.raised,
                borderRadius: BorderRadius.circular(14)),
            child: const Center(child: CircularProgressIndicator()),
          );
          return attachment.contentType.startsWith('image/')
              ? AspectRatio(aspectRatio: imageRatio, child: loading)
              : loading;
        }
        final file = snapshot.data!;
        if (attachment.contentType.startsWith('image/')) {
          return AspectRatio(
            aspectRatio: imageRatio,
            child: Padding(
              padding: const EdgeInsets.only(top: 7),
              child: Semantics(
                button: true,
                label: 'Open image ${attachment.filename}',
                child: GestureDetector(
                  onLongPress: _showMediaActions,
                  onTap: () => showDialog<void>(
                    context: context,
                    builder: (dialogContext) => Dialog.fullscreen(
                      backgroundColor: Colors.black.withValues(alpha: .92),
                      child: Stack(
                        children: [
                          InteractiveViewer(
                            minScale: .5,
                            maxScale: 6,
                            child: Center(
                              child: Image.file(
                                file,
                                fit: BoxFit.contain,
                                errorBuilder: (_, __, ___) =>
                                    const _MediaDecodeError(kind: 'image'),
                              ),
                            ),
                          ),
                          Positioned(
                              top: 12,
                              right: 12,
                              child: IconButton.filled(
                                  tooltip: 'Close image viewer',
                                  onPressed: () => Navigator.pop(dialogContext),
                                  icon: const Icon(Icons.close_rounded))),
                        ],
                      ),
                    ),
                  ),
                  child: ClipRRect(
                    borderRadius: BorderRadius.circular(12),
                    child: Image.file(
                      file,
                      fit: BoxFit.contain,
                      width: double.infinity,
                      alignment: Alignment.centerLeft,
                      errorBuilder: (_, __, ___) =>
                          const _MediaDecodeError(kind: 'image'),
                    ),
                  ),
                ),
              ),
            ),
          );
        }
        if (attachment.contentType.startsWith('video/')) {
          return GestureDetector(
            onLongPress: _showMediaActions,
            child: _FileVideo(file: file),
          );
        }
        return ListTile(
          onLongPress: _showMediaActions,
          contentPadding: EdgeInsets.zero,
          leading: const Icon(Icons.insert_drive_file_outlined),
          title: Text(attachment.filename),
          subtitle: Text(formatAttachmentSize(attachment.size)),
        );
      },
    );
  }
}

final class _TypingIndicator extends StatelessWidget {
  const _TypingIndicator({required this.participants});

  final List<TypingParticipant> participants;

  @override
  Widget build(BuildContext context) {
    final names = participants.map((item) => item.name).toList();
    final label = switch (names.length) {
      1 => '${names.first} is typing…',
      2 => '${names[0]} and ${names[1]} are typing…',
      _ =>
        '${names[0]}, ${names[1]}, and ${names.length - 2} others are typing…',
    };
    return Semantics(
      liveRegion: true,
      label: label,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(_messageGutter, 3, 16, 3),
        child: Row(
          children: [
            const _TypingDots(),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                label,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(
                  color: KaedeColors.muted,
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

final class _FileVideo extends StatefulWidget {
  const _FileVideo({required this.file});
  final File file;
  @override
  State<_FileVideo> createState() => _FileVideoState();
}

final class _FileVideoState extends State<_FileVideo> {
  VideoPlayerController? controller;
  Object? _error;
  var _prepareGeneration = 0;

  @override
  void initState() {
    super.initState();
    unawaited(_prepare());
  }

  @override
  void didUpdateWidget(covariant _FileVideo oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.file.path != widget.file.path) unawaited(_prepare());
  }

  Future<void> _prepare() async {
    final generation = ++_prepareGeneration;
    _error = null;
    final previous = controller;
    controller = null;
    await previous?.dispose();
    final next = VideoPlayerController.file(widget.file);
    try {
      await next.initialize();
      if (!mounted || generation != _prepareGeneration) {
        await next.dispose();
        return;
      }
      setState(() => controller = next);
    } on Object catch (error) {
      await next.dispose();
      if (mounted && generation == _prepareGeneration) {
        setState(() => _error = error);
      }
    }
  }

  @override
  void dispose() {
    _prepareGeneration += 1;
    controller?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final player = controller;
    if (_error != null) {
      return Padding(
        padding: const EdgeInsets.only(top: 7),
        child: _MediaDecodeError(
          kind: 'video',
          onRetry: () {
            setState(() => _error = null);
            unawaited(_prepare());
          },
        ),
      );
    }
    if (player == null) {
      return const Padding(
          padding: EdgeInsets.all(18), child: LinearProgressIndicator());
    }
    return Padding(
      padding: const EdgeInsets.only(top: 7),
      child: ClipRRect(
        borderRadius: BorderRadius.circular(14),
        child: AspectRatio(
          aspectRatio: player.value.aspectRatio,
          child: Stack(
            alignment: Alignment.center,
            children: [
              VideoPlayer(player),
              IconButton.filled(
                onPressed: () => setState(() =>
                    player.value.isPlaying ? player.pause() : player.play()),
                icon: Icon(player.value.isPlaying
                    ? Icons.pause_rounded
                    : Icons.play_arrow_rounded),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

final class _MediaDecodeError extends StatelessWidget {
  const _MediaDecodeError({required this.kind, this.onRetry});

  final String kind;
  final VoidCallback? onRetry;

  @override
  Widget build(BuildContext context) => ColoredBox(
        color: KaedeColors.raised,
        child: Center(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(
                  Icons.broken_image_outlined,
                  color: KaedeColors.danger,
                ),
                const SizedBox(height: 8),
                Text(
                  'Could not display this $kind. The file may be damaged or use an unsupported format.',
                  textAlign: TextAlign.center,
                  style: const TextStyle(fontSize: 12),
                ),
                if (onRetry != null)
                  TextButton(onPressed: onRetry, child: const Text('Retry')),
              ],
            ),
          ),
        ),
      );
}

final class _MentionSuggestions extends StatelessWidget {
  const _MentionSuggestions({required this.users, required this.onSelected});

  final List<KaedeUser> users;
  final ValueChanged<KaedeUser> onSelected;

  @override
  Widget build(BuildContext context) {
    if (users.isEmpty) return const SizedBox.shrink();
    return Container(
      constraints: const BoxConstraints(maxHeight: 210),
      margin: const EdgeInsets.fromLTRB(8, 2, 8, 4),
      decoration: BoxDecoration(
        color: KaedeColors.panel,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: KaedeColors.border),
      ),
      child: ListView.builder(
        shrinkWrap: true,
        itemCount: users.length,
        itemBuilder: (context, index) {
          final user = users[index];
          return ListTile(
            dense: true,
            leading: UserAvatar(user: user, radius: 17),
            title: Text(user.name),
            subtitle: Text(user.profileResolved
                ? user.handle
                : 'Profile unavailable · refreshes automatically'),
            onTap: () => onSelected(user),
          );
        },
      ),
    );
  }
}

final class _Composer extends StatelessWidget {
  const _Composer(
      {required this.controller,
      required this.focusNode,
      required this.hint,
      required this.reply,
      required this.notifyReply,
      required this.uploads,
      required this.sending,
      required this.slowModeRemaining,
      required this.compact,
      required this.canAttach,
      required this.gifsAllowed,
      required this.onNotifyChanged,
      required this.onCancelReply,
      required this.onRemoveUpload,
      required this.onMore,
      required this.onAttach,
      required this.onEmoji,
      required this.onGif,
      required this.onSend});
  final TextEditingController controller;
  final FocusNode focusNode;
  final String hint;
  final KaedeMessage? reply;
  final bool notifyReply;
  final List<_PendingUpload> uploads;
  final bool sending;
  final Duration slowModeRemaining;

  /// Narrow phones collapse the attachment, emoji and GIF entry points into a
  /// single sheet so the text field keeps a usable width.
  final bool compact;
  final bool canAttach;
  final bool gifsAllowed;
  final ValueChanged<bool> onNotifyChanged;
  final VoidCallback onCancelReply;
  final ValueChanged<_PendingUpload> onRemoveUpload;
  final VoidCallback onMore;
  final VoidCallback onAttach;
  final VoidCallback onEmoji;
  final VoidCallback onGif;
  final VoidCallback onSend;

  @override
  Widget build(BuildContext context) {
    final coolingDown = slowModeRemaining > Duration.zero;
    final seconds = (slowModeRemaining.inMilliseconds / 1000).ceil();
    return SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(8, 4, 8, 8),
        child: DecoratedBox(
          decoration: BoxDecoration(
            color: KaedeColors.raised,
            borderRadius: BorderRadius.circular(24),
            border: Border.all(color: KaedeColors.border),
          ),
          child: Column(
            children: [
              if (reply case final message?)
                _ReplyingBar(
                  author: message.author?.name ?? 'Unknown author',
                  preview: spoilerSafeReplyPreview(
                    message.content ?? 'Attachment',
                  ),
                  notify: notifyReply,
                  onNotifyChanged: onNotifyChanged,
                  onClose: onCancelReply,
                ),
              if (uploads.isNotEmpty)
                SizedBox(
                  height: 78,
                  child: ListView.separated(
                    scrollDirection: Axis.horizontal,
                    padding: const EdgeInsets.fromLTRB(10, 10, 10, 4),
                    separatorBuilder: (_, __) => const SizedBox(width: 8),
                    itemCount: uploads.length,
                    itemBuilder: (_, index) {
                      final item = uploads[index];
                      return _UploadChip(
                        item: item,
                        onRemove: () => onRemoveUpload(item),
                      );
                    },
                  ),
                ),
              if (coolingDown)
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
                  child: Row(
                    children: [
                      const Icon(Icons.timer_outlined,
                          size: 15, color: KaedeColors.muted),
                      const SizedBox(width: 6),
                      Text(
                        'Slow mode · ${seconds}s remaining',
                        style: const TextStyle(
                          color: KaedeColors.muted,
                          fontSize: 12,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ],
                  ),
                ),
              Row(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  _ComposerButton(
                    icon: Icons.add_rounded,
                    tooltip: compact
                        ? 'Add files, emoji, or GIF'
                        : canAttach
                            ? 'Attach files'
                            : 'Attachments are not allowed here',
                    size: 22,
                    onPressed: sending || (!compact && !canAttach)
                        ? null
                        : compact
                            ? onMore
                            : onAttach,
                  ),
                  Expanded(
                    child: TextField(
                      controller: controller,
                      focusNode: focusNode,
                      minLines: 1,
                      maxLines: 5,
                      maxLength: 4000,
                      textCapitalization: TextCapitalization.sentences,
                      keyboardType: TextInputType.multiline,
                      textInputAction: TextInputAction.newline,
                      style: const TextStyle(fontSize: 15.5, height: 1.35),
                      decoration: InputDecoration(
                        hintText: hint,
                        isCollapsed: true,
                        filled: false,
                        contentPadding: const EdgeInsets.symmetric(
                          horizontal: 4,
                          vertical: 15,
                        ),
                        border: InputBorder.none,
                        enabledBorder: InputBorder.none,
                        focusedBorder: InputBorder.none,
                        counterText: '',
                      ),
                    ),
                  ),
                  if (!compact)
                    _ComposerButton(
                      icon: Icons.emoji_emotions_outlined,
                      tooltip: 'Emoji',
                      onPressed: sending ? null : onEmoji,
                    ),
                  if (!compact)
                    _ComposerButton(
                      icon: Icons.gif_box_outlined,
                      tooltip: gifsAllowed
                          ? 'GIFs'
                          : 'GIF search is unavailable in encrypted '
                              'conversations',
                      onPressed: sending ? null : onGif,
                      muted: !gifsAllowed,
                    ),
                  _ComposerSend(
                    controller: controller,
                    hasAttachments: uploads.isNotEmpty,
                    sending: sending,
                    enabled: !coolingDown,
                    onSend: onSend,
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// Circular composer affordance sized for comfortable one-handed reach.
final class _ComposerButton extends StatelessWidget {
  const _ComposerButton({
    required this.icon,
    required this.tooltip,
    required this.onPressed,
    this.size = 21,
    this.muted = false,
  });

  final IconData icon;
  final String tooltip;
  final VoidCallback? onPressed;
  final double size;
  final bool muted;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(3, 0, 3, 4),
        child: IconButton(
          tooltip: tooltip,
          onPressed: onPressed,
          visualDensity: VisualDensity.compact,
          constraints: const BoxConstraints.tightFor(width: 40, height: 40),
          padding: EdgeInsets.zero,
          style: IconButton.styleFrom(
            foregroundColor: muted ? KaedeColors.muted : KaedeColors.textSoft,
            shape: const CircleBorder(),
          ),
          icon: Icon(icon, size: size),
        ),
      );
}

/// The send button only appears once there is something to send, so an empty
/// composer stays out of the way.
final class _ComposerSend extends StatelessWidget {
  const _ComposerSend({
    required this.controller,
    required this.hasAttachments,
    required this.sending,
    required this.enabled,
    required this.onSend,
  });

  final TextEditingController controller;
  final bool hasAttachments;
  final bool sending;
  final bool enabled;
  final VoidCallback onSend;

  @override
  Widget build(BuildContext context) =>
      ValueListenableBuilder<TextEditingValue>(
        valueListenable: controller,
        builder: (context, value, _) {
          final ready =
              value.text.trim().isNotEmpty || hasAttachments || sending;
          return AnimatedSize(
            duration: const Duration(milliseconds: 160),
            curve: Curves.easeOutCubic,
            alignment: Alignment.centerLeft,
            child: ready
                ? Padding(
                    padding: const EdgeInsets.fromLTRB(2, 0, 5, 5),
                    child: IconButton.filled(
                      tooltip: 'Send message',
                      constraints: const BoxConstraints.tightFor(
                        width: 38,
                        height: 38,
                      ),
                      padding: EdgeInsets.zero,
                      style: IconButton.styleFrom(
                        backgroundColor: KaedeColors.coral,
                        foregroundColor: KaedeColors.onCoral,
                        disabledBackgroundColor: KaedeColors.hover,
                        disabledForegroundColor: KaedeColors.muted,
                      ),
                      onPressed: sending || !enabled ? null : onSend,
                      icon: sending
                          ? const SizedBox.square(
                              dimension: 16,
                              child: CircularProgressIndicator(
                                strokeWidth: 2,
                                color: KaedeColors.muted,
                              ),
                            )
                          : const Icon(Icons.arrow_upward_rounded, size: 20),
                    ),
                  )
                : const SizedBox(width: 6, height: 46),
          );
        },
      );
}

/// Placeholder text naming the conversation, like the web composer.
String composerHint(KaedeChannel channel) {
  if (channel.guildRef != null) {
    final name = channel.name?.trim();
    return 'Message #${name?.isNotEmpty == true ? name : 'channel'}';
  }
  if (channel.conversationType == 'group') {
    final name = channel.name?.trim();
    return name?.isNotEmpty == true ? 'Message $name' : 'Message the group';
  }
  final recipient =
      channel.recipients.isEmpty ? null : channel.recipients.first.name.trim();
  return recipient?.isNotEmpty == true ? 'Message $recipient' : 'Message';
}

final class _PermissionNotice extends StatelessWidget {
  const _PermissionNotice({required this.message});

  final String message;

  @override
  Widget build(BuildContext context) => SafeArea(
        top: false,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(8, 4, 8, 8),
          child: DecoratedBox(
            decoration: BoxDecoration(
              color: KaedeColors.panel,
              borderRadius: BorderRadius.circular(KaedeRadius.large),
              border: Border.all(color: KaedeColors.border),
            ),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 15),
              child: Row(
                children: [
                  const Icon(Icons.lock_outline_rounded,
                      size: 17, color: KaedeColors.muted),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      message,
                      style: const TextStyle(
                        color: KaedeColors.muted,
                        fontSize: 13,
                        height: 1.35,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      );
}

final class _ConversationStart extends StatelessWidget {
  const _ConversationStart({required this.channel});
  final KaedeChannel channel;

  @override
  Widget build(BuildContext context) {
    final guildChannel = channel.guildRef != null;
    final name = channel.name?.trim();
    final title = guildChannel
        ? '#${name?.isNotEmpty == true ? name : 'channel'}'
        : name?.isNotEmpty == true
            ? name!
            : channel.recipients.isEmpty
                ? 'this conversation'
                : channel.recipients.first.name;
    final topic = channel.topic?.trim();
    return Align(
      alignment: Alignment.bottomLeft,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(18, 30, 18, 20),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              width: 62,
              height: 62,
              decoration: BoxDecoration(
                color: KaedeColors.raised,
                shape: BoxShape.circle,
                border: Border.all(color: KaedeColors.border),
              ),
              child: Icon(
                guildChannel
                    ? Icons.tag_rounded
                    : channel.conversationType == 'group'
                        ? Icons.group_rounded
                        : Icons.alternate_email_rounded,
                size: 28,
                color: KaedeColors.muted,
              ),
            ),
            const SizedBox(height: 16),
            Text(
              guildChannel ? 'Welcome to $title' : title,
              style: Theme.of(context).textTheme.headlineMedium,
            ),
            const SizedBox(height: 6),
            Text(
              topic?.isNotEmpty == true
                  ? topic!
                  : guildChannel
                      ? 'This is the start of #${name ?? 'channel'}. Say hello.'
                      : 'This is the beginning of your conversation.',
              style: const TextStyle(color: KaedeColors.muted, height: 1.4),
            ),
          ],
        ),
      ),
    );
  }
}

final class _PendingUpload {
  const _PendingUpload(
      {required this.name,
      required this.file,
      required this.size,
      required this.contentType,
      required this.temporary});
  final String name;
  final File file;
  final int size;
  final String contentType;
  final bool temporary;

  Future<void> deleteIfTemporary() async {
    if (temporary && await file.exists()) await file.delete();
  }
}

/// Human readable attachment size, used by upload chips and attachment cards.
String formatAttachmentSize(int bytes) {
  if (bytes < 1024) return '$bytes B';
  final kilobytes = bytes / 1024;
  if (kilobytes < 1024) return '${kilobytes.toStringAsFixed(0)} KB';
  final megabytes = kilobytes / 1024;
  if (megabytes < 1024) {
    return '${megabytes.toStringAsFixed(megabytes < 10 ? 1 : 0)} MB';
  }
  return '${(megabytes / 1024).toStringAsFixed(1)} GB';
}

String _safeName(String filename) =>
    filename.replaceAll(RegExp(r'[^A-Za-z0-9._-]'), '_');

String _contentType(String filename) {
  final extension = filename.split('.').last.toLowerCase();
  return switch (extension) {
    'png' => 'image/png',
    'jpg' || 'jpeg' => 'image/jpeg',
    'gif' => 'image/gif',
    'webp' => 'image/webp',
    'mp4' => 'video/mp4',
    'webm' => 'video/webm',
    'mp3' => 'audio/mpeg',
    'ogg' => 'audio/ogg',
    'pdf' => 'application/pdf',
    _ => 'application/octet-stream',
  };
}

extension _FirstOrNull<T> on Iterable<T> {
  T? get firstOrNull => isEmpty ? null : first;
}
