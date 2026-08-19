import 'dart:async';
import 'dart:io';
import 'dart:math';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:file_picker/file_picker.dart';
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
          child: messages.isEmpty &&
                  pending.isEmpty &&
                  !state.loadingMessages &&
                  !channel.historyTruncated
              ? _ConversationStart(channel: channel)
              : ListView.builder(
                  controller: _scroll,
                  reverse: true,
                  padding: const EdgeInsets.fromLTRB(4, 4, 4, 14),
                  itemCount: messages.length +
                      pending.length +
                      (state.channelsWithOlderMessages.contains(channel.ref) ||
                              (channel.historyTruncated && messages.isEmpty) ||
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
                        padding: const EdgeInsets.fromLTRB(44, 4, 44, 10),
                        child: OutlinedButton.icon(
                          onPressed:
                              state.loadingMessages ? null : _loadEarlier,
                          icon: state.loadingMessages
                              ? const SizedBox.square(
                                  dimension: 16,
                                  child:
                                      CircularProgressIndicator(strokeWidth: 2))
                              : const Icon(Icons.history_rounded),
                          label: const Text('Load earlier messages'),
                        ),
                      );
                    }
                    final message = messages[messageIndex];
                    final previous =
                        messageIndex > 0 ? messages[messageIndex - 1] : null;
                    final compact = previous != null &&
                        previous.authorRef == message.authorRef &&
                        message.createdAt
                                .difference(previous.createdAt)
                                .inMinutes <
                            7;
                    final key = _messageKeys.putIfAbsent(
                        message.ref.wire, GlobalKey.new);
                    return KeyedSubtree(
                      key: key,
                      child: AnimatedContainer(
                        duration: const Duration(milliseconds: 220),
                        color: _highlightedMessage == message.ref
                            ? Theme.of(context)
                                .colorScheme
                                .primary
                                .withValues(alpha: .16)
                            : Colors.transparent,
                        child: _MessageTile(
                          state: state,
                          message: message,
                          compact: compact,
                          referenced: message.reference == null
                              ? null
                              : messages
                                  .where((candidate) =>
                                      candidate.ref == message.reference)
                                  .firstOrNull,
                          onReply: () => setState(() {
                            _reply = message;
                            _notifyReply =
                                message.authorRef != state.user?.ref &&
                                    channel.type != ChannelType.dm;
                          }),
                          onJump: message.reference == null
                              ? null
                              : () => _jumpTo(message.reference!),
                          onMenu: () => _showMessageActions(message),
                          onReaction: (emoji) =>
                              _toggleReaction(message, emoji),
                          onAuthorTap: message.author == null
                              ? null
                              : () => showUserProfile(
                                    context,
                                    message.author!,
                                    state.presenceByUser[message.author!.ref] ??
                                        message.author!.presence,
                                  ),
                        ),
                      ),
                    );
                  },
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
              reply: _reply,
              notifyReply: _notifyReply,
              uploads: _uploads,
              sending: _sending,
              slowModeRemaining: _slowModeRemaining(channel.ref),
              onNotifyChanged: (value) => setState(() => _notifyReply = value),
              onCancelReply: () => setState(() => _reply = null),
              onRemoveUpload: (item) {
                setState(() => _uploads.remove(item));
                unawaited(item.deleteIfTemporary());
              },
              onAdd: () => _showComposerActions(channel),
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
    if (!mounted ||
        action == null ||
        ref.read(mobileControllerProvider).activeChannel?.ref != channel.ref) {
      return;
    }
    switch (action) {
      case ComposerAction.attach:
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
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (canReact) ...[
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 0, 16, 6),
                child: Row(
                  children: [
                    for (final emoji in recent)
                      Expanded(
                        child: Padding(
                          padding: const EdgeInsets.symmetric(horizontal: 3),
                          child: FilledButton.tonal(
                            onPressed: () =>
                                Navigator.pop(context, 'reaction:$emoji'),
                            style: FilledButton.styleFrom(
                              padding: const EdgeInsets.symmetric(vertical: 12),
                            ),
                            child: Text(emoji,
                                style: const TextStyle(fontSize: 23)),
                          ),
                        ),
                      ),
                  ],
                ),
              ),
            ],
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
                  title: Text(message.pinned ? 'Unpin message' : 'Pin message'),
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
          ],
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
            final guild = ref.read(mobileControllerProvider).activeGuild;
            final route = guild == null
                ? '/home/${Uri.encodeComponent(channel.ref.wire)}'
                : '/channels/${Uri.encodeComponent(guild.ref.wire)}/'
                    '${Uri.encodeComponent(channel.ref.wire)}';
            await Clipboard.setData(ClipboardData(
              text: 'https://$instance$route#message-${message.ref.wire}',
            ));
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
        padding: const EdgeInsets.fromLTRB(14, 9, 14, 10),
        color: KaedeColors.coral.withValues(alpha: .12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(title,
                style: const TextStyle(
                    color: KaedeColors.coral, fontWeight: FontWeight.w800)),
            const SizedBox(height: 2),
            Text(message,
                style: const TextStyle(
                    color: KaedeColors.muted, fontSize: 12, height: 1.3)),
          ],
        ),
      );
}

final class _HistoryBoundary extends StatelessWidget {
  const _HistoryBoundary({required this.complete});

  final bool complete;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(28, 8, 28, 14),
        child: Column(
          children: [
            Text(
                complete
                    ? 'Beginning of conversation'
                    : 'Recent history starts here',
                textAlign: TextAlign.center,
                style: const TextStyle(fontWeight: FontWeight.w800)),
            const SizedBox(height: 3),
            Text(
              complete
                  ? 'You have reached the oldest message available from the conversation home.'
                  : 'This instance keeps a rolling cache of this remote conversation. Older messages load on demand from its home instance; retry if that instance is temporarily unavailable.',
              textAlign: TextAlign.center,
              style: const TextStyle(color: KaedeColors.muted, fontSize: 12),
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
    return Opacity(
      opacity: failed ? 1 : .58,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(62, 8, 8, 8),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (content.isNotEmpty) Text(content),
            Row(
              children: [
                Icon(
                  failed ? Icons.error_outline_rounded : Icons.schedule_rounded,
                  size: 16,
                  color: failed ? KaedeColors.danger : KaedeColors.muted,
                ),
                const SizedBox(width: 6),
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
                    ),
                  ),
                ),
                if (failed) ...[
                  TextButton(onPressed: onRetry, child: const Text('Retry')),
                  IconButton(
                    onPressed: onDiscard,
                    tooltip: 'Discard message',
                    icon: const Icon(Icons.close_rounded, size: 18),
                  ),
                ],
              ],
            ),
          ],
        ),
      ),
    );
  }
}

final class _ChatErrorStrip extends StatelessWidget {
  const _ChatErrorStrip({required this.message, required this.onRetry});

  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) => ColoredBox(
        color: KaedeColors.danger.withValues(alpha: .16),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(14, 8, 8, 8),
          child: Row(
            children: [
              const Icon(Icons.error_outline_rounded,
                  size: 18, color: KaedeColors.danger),
              const SizedBox(width: 9),
              Expanded(
                child: Text(
                  message,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: KaedeColors.danger,
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
              TextButton(onPressed: onRetry, child: const Text('Retry')),
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
    required this.onTap,
  });

  final String emoji;
  final int count;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(9),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
          decoration: BoxDecoration(
            color: KaedeColors.selected,
            borderRadius: BorderRadius.circular(9),
            border: Border.all(color: KaedeColors.border),
          ),
          child: Text(
            '$emoji  $count',
            style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w700),
          ),
        ),
      );
}

final class _TypingDots extends StatelessWidget {
  const _TypingDots();

  @override
  Widget build(BuildContext context) => Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          for (var index = 0; index < 3; index++) ...[
            if (index > 0) const SizedBox(width: 3),
            Container(
              width: 4,
              height: 4,
              decoration: const BoxDecoration(
                color: KaedeColors.muted,
                shape: BoxShape.circle,
              ),
            ),
          ],
        ],
      );
}

final class _ReplyingBar extends StatelessWidget {
  const _ReplyingBar({
    required this.author,
    required this.notify,
    required this.onNotifyChanged,
    required this.onClose,
  });

  final String author;
  final bool notify;
  final ValueChanged<bool> onNotifyChanged;
  final VoidCallback onClose;

  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.fromLTRB(13, 7, 5, 7),
        decoration: const BoxDecoration(
          border: Border(bottom: BorderSide(color: KaedeColors.border)),
        ),
        child: Row(
          children: [
            Expanded(
              child: Text.rich(
                TextSpan(
                  text: 'Replying to ',
                  style: const TextStyle(color: KaedeColors.muted),
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
            ),
            TextButton.icon(
              onPressed: () => onNotifyChanged(!notify),
              icon: Icon(
                  notify ? Icons.alternate_email : Icons.notifications_off,
                  size: 15),
              label: Text(notify ? 'ON' : 'OFF'),
              style: TextButton.styleFrom(
                visualDensity: VisualDensity.compact,
                padding: const EdgeInsets.symmetric(horizontal: 6),
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
  Widget build(BuildContext context) => Container(
        width: 176,
        padding: const EdgeInsets.fromLTRB(10, 5, 3, 5),
        decoration: BoxDecoration(
          color: KaedeColors.selected,
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: KaedeColors.border),
        ),
        child: Row(
          children: [
            const Icon(Icons.attach_file_rounded,
                size: 18, color: KaedeColors.muted),
            const SizedBox(width: 6),
            Expanded(
              child: Text(
                item.name,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style:
                    const TextStyle(fontSize: 12, fontWeight: FontWeight.w600),
              ),
            ),
            IconButton(
              onPressed: onRemove,
              tooltip: 'Remove attachment',
              visualDensity: VisualDensity.compact,
              icon: const Icon(Icons.close_rounded, size: 17),
            ),
          ],
        ),
      );
}

final class _MessageTile extends StatelessWidget {
  const _MessageTile(
      {required this.message,
      required this.state,
      required this.compact,
      required this.onReply,
      required this.onMenu,
      required this.onReaction,
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
  final VoidCallback? onAuthorTap;
  final VoidCallback? onJump;

  @override
  Widget build(BuildContext context) {
    if (message.messageType >= 3 && message.messageType <= 5) {
      final icon = switch (message.messageType) {
        3 => Icons.person_add_alt_1_rounded,
        4 => Icons.logout_rounded,
        _ => Icons.person_remove_alt_1_rounded,
      };
      return Padding(
        padding: const EdgeInsets.fromLTRB(14, 5, 14, 5),
        child: Row(
          children: [
            Container(
              width: 25,
              height: 25,
              decoration: const BoxDecoration(
                shape: BoxShape.circle,
                color: Color(0xFF4B302A),
              ),
              child: Icon(icon, size: 14, color: KaedeColors.coral),
            ),
            const SizedBox(width: 9),
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
    final author = message.author;
    final deleted = message.deletedAt != null;
    final mediaPreview = previewMediaUrl(message.content ?? '');
    return InkWell(
      onLongPress: onMenu,
      onDoubleTap: deleted ? null : onReply,
      borderRadius: BorderRadius.circular(6),
      child: Padding(
        padding: EdgeInsets.fromLTRB(8, compact ? 1 : 9, 8, 2),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              width: 40,
              child: compact
                  ? null
                  : author == null
                      ? const CircleAvatar(
                          backgroundColor: KaedeColors.raised,
                          child: Text('?'),
                        )
                      : GestureDetector(
                          onTap: onAuthorTap,
                          child: UserAvatar(user: author),
                        ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (!compact)
                    Row(
                      children: [
                        Flexible(
                            child: GestureDetector(
                          onTap: onAuthorTap,
                          child: Text(author?.name ?? 'Unknown author',
                              style: const TextStyle(
                                  fontWeight: FontWeight.w800, fontSize: 15)),
                        )),
                        const SizedBox(width: 7),
                        Text(
                            DateFormat.jm().format(message.createdAt.toLocal()),
                            style: const TextStyle(
                                color: KaedeColors.muted, fontSize: 12)),
                        if (message.editedAt != null)
                          const Text('  (edited)',
                              style: TextStyle(
                                  color: KaedeColors.muted, fontSize: 11)),
                      ],
                    ),
                  if (message.reference != null)
                    InkWell(
                      onTap: onJump,
                      borderRadius: BorderRadius.circular(6),
                      child: Padding(
                        padding: const EdgeInsets.only(top: 3, bottom: 2),
                        child: Row(
                          children: [
                            const Icon(Icons.reply_rounded,
                                size: 14, color: KaedeColors.muted),
                            const SizedBox(width: 5),
                            Text(referenced?.author?.name ?? 'Original message',
                                style: const TextStyle(
                                    fontWeight: FontWeight.w700, fontSize: 13)),
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
                                        fontSize: 13))),
                          ],
                        ),
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
                    const Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Icon(
                          Icons.lock_outline_rounded,
                          size: 17,
                          color: KaedeColors.muted,
                        ),
                        SizedBox(width: 7),
                        Expanded(
                          child: Text(
                            'Can’t decrypt this message on this device. Verify, recover, or update this device’s encryption support.',
                            style: TextStyle(
                              color: KaedeColors.muted,
                              fontSize: 14,
                              height: 1.25,
                            ),
                          ),
                        ),
                      ],
                    )
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
                      padding: const EdgeInsets.only(top: 6),
                      child: Wrap(
                        spacing: 6,
                        runSpacing: 4,
                        children: [
                          for (final reaction in message.reactionCounts.entries)
                            _ReactionChip(
                              emoji: reaction.key,
                              count: reaction.value,
                              onTap: () => onReaction(reaction.key),
                            ),
                        ],
                      ),
                    ),
                  if (message.deliveryStatus == 'failed')
                    Text(message.failureReason ?? 'Message not delivered.',
                        style: const TextStyle(
                            color: KaedeColors.danger,
                            fontWeight: FontWeight.w700)),
                  if (message.deliveryStatus == 'retrying')
                    Text(
                      message.failureReason ??
                          'The receiving instance is temporarily at capacity. Kaede is retrying automatically.',
                      style: const TextStyle(
                        color: KaedeColors.muted,
                        fontWeight: FontWeight.w600,
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
              padding: const EdgeInsets.symmetric(horizontal: 3, vertical: 1),
              decoration: BoxDecoration(
                color: _revealed ? KaedeColors.raised : KaedeColors.text,
                borderRadius: BorderRadius.circular(4),
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
                '${(widget.attachment.size / 1024).ceil()} KB',
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
          subtitle: Text('${(attachment.size / 1024).ceil()} KB'),
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
        padding: const EdgeInsets.fromLTRB(18, 2, 18, 0),
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
      required this.reply,
      required this.notifyReply,
      required this.uploads,
      required this.sending,
      required this.slowModeRemaining,
      required this.onNotifyChanged,
      required this.onCancelReply,
      required this.onRemoveUpload,
      required this.onAdd,
      required this.onSend});
  final TextEditingController controller;
  final FocusNode focusNode;
  final KaedeMessage? reply;
  final bool notifyReply;
  final List<_PendingUpload> uploads;
  final bool sending;
  final Duration slowModeRemaining;
  final ValueChanged<bool> onNotifyChanged;
  final VoidCallback onCancelReply;
  final ValueChanged<_PendingUpload> onRemoveUpload;
  final VoidCallback onAdd;
  final VoidCallback onSend;

  @override
  Widget build(BuildContext context) {
    final coolingDown = slowModeRemaining > Duration.zero;
    final seconds = (slowModeRemaining.inMilliseconds / 1000).ceil();
    return SafeArea(
      top: false,
      child: Container(
        margin: const EdgeInsets.fromLTRB(8, 3, 8, 7),
        decoration: BoxDecoration(
            color: KaedeColors.raised,
            borderRadius: BorderRadius.circular(23),
            border: Border.all(color: KaedeColors.border)),
        child: Column(
          children: [
            if (reply case final message?)
              _ReplyingBar(
                author: message.author?.name ?? 'Unknown author',
                notify: notifyReply,
                onNotifyChanged: onNotifyChanged,
                onClose: onCancelReply,
              ),
            if (uploads.isNotEmpty)
              SizedBox(
                height: 68,
                child: ListView.separated(
                  scrollDirection: Axis.horizontal,
                  padding: const EdgeInsets.all(8),
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
                padding: const EdgeInsets.fromLTRB(14, 7, 14, 0),
                child: Row(
                  children: [
                    const Icon(Icons.timer_outlined,
                        size: 16, color: KaedeColors.muted),
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
                Padding(
                  padding: const EdgeInsets.only(left: 5, bottom: 3),
                  child: IconButton.filledTonal(
                      constraints:
                          const BoxConstraints.tightFor(width: 38, height: 38),
                      padding: EdgeInsets.zero,
                      tooltip: 'Add files, emoji, or GIF',
                      onPressed: sending ? null : onAdd,
                      icon: const Icon(Icons.add_rounded, size: 23)),
                ),
                Expanded(
                  child: TextField(
                    controller: controller,
                    focusNode: focusNode,
                    minLines: 1,
                    maxLines: 5,
                    maxLength: 4000,
                    textCapitalization: TextCapitalization.sentences,
                    decoration: const InputDecoration(
                        hintText: 'Message',
                        contentPadding:
                            EdgeInsets.symmetric(horizontal: 10, vertical: 13),
                        border: InputBorder.none,
                        focusedBorder: InputBorder.none,
                        counterText: ''),
                  ),
                ),
                Padding(
                  padding: const EdgeInsets.only(right: 5, bottom: 3),
                  child: IconButton.filled(
                    constraints:
                        const BoxConstraints.tightFor(width: 40, height: 40),
                    padding: EdgeInsets.zero,
                    onPressed: sending || coolingDown ? null : onSend,
                    icon: sending
                        ? const SizedBox.square(
                            dimension: 17,
                            child: CircularProgressIndicator(strokeWidth: 2))
                        : const Icon(Icons.arrow_upward_rounded, size: 21),
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

final class _PermissionNotice extends StatelessWidget {
  const _PermissionNotice({required this.message});

  final String message;

  @override
  Widget build(BuildContext context) => SafeArea(
        top: false,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(8, 3, 8, 7),
          child: DecoratedBox(
            decoration: const BoxDecoration(
              color: KaedeColors.raised,
              borderRadius: BorderRadius.all(Radius.circular(18)),
              border: Border.fromBorderSide(
                BorderSide(color: KaedeColors.border),
              ),
            ),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 15, vertical: 14),
              child: Row(
                children: [
                  const Icon(Icons.lock_outline_rounded,
                      size: 18, color: KaedeColors.muted),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      message,
                      style: const TextStyle(color: KaedeColors.muted),
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
  Widget build(BuildContext context) => Align(
        alignment: Alignment.bottomLeft,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(18, 28, 18, 22),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const CircleAvatar(radius: 30, child: Icon(Icons.tag_rounded)),
              const SizedBox(height: 14),
              Text('Welcome to ${channel.name ?? 'this conversation'}',
                  style: Theme.of(context).textTheme.headlineMedium,
                  textAlign: TextAlign.left),
              const SizedBox(height: 8),
              const Text('This is the beginning of the conversation.',
                  style: TextStyle(color: KaedeColors.muted)),
            ],
          ),
        ),
      );
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
