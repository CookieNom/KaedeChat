import 'dart:async';
import 'dart:developer';
import 'dart:io';
import 'dart:math';

import 'package:audioplayers/audioplayers.dart';
import 'package:cached_network_image/cached_network_image.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:intl/intl.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/api/media_urls.dart';
import 'package:kaede_mobile/src/app/mobile_controller.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/announcements.dart';
import 'package:kaede_mobile/src/domain/application_commands.dart';
import 'package:kaede_mobile/src/domain/application_directory.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/domain/reaction_emoji.dart';
import 'package:kaede_mobile/src/domain/reaction_management.dart';
import 'package:kaede_mobile/src/domain/rich_content.dart';
import 'package:kaede_mobile/src/domain/role_colors.dart';
import 'package:kaede_mobile/src/domain/text_to_speech.dart';
import 'package:kaede_mobile/src/domain/thread_permissions.dart';
import 'package:kaede_mobile/src/domain/voice_messages.dart';
import 'package:kaede_mobile/src/e2ee/client.dart';
import 'package:kaede_mobile/src/e2ee/media.dart';
import 'package:kaede_mobile/src/features/chat/application_launcher.dart';
import 'package:kaede_mobile/src/features/chat/composer_pickers.dart';
import 'package:kaede_mobile/src/features/chat/swipe_to_reply.dart';
import 'package:kaede_mobile/src/features/chat/voice_message_recorder.dart';
import 'package:kaede_mobile/src/features/shared/developer_mode.dart';
import 'package:kaede_mobile/src/features/shared/remote_media.dart';
import 'package:kaede_mobile/src/features/tracker/tracker_channel_view.dart';
import 'package:kaede_mobile/src/features/voice/voice_room.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/storage/local_database.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';
import 'package:markdown/markdown.dart' as md;
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:video_player/video_player.dart';

export 'package:kaede_mobile/src/domain/application_commands.dart';

final _mentionPattern = RegExp(r'<@([1-9][0-9]{0,18}@[A-Za-z0-9.-]+)>');
final _urlPattern = RegExp(r'https?://[^\s<>"\u0027]+', caseSensitive: false);
const _messageSpoilerPattern = r'\|\|([^|](?:(?!\|\|)[\s\S])*?)\|\|';
const _messageTokenPattern =
    r'(<a?:[A-Za-z0-9_]{2,32}:[1-9][0-9]{0,18}@[A-Za-z0-9.-]{1,253}>|<@&[1-9][0-9]{0,18}@[A-Za-z0-9.-]+>|<@[1-9][0-9]{0,18}(?:@[A-Za-z0-9.-]+)?>|@[A-Za-z0-9_.-]{1,64}@[A-Za-z0-9.-]+|#[A-Za-z0-9_-]{1,100})';
final _messageSpoilerRegExp = RegExp(_messageSpoilerPattern);
final _messageTokenRegExp = RegExp(_messageTokenPattern);

String _interactionAcknowledgementRef(Map<String, Object?> acknowledged) {
  final rawRef = acknowledged['interaction_ref'];
  final rawId = acknowledged['id'];
  try {
    final ref = EntityRef.fromJson(rawRef);
    if ('$rawId' != ref.id.value) throw const FormatException();
    return ref.wire;
  } on FormatException {
    throw UserInputException(
      'The application returned an invalid federated interaction identity.',
    );
  }
}

bool canCreatePollInChannel(KaedeChannel channel) =>
    !channel.archived &&
    (!channel.locked || canManageThreads(channel)) &&
    (channel.guildRef == null || channel.allows(Permission.sendPolls));

bool _canSendUserContextCommands(KaedeChannel channel) =>
    channel.guildRef == null ||
    (channel.isThread
        ? canSendInThread(channel)
        : channel.allows(Permission.sendMessages));

bool canPinMessage(KaedeChannel channel, KaedeMessage message) =>
    canPinMessages(channel) &&
    const <int>{0, 19, 20, 23}.contains(message.messageType);

String? forwardMessageUnavailableReason(KaedeMessage message) {
  if (message.poll != null) return 'Poll messages cannot be forwarded.';
  if (message.messageType == 3) return 'Call messages cannot be forwarded.';
  if (!const <int>{0, 19, 20, 23}.contains(message.messageType)) {
    return 'System messages cannot be forwarded.';
  }
  if (message.e2ee != null &&
      (!message.e2eeVerified ||
          message.e2ee?['forward_projection_version'] != 2 ||
          message.e2ee?['forward_projection_digest'] is! String)) {
    return 'This encrypted snapshot is not verified for forwarding.';
  }
  return null;
}

String channelFollowSystemMessageText(
  KaedeMessage message,
  Iterable<KaedeChannel> channels,
) {
  final source = message.followedChannelRef;
  KaedeChannel? resolved;
  if (source != null) {
    for (final channel in channels) {
      if (channel.ref == source) {
        resolved = channel;
        break;
      }
    }
  }
  final resolvedName = resolved?.name?.trim();
  final contentName = message.content?.trim();
  final name = resolvedName?.isNotEmpty == true
      ? resolvedName!
      : contentName?.isNotEmpty == true
          ? contentName!
          : null;
  final label = name != null
      ? (name.startsWith('#') ? name : '#$name')
      : source != null
          ? '#${source.wire}'
          : 'an announcement channel';
  return '${message.author?.name ?? 'A member'} has added $label to this channel. '
      'Its most important updates will show up here.';
}

bool _canForwardMessageToResolvedChannel(
  KaedeChannel source,
  KaedeChannel target,
  bool sourceNsfw,
  bool targetNsfw,
) {
  if ((sourceNsfw && !targetNsfw) ||
      target.archived ||
      (target.locked && !canManageThreads(target))) {
    return false;
  }
  if (target.guildRef == null) {
    return target.type == ChannelType.dm || target.type == ChannelType.groupDm;
  }
  return switch (target.type) {
    ChannelType.text ||
    ChannelType.voice ||
    ChannelType.stage ||
    ChannelType.announcement =>
      target.allows(Permission.sendMessages),
    ChannelType.announcementThread ||
    ChannelType.publicThread ||
    ChannelType.privateThread =>
      canSendInThread(target),
    _ => false,
  };
}

bool canForwardMessageToChannel(
  KaedeChannel source,
  KaedeChannel target,
) =>
    _canForwardMessageToResolvedChannel(
      source,
      target,
      source.nsfw,
      target.nsfw,
    );

bool? _effectiveForwardNsfw(
  KaedeChannel channel,
  Map<EntityRef, KaedeChannel> channels,
) {
  if (channel.guildRef == null) return false;
  if (!channel.isThread) return channel.nsfw;
  final parent = channel.parentRef;
  return parent == null ? null : channels[parent]?.nsfw;
}

List<KaedeChannel> forwardDestinationChannels(
  MobileState state,
  KaedeChannel source,
) {
  final candidates = <KaedeChannel>[
    ...state.dms,
    ...state.guilds.expand((guild) => guild.channels),
    ...state.threads,
  ];
  final byRef = <EntityRef, KaedeChannel>{
    for (final channel in candidates) channel.ref: channel,
    source.ref: source,
  };
  final sourceNsfw = _effectiveForwardNsfw(source, byRef);
  if (sourceNsfw == null) return const <KaedeChannel>[];
  final unique = <EntityRef, KaedeChannel>{
    for (final channel in candidates)
      if (_effectiveForwardNsfw(channel, byRef) case final targetNsfw?)
        if (_canForwardMessageToResolvedChannel(
          source,
          channel,
          sourceNsfw,
          targetNsfw,
        ))
          channel.ref: channel,
  }.values.toList();
  unique.sort((left, right) {
    if (left.ref == source.ref) return -1;
    if (right.ref == source.ref) return 1;
    return (left.name ?? '').toLowerCase().compareTo(
          (right.name ?? '').toLowerCase(),
        );
  });
  return List.unmodifiable(unique);
}

int _channelTypeWireValue(ChannelType type) => switch (type) {
      ChannelType.text => 0,
      ChannelType.dm => 1,
      ChannelType.groupDm => 3,
      ChannelType.voice => 2,
      ChannelType.stage => 13,
      ChannelType.category => 4,
      ChannelType.announcement => 5,
      ChannelType.announcementThread => 10,
      ChannelType.publicThread => 11,
      ChannelType.privateThread => 12,
      ChannelType.forum => 15,
      ChannelType.tracker => 17,
      ChannelType.unknown => -1,
    };

const _privateAttachmentStates = <String>{
  'pending',
  'clean',
  'rejected',
  'infected',
  'failed',
};
final _privateAttachmentContentType =
    RegExp(r'^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$', caseSensitive: false);

String _privateAttachmentFilename(Object? value, String id) {
  final cleaned =
      value is String ? value.replaceAll(RegExp(r'[\x00-\x1f\x7f]'), '') : '';
  final parts = cleaned.split(RegExp(r'[/\\]'));
  final leaf = (parts.isEmpty ? '' : parts.last).trim();
  return leaf.isEmpty ? 'attachment-$id' : leaf.characters.take(255).toString();
}

/// Safely decodes the attachment projection carried by a private interaction
/// event. Malformed entries never become media URLs or local filenames.
List<KaedeAttachment> interactionResponseAttachments(
  Map<String, Object?> data,
) {
  final raw = data['attachments'];
  if (raw is! List) return const <KaedeAttachment>[];
  final attachments = <KaedeAttachment>[];
  final seen = <EntityRef>{};
  for (final value in raw.take(10)) {
    if (value is! Map) continue;
    final item = value.map((key, value) => MapEntry('$key', value));
    final id = '${item['id'] ?? ''}';
    final domain = '${item['origin_domain'] ?? ''}';
    try {
      final ref = EntityRef(Snowflake(id), Domain(domain));
      if (!seen.add(ref)) continue;
      final encrypted = item['encryption_mode'] == 'e2ee';
      final manifest = item['encrypted_manifest'];
      if (encrypted &&
          (manifest is! Map ||
              manifest['version'] != 1 ||
              manifest['protocol'] != 'kaede-file-v1' ||
              '${manifest['attachment_id']}' != id ||
              '${manifest['attachment_domain']}' != domain)) {
        continue;
      }
      final rawStatus = '${item['scan_status'] ?? 'failed'}';
      final status = encrypted
          ? 'encrypted'
          : _privateAttachmentStates.contains(rawStatus)
              ? rawStatus
              : 'failed';
      final rawContentType = '${item['content_type'] ?? ''}';
      final contentType = _privateAttachmentContentType.hasMatch(rawContentType)
          ? rawContentType.toLowerCase()
          : 'application/octet-stream';
      final size = switch (item['size']) {
        final num value when value >= 0 => value.toInt(),
        _ => 0,
      };
      int? positiveDimension(Object? value) => switch (value) {
            final num dimension when dimension > 0 && dimension <= 100000 =>
              dimension.toInt(),
            _ => null,
          };
      attachments.add(KaedeAttachment.fromJson(<String, Object?>{
        'id': ref.id.value,
        'origin_domain': ref.domain.value,
        'filename': _privateAttachmentFilename(item['filename'], id),
        'content_type': contentType,
        'size': size,
        'width': positiveDimension(item['width']),
        'height': positiveDimension(item['height']),
        'blurhash': item['blurhash'] is String
            ? (item['blurhash']! as String).characters.take(128).toString()
            : null,
        'scan_status': status,
        'private_media_url': item['private_media_url'],
      }));
    } on FormatException {
      // Ignore only the malformed attachment, not the private response.
    }
  }
  return List.unmodifiable(attachments);
}

Map<String, Object?>? interactionResponseEncryptedManifest(
  Map<String, Object?> data,
  KaedeAttachment attachment,
) {
  final raw = data['attachments'];
  if (raw is! List) return null;
  for (final value in raw.take(10)) {
    if (value is! Map || value['encrypted_manifest'] is! Map) continue;
    if ('${value['id']}@${value['origin_domain']}' != attachment.ref.wire) {
      continue;
    }
    return Map<String, Object?>.unmodifiable(
      Map<String, Object?>.from(value['encrypted_manifest']! as Map),
    );
  }
  return null;
}

/// Ephemeral callbacks retain Discord's poll-create shape because there is no
/// private vote route. Supply deterministic answer IDs and empty counts solely
/// so the shared poll card can present the question read-only.
RichPoll? interactionResponsePoll(Map<String, Object?> data) {
  if (data['poll'] is! Map) return null;
  final poll = Map<String, Object?>.from(data['poll']! as Map);
  final question = poll['question'] is Map
      ? Map<String, Object?>.from(poll['question']! as Map)
      : const <String, Object?>{};
  if ('${question['text'] ?? ''}'.trim().isEmpty || poll['answers'] is! List) {
    return null;
  }
  final answers = <Map<String, Object?>>[];
  final answerIds = <int>{};
  for (final (index, value) in (poll['answers']! as List).indexed.take(10)) {
    if (value is! Map) continue;
    final answer = Map<String, Object?>.from(value);
    if (answer['poll_media'] is! Map) continue;
    final media = Map<String, Object?>.from(answer['poll_media']! as Map);
    if ('${media['text'] ?? ''}'.trim().isEmpty && media['emoji'] is! Map) {
      continue;
    }
    final supplied = switch (answer['answer_id']) {
      final num id when id > 0 => id.toInt(),
      final Object id => int.tryParse('$id'),
      null => null,
    };
    final answerId = supplied != null && supplied > 0 ? supplied : index + 1;
    if (!answerIds.add(answerId)) return null;
    answers.add(<String, Object?>{
      'answer_id': answerId,
      'poll_media': media,
    });
  }
  if (answers.length < 2) return null;
  final result = poll['results'] is Map
      ? Map<String, Object?>.from(poll['results']! as Map)
      : const <String, Object?>{};
  final rawCounts = result['answer_counts'] is List
      ? result['answer_counts']! as List
      : const <Object>[];
  final counts = <int, Map<String, Object?>>{};
  for (final value in rawCounts) {
    if (value is! Map) continue;
    final count = Map<String, Object?>.from(value);
    final id = switch (count['id']) {
      final num value => value.toInt(),
      final Object value => int.tryParse('$value'),
      null => null,
    };
    if (id != null && answerIds.contains(id)) counts[id] = count;
  }
  try {
    return RichPoll.fromJson(<String, Object?>{
      'question': question,
      'answers': answers,
      'expiry': poll['expiry'],
      'allow_multiselect': poll['allow_multiselect'] == true,
      'layout_type': 1,
      'results': <String, Object?>{
        'is_finalized': result['is_finalized'] == true,
        'answer_counts': <Object?>[
          for (final answer in answers)
            counts[answer['answer_id']] ??
                <String, Object?>{
                  'id': answer['answer_id'],
                  'count': 0,
                  'me_voted': false,
                },
        ],
      },
    });
  } on FormatException {
    return null;
  }
}

final class NativeThreadCommand {
  NativeThreadCommand({required this.name, required this.message});

  final String name;
  final String message;
}

/// Text representation used by the mobile composer for Discord's native
/// `/thread name message` command options.
NativeThreadCommand? parseNativeThreadCommand(String input) {
  final match = RegExp(
    r'^/thread\s+name:(?:"([^"]+)"|(.+?))\s+message:(?:"([\s\S]*)"|([\s\S]+))$',
    caseSensitive: false,
  ).firstMatch(input.trim());
  if (match == null) return null;
  final name = (match.group(1) ?? match.group(2) ?? '').trim();
  final message = (match.group(3) ?? match.group(4) ?? '').trim();
  if (name.isEmpty ||
      name.length > 100 ||
      message.isEmpty ||
      message.length > 4000) {
    return null;
  }
  return NativeThreadCommand(name: name, message: message);
}

@visibleForTesting
List<KaedeMessage> threadTimelineMessages(
  KaedeChannel channel,
  List<KaedeMessage> messages,
) {
  final starter = channel.isThread ? channel.starterMessage : null;
  if (starter == null ||
      messages.any((message) => message.ref == starter.ref)) {
    return messages;
  }
  return List<KaedeMessage>.unmodifiable(<KaedeMessage>[starter, ...messages]);
}

/// Discord type-21 thread starters carry the source message only in
/// `referenced_message`. A missing projection is a deliberate unavailable
/// state, never an empty message.
@visibleForTesting
KaedeMessage threadStarterDisplayMessage(KaedeMessage message) {
  if (message.messageType != 21) return message;
  return message.referencedMessage ??
      message.copyWith(contentUnavailable: true);
}

@visibleForTesting
RichPollResultMessage? resolvedMessagePollResult(
  KaedeMessage message,
  KaedeMessage? referenced,
) {
  final result = message.pollResult;
  if (result == null) return null;
  final source = referenced ?? message.referencedMessage;
  if (source?.ref == result.pollMessageRef &&
      ((result.sourceEncryptionMode == 'e2ee') != (source?.e2ee != null))) {
    return null;
  }
  if (result.sourceEncryptionMode != 'e2ee') return result;
  if (source == null ||
      source.ref != result.pollMessageRef ||
      source.e2ee == null ||
      !source.e2eeVerified ||
      source.poll == null) {
    return result;
  }
  return result.withVerifiedPoll(source.poll!);
}

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
final _stickerToken = RegExp(
  r'^<sticker:([A-Za-z0-9_]{2,32}):([1-9][0-9]{0,18})@([A-Za-z0-9.-]{1,253})>$',
);

@visibleForTesting
({String name, EntityRef ref})? messageSticker(String? content) {
  final match =
      content == null ? null : _stickerToken.firstMatch(content.trim());
  if (match == null) return null;
  try {
    return (
      name: match.group(1)!,
      ref: EntityRef(Snowflake(match.group(2)!), Domain(match.group(3)!)),
    );
  } on FormatException {
    return null;
  }
}

@visibleForTesting
List<({String name, EntityRef ref})> messageStickers(KaedeMessage message) {
  if (message.stickerItems.isNotEmpty) {
    return message.stickerItems
        .map((item) => (name: item.name, ref: item.ref))
        .toList(growable: false);
  }
  final legacy = messageSticker(message.content);
  return legacy == null ? const [] : <({String name, EntityRef ref})>[legacy];
}

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
  bool requiresAttachmentDisclosure = false,
  bool attachmentDisclosureAvailable = true,
}) =>
    (message.e2ee == null ||
        (encryptedReportEvidenceAvailable(message) &&
            disclosureAcknowledged)) &&
    (!requiresAttachmentDisclosure ||
        (attachmentDisclosureAvailable && disclosureAcknowledged));

typedef _OpenAttachmentActions = Future<void> Function(
  KaedeAttachment attachment,
  Map<String, Object?>? manifest,
  File? decryptedFile,
);

const _automaticHistoryLoadThreshold = 320.0;
const _defaultRecentReactions = <String>['❤', '😂', '👍', '🔥'];

List<String> rankRecentReactions(List<String> history, {int limit = 4}) {
  if (limit <= 0) return const <String>[];
  final counts = <String, int>{};
  final lastUsed = <String, int>{};
  for (var index = 0; index < history.length; index++) {
    final emoji = tryParseReactionEmoji(history[index])?.value;
    if (emoji == null) continue;
    counts[emoji] = (counts[emoji] ?? 0) + 1;
    lastUsed[emoji] = index;
  }
  if (counts.isEmpty) return _defaultRecentReactions.take(limit).toList();
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

Uri? automaticMessageMediaPreview(
  String? content, {
  required bool encrypted,
}) =>
    encrypted || content == null ? null : previewMediaUrl(content);

String? previewableMessageLink(String? content) {
  if (content == null) return null;
  final visibleContent = content.replaceAll(_messageSpoilerRegExp, ' ');
  for (final match in _urlPattern.allMatches(visibleContent)) {
    final raw = match.group(0)!.replaceFirst(RegExp(r'[),.!?:;\]}]+$'), '');
    final uri = Uri.tryParse(raw);
    if (uri == null ||
        !const <String>{'http', 'https'}.contains(uri.scheme) ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        uri.pathSegments.firstOrNull == 'invite') {
      continue;
    }
    if (previewMediaUrl(raw) != null) continue;
    return uri.toString();
  }
  return null;
}

String? automaticMessageLinkPreview(
  String? content, {
  required bool encrypted,
}) =>
    encrypted ? null : previewableMessageLink(content);

String spoilerSafeReplyPreview(String content) =>
    content.replaceAll(_messageSpoilerRegExp, 'Spoiler');

String replyReferencePreview(KaedeMessage? referenced) {
  if (referenced == null) return 'Tap to load message';
  if (!referenced.clientContentAvailable) {
    return 'Encrypted message unavailable';
  }
  return spoilerSafeReplyPreview(referenced.content ?? 'Attachment');
}

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

  // Single shared instance: the markdown body re-parses when the style sheet
  // is not equal, and value-comparing this on every row rebuild would be
  // wasted work. Identity-equal instances short-circuit the check.
  static MarkdownStyleSheet _styleSheet(BuildContext context) =>
      MarkdownStyleSheet(
        p: TextStyle(
          color: context.kaede.text,
          fontSize: 16,
          height: 1.24,
        ),
        pPadding: EdgeInsets.zero,
        code: TextStyle(
          color: context.kaede.text,
          backgroundColor: context.kaede.rail,
          fontSize: 14,
        ),
        codeblockDecoration: BoxDecoration(
          color: context.kaede.rail,
          borderRadius: BorderRadius.circular(6),
          border: Border.all(color: context.kaede.border),
        ),
        blockquoteDecoration: BoxDecoration(
          border: Border(
            left: BorderSide(
              color: context.kaede.muted,
              width: 3,
            ),
          ),
        ),
      );

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
      // Selectable text renders a SelectableText, which on mobile installs a
      // horizontal drag recognizer for selection dragging. Sitting deeper than
      // the row's reply gesture, it silently swallowed every swipe that started
      // on message text — the largest part of a row. Long-press exposes both
      // 'Copy text' and 'Copy message link' instead.
      selectable: false,
      softLineBreak: true,
      styleSheet: _styleSheet(context),
      onTapLink: (_, href, __) async {
        final uri = Uri.tryParse(href ?? '');
        if (uri != null && (uri.scheme == 'https' || uri.scheme == 'http')) {
          await launchUrl(uri, mode: LaunchMode.externalApplication);
        }
      },
    );
  }
}

final class _StickerMessage extends StatelessWidget {
  const _StickerMessage({required this.sticker, this.size = 240});

  final ({String name, EntityRef ref}) sticker;
  final double size;

  @override
  Widget build(BuildContext context) => Semantics(
        image: true,
        label: 'Sticker: ${sticker.name}',
        child: CachedNetworkImage(
          key: ValueKey('message-sticker-${sticker.ref.wire}'),
          imageUrl: Uri.https(
            sticker.ref.domain.value,
            '/media/stickers/${sticker.ref.id.value}/thumbnail_512',
          ).toString(),
          width: size,
          height: size,
          fit: BoxFit.contain,
          alignment: Alignment.centerLeft,
          placeholder: (_, __) => SizedBox(
            width: size,
            height: size,
            child: Center(
              child: CircularProgressIndicator(strokeWidth: 2),
            ),
          ),
          errorWidget: (_, __, ___) => SizedBox(
            width: size,
            height: 80,
            child: Align(
              alignment: Alignment.centerLeft,
              child: Text(':${sticker.name}:'),
            ),
          ),
        ),
      );
}

typedef _CommandAutocompleteRequest = ({
  int generation,
  String path,
  Completer<List<MobileApplicationCommandChoice>> waiter,
});
typedef _CommandEntityChoice = ({String value, String label});
typedef _CommandChannelChoice = ({String value, String label, int type});
typedef _CommandAttachmentChoice = ({String key, String label});
typedef _PreparedCommandFiles = ({
  CommandComposerValues values,
  List<_PendingUpload> uploads,
  Map<String, Map<String, Object?>> manifests,
});
typedef _ModalInteractionSubmission = ({
  List<Map<String, Object?>> components,
  List<String> attachmentIds,
  Map<String, Map<String, Object?>> manifests,
});

typedef _ActiveEphemeralResponse = ({
  String interactionRef,
  List<MobileInteractionResponse> responses,
  MobileInteractionRequest request,
  MobileState state,
});

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
  String? _commandQuery;
  EntityRef? _commandsChannel;
  List<MobileApplicationCommand> _applicationCommands = const [];
  final _autocompleteRequests = <String, _CommandAutocompleteRequest>{};
  int? _handledJumpGeneration;
  EntityRef? _highlightedMessage;
  Timer? _highlightTimer;
  final _handledAutocompleteResponses = <String>{};
  final _publishingMessages = <EntityRef>{};
  ValueNotifier<_ActiveEphemeralResponse?>? _activeEphemeralResponse;

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
    for (final request in _autocompleteRequests.values) {
      if (!request.waiter.isCompleted) {
        request.waiter.complete(const <MobileApplicationCommandChoice>[]);
      }
    }
    _autocompleteRequests.clear();
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
    _activeEphemeralResponse?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(mobileControllerProvider);
    _scheduleInteractionResponses(state);
    final channel = state.activeChannel;
    if (channel == null) {
      return Center(child: Text('Choose a conversation.'));
    }
    if (channel.type == ChannelType.tracker) {
      return TrackerChannelView(channel: channel);
    }
    final composerReady = _composerChannel == channel.ref;
    if (!composerReady && _pendingComposerChannel != channel.ref) {
      _pendingComposerChannel = channel.ref;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _switchComposer(channel.ref);
      });
    }
    if (_commandsChannel != channel.ref) {
      _commandsChannel = channel.ref;
      _applicationCommands = const [];
      unawaited(_loadApplicationCommands(channel.ref));
    }
    if (channel.type.isVoiceLike) {
      return VoiceRoom(
        channel: channel,
        onApps: state.user == null
            ? null
            : () => _showApplicationCommandLauncher(channel),
      );
    }
    final moderationStatus = state.activeModerationStatus;
    final encryptedPaused = channelEncryptionPaused(channel);
    final canUseCommands = canUseApplicationCommands(channel);
    final usableApplicationCommands = _applicationCommands
        .where((command) => mobileApplicationCommandAllowedByChannelPermissions(
              command,
              canUseCommands,
              _canSendUserContextCommands(channel),
            ))
        .toList(growable: false);
    final canSend = !encryptedPaused &&
        (!channel.locked || canManageThreads(channel)) &&
        moderationStatus == null &&
        (channel.type == ChannelType.dm ||
            channel.type == ChannelType.groupDm ||
            (channel.isThread
                ? canSendInThread(channel)
                : channel.allows(Permission.sendMessages)));
    final canReadHistory = canReadRetainedChannelHistory(channel);
    final stateMessages = state.messages;
    final messages = canReadHistory
        ? threadTimelineMessages(channel, stateMessages)
        : stateMessages;
    final detachedStarter =
        !canReadHistory || messages.length == stateMessages.length
            ? null
            : channel.starterMessage;
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
    if (canReadHistory &&
        state.channelsWithOlderMessages.contains(channel.ref) &&
        !state.loadingMessages &&
        state.error == null) {
      _scheduleAutomaticHistoryCheck();
    }
    final historySyncWarning = _guildHistorySyncWarning(state.activeGuild);
    // Per-build lookup indexes for the lazy list. Building them once here
    // keeps the itemBuilder and the moved-child callback below O(1); without
    // them every visible row would scan the whole message list for its reply
    // reference and every moved row for its new index.
    final messageByWire = <String, KaedeMessage>{
      for (final message in messages) message.ref.wire: message,
    };
    final chronologicalIndex = <String, int>{
      for (var index = 0; index < messages.length; index++)
        messages[index].ref.wire: index,
    };
    // Drop row identities for messages that left the list (edits, deletions,
    // retention trims) so the key map stays bounded. Live rows keep their
    // GlobalKeys so per-row state (spoilers, videos, in-flight swipes)
    // survives rebuilds.
    _messageKeys.removeWhere((wire, _) => !messageByWire.containsKey(wire));
    final indexByKey = <GlobalKey, int>{
      for (final entry in _messageKeys.entries)
        if (chronologicalIndex[entry.key] case final index?) entry.value: index,
    };
    final profiled = kProfileMode ? TimelineTask() : null;
    profiled?.start(
      'kaede.channelview.build',
      arguments: {'messages': messages.length, 'pending': pending.length},
    );
    Widget content;
    try {
      content = Column(
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
          if (!canReadHistory)
            const _FederationStatusStrip(
              title: 'Message history is unavailable.',
              message: 'New messages will appear here while you are connected.',
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
              onRetry: error
                      .startsWith('Older messages are temporarily unavailable')
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
                      ? canReadHistory
                          ? _ConversationStart(channel: channel)
                          : const SizedBox.shrink()
                      : ListView.builder(
                          controller: _scroll,
                          reverse: true,
                          padding: EdgeInsets.fromLTRB(0, 6, 0, 12),
                          // New realtime messages move every visible child by an
                          // index in this reversed list. Tell the sliver where a
                          // keyed row moved so it preserves the row (and any
                          // pointer currently swiping it) instead of disposing it
                          // midway through the gesture.
                          findChildIndexCallback: (key) {
                            final index = indexByKey[key];
                            if (index == null) return null;
                            return pending.length + messages.length - index - 1;
                          },
                          itemCount: messages.length +
                              pending.length +
                              (canReadHistory &&
                                      (state.channelsWithOlderMessages
                                              .contains(channel.ref) ||
                                          (channel.historyTruncated &&
                                              messages.isEmpty) ||
                                          (channel.historyTruncated &&
                                              !channel.historyRemoteAvailable))
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
                                padding: EdgeInsets.fromLTRB(52, 12, 52, 10),
                                child: OutlinedButton.icon(
                                  style: OutlinedButton.styleFrom(
                                    minimumSize: Size(0, 40),
                                    foregroundColor: context.kaede.textSoft,
                                    side:
                                        BorderSide(color: context.kaede.border),
                                    backgroundColor: context.kaede.panel,
                                    textStyle: TextStyle(
                                      fontSize: 13,
                                      fontWeight: FontWeight.w600,
                                    ),
                                  ),
                                  onPressed: state.loadingMessages
                                      ? null
                                      : _loadEarlier,
                                  icon: state.loadingMessages
                                      ? SizedBox.square(
                                          dimension: 15,
                                          child: CircularProgressIndicator(
                                              strokeWidth: 2))
                                      : Icon(Icons.history_rounded, size: 17),
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
                            final startsNewDay = message.createdAtAvailable &&
                                (previous == null ||
                                    !previous.createdAtAvailable ||
                                    !sameCalendarDay(
                                      previous.createdAt,
                                      message.createdAt,
                                    ));
                            final detached =
                                message.ref == detachedStarter?.ref;
                            final compact = !detached &&
                                previous != null &&
                                previous.ref != detachedStarter?.ref &&
                                previous.createdAtAvailable &&
                                message.createdAtAvailable &&
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
                                  : messageByWire[message.reference!.wire],
                              onQuickReaction: detached || channel.archived
                                  ? null
                                  : () => unawaited(_quickReact(message)),
                              onJump: message.reference == null
                                  ? null
                                  : () => _jumpTo(message.reference!),
                              onMenu: detached
                                  ? null
                                  : () => _showMessageActions(message),
                              onAttachmentActions: detached
                                  ? null
                                  : (attachment, manifest, decryptedFile) =>
                                      _showMessageActions(
                                        message,
                                        attachment: attachment,
                                        attachmentManifest: manifest,
                                        decryptedAttachment: decryptedFile,
                                      ),
                              onOpenThread: message.thread == null
                                  ? null
                                  : () => ref
                                      .read(mobileControllerProvider.notifier)
                                      .selectChannel(message.thread!),
                              onReaction: detached || channel.archived
                                  ? null
                                  : (emoji) => _toggleReaction(message, emoji),
                              onComponent: detached || channel.archived
                                  ? null
                                  : (component, values) =>
                                      _invokeMessageComponent(
                                          message, component, values),
                              onPollVote: detached || channel.archived
                                  ? null
                                  : (answerId, selected) =>
                                      _setPollVote(message, answerId, selected),
                              onPollVoters: detached || message.poll == null
                                  ? null
                                  : () => _showPollVoters(message),
                              onAddReaction: detached ||
                                      message.reactionCounts.isEmpty ||
                                      !canAddMessageReaction(
                                        channel,
                                        emojiExists: false,
                                      )
                                  ? null
                                  : () => _addReactionFromPicker(message),
                              onAuthorTap: message.author == null
                                  ? null
                                  : () => _showChannelUserProfile(
                                        message.author!,
                                      ),
                            );
                            return KeyedSubtree(
                              key: key,
                              child: RepaintBoundary(
                                child: Column(
                                  crossAxisAlignment:
                                      CrossAxisAlignment.stretch,
                                  children: [
                                    if (startsNewDay)
                                      _DayDivider(day: message.createdAt),
                                    AnimatedContainer(
                                      duration: Duration(milliseconds: 240),
                                      curve: Curves.easeOut,
                                      color: _highlightedMessage == message.ref
                                          ? context.kaede.coral
                                              .withValues(alpha: .13)
                                          : Colors.transparent,
                                      child: SwipeToReply(
                                        enabled: !detached &&
                                            canSend &&
                                            message.deletedAt == null,
                                        onReply: reply,
                                        child: tile,
                                      ),
                                    ),
                                  ],
                                ),
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
          if (_commandQuery != null)
            _CommandSuggestions(
              commands: channel.archived
                  ? const <MobileApplicationCommand>[]
                  : _commandCandidates(
                      usableApplicationCommands,
                      _commandQuery!,
                    ),
              showNativeThread: !channel.isThread &&
                  (channel.type == ChannelType.text ||
                      channel.type == ChannelType.announcement) &&
                  canCreatePublicThread(channel) &&
                  hasSendMessagesInThreads(channel) &&
                  'thread'.contains(_commandQuery!.toLowerCase()),
              onNativeThread: _insertNativeThreadCommand,
              onSelected: _insertCommand,
            ),
          if (!canSend)
            _PermissionNotice(
              message: encryptedPaused
                  ? 'Encrypted messaging is paused while participant device keys are secured. No plaintext will be sent.'
                  : channel.locked && !canManageThreads(channel)
                      ? 'This thread is locked. Only moderators can send messages.'
                      : moderationStatus == null
                          ? 'You do not have permission to send messages here.'
                          : 'You cannot send messages while timed out.',
              onApps: state.user == null
                  ? null
                  : () => _showApplicationCommandLauncher(channel),
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
                slowModeRemaining: _slowModeRemaining(channel),
                compact: MediaQuery.sizeOf(context).width <= 360,
                // Discovery remains available even when this channel denies
                // command execution; the launcher receives no runnable
                // commands in that case and still exposes reviewed apps.
                appsEnabled: state.user != null,
                onNotifyChanged: (value) =>
                    setState(() => _notifyReply = value),
                onCancelReply: () => setState(() => _reply = null),
                onRemoveUpload: (item) {
                  setState(() => _uploads.remove(item));
                  unawaited(item.deleteIfTemporary());
                },
                onMore: () => _showComposerActions(channel),
                onApps: () => _showApplicationCommandLauncher(channel),
                onMedia: () =>
                    _runComposerAction(channel, ComposerAction.media),
                idleAction: canSendVoiceMessage(channel)
                    ? VoiceMessageRecorder(
                        enabled: _slowModeRemaining(channel) <= Duration.zero,
                        busy: _sending,
                        onRecorded: (recording) =>
                            _sendVoiceRecording(channel, recording),
                        onError: _showVoiceMessageError,
                      )
                    : null,
                onSend: _send,
              ),
            ),
        ],
      );
    } finally {
      profiled?.finish();
    }
    return content;
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
    final commandMatch = RegExp(r'^/([^\s/]{0,32})$').firstMatch(beforeCursor);
    final nextCommand = commandMatch?.group(1);
    if ((_mentionQuery != nextMention || _commandQuery != nextCommand) &&
        mounted) {
      setState(() {
        _mentionQuery = nextMention;
        _commandQuery = nextCommand;
      });
    }
    final channel = _composerChannel;
    if (channel == null) return;
    _draftTimer?.cancel();
    _draftTimer = Timer(Duration(milliseconds: 300), () {
      if (!mounted || _composerChannel != channel) return;
      ref.read(mobileControllerProvider.notifier).setDraft(
            channel,
            _composer.text,
          );
    });
    _publishTyping(channel);
  }

  Future<void> _loadApplicationCommands(EntityRef channel) async {
    try {
      final raw = await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .applicationCommands(channel);
      final commands = raw
          .map(MobileApplicationCommand.fromJson)
          .where((command) =>
              const {'chat_input', 'message', 'user'}.contains(command.type))
          .toList(growable: false);
      if (mounted && _commandsChannel == channel) {
        setState(() => _applicationCommands = commands);
      }
    } on Object {
      // Messaging remains usable when an optional integration is unavailable.
    }
  }

  List<MobileApplicationCommand> _commandCandidates(
    Iterable<MobileApplicationCommand> commands,
    String query,
  ) {
    final needle = query.toLowerCase();
    final locale = Localizations.localeOf(context).toLanguageTag();
    return commands
        .where((command) => command.type == 'chat_input')
        .where((command) =>
            command.displayName(locale).toLowerCase().contains(needle) ||
            command.name.toLowerCase().contains(needle) ||
            command.applicationName.toLowerCase().contains(needle))
        .take(6)
        .toList(growable: false);
  }

  void _insertCommand(MobileApplicationCommand command) {
    final displayName =
        command.displayName(Localizations.localeOf(context).toLanguageTag());
    _composer.value = TextEditingValue(
      text: '/$displayName',
      selection: TextSelection.collapsed(offset: displayName.length + 1),
    );
    setState(() => _commandQuery = null);
    unawaited(_openApplicationCommandComposer(command));
  }

  Future<void> _showApplicationCommandLauncher(KaedeChannel channel) async {
    if (_sending) return;
    final state = ref.read(mobileControllerProvider);
    final account = state.user?.ref;
    final home =
        ref.read(mobileControllerProvider.notifier).api.tokens?.instance;
    if (account == null || home == null) return;
    final canRunCommands = !channel.archived &&
        !channelEncryptionPaused(channel) &&
        state.activeModerationStatus == null;
    final usableApplicationCommands = _applicationCommands
        .where((command) => mobileApplicationCommandAllowedByChannelPermissions(
              command,
              canUseApplicationCommands(channel),
              _canSendUserContextCommands(channel),
            ))
        .toList(growable: false);
    final repository = ref.read(mobileControllerProvider.notifier).repository;
    final selection = await showModalBottomSheet<MobileAppLauncherSelection>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: context.kaede.panel,
      builder: (context) => MobileApplicationLauncherSheet(
        commands: canRunCommands
            ? usableApplicationCommands
            : const <MobileApplicationCommand>[],
        account: account,
        home: home,
        isAccountCurrent: (expected) {
          final current = ref.read(mobileControllerProvider);
          return current.user?.ref == expected &&
              current.activeChannel?.ref == channel.ref;
        },
        loadInstalledApplications: repository.userApplicationInstallations,
        loadRecentApplications: () => _recentApplications(account),
        loadBotProfileApplication: repository.botProfileApplication,
        loadDirectory: ({query, collection, domain, required limit}) =>
            repository.applicationDirectory(
          query: query,
          collection: collection,
          domain: domain,
          limit: limit,
        ),
      ),
    );
    if (!mounted ||
        selection == null ||
        ref.read(mobileControllerProvider).user?.ref != account ||
        ref.read(mobileControllerProvider).activeChannel?.ref != channel.ref) {
      return;
    }
    switch (selection) {
      case MobileAppLauncherCommandSelection(:final command):
        _insertCommand(command);
      case MobileAppLauncherInstallSelection(:final application):
        await context.push<void>(
          mobileApplicationInstallPath(application, home),
        );
      case MobileAppLauncherBotInstallSelection(:final application):
        await context.push<void>(
          mobileBotApplicationInstallPath(application, home),
        );
    }
  }

  Future<void> _openApplicationCommandComposer(
    MobileApplicationCommand command,
  ) async {
    final state = ref.read(mobileControllerProvider);
    final channel = state.activeChannel;
    if (channel == null || channel.ref != _composerChannel) return;
    final unavailable = switch (channel) {
      KaedeChannel(archived: true) =>
        'Commands are unavailable in archived threads.',
      _
          when !mobileApplicationCommandAllowedByChannelPermissions(
            command,
            canUseApplicationCommands(channel),
            _canSendUserContextCommands(channel),
          ) =>
        'You do not have permission to use application commands in this channel.',
      _ => null,
    };
    if (unavailable != null) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(unavailable)),
        );
      }
      return;
    }

    final users = <EntityRef, KaedeUser>{};
    if (channel.guildRef != null) {
      for (final member in state.activeGuildMembers) {
        users[member.user.ref] =
            state.userProfiles[member.user.ref] ?? member.user;
      }
    } else {
      for (final user in <KaedeUser>[
        ...channel.recipients,
        if (state.user case final current?) current,
      ]) {
        users[user.ref] = state.userProfiles[user.ref] ?? user;
      }
    }
    final userChoices = users.values
        .map((user) => (
              value: user.ref.wire,
              label: '${user.name} · ${user.handle}',
            ))
        .toList()
      ..sort((left, right) => left.label.compareTo(right.label));
    final channels = <EntityRef, KaedeChannel>{
      if (state.activeGuild case final guild?)
        for (final item in guild.channels) item.ref: item,
      for (final item in state.threads)
        if (item.guildRef == channel.guildRef) item.ref: item,
      if (channel.guildRef == null) channel.ref: channel,
    }
        .values
        .map((item) => (
              value: item.ref.wire,
              label: '#${item.name ?? 'channel'}',
              type: _channelTypeWireValue(item.type),
            ))
        .toList()
      ..sort((left, right) => left.label.compareTo(right.label));
    final roles = (state.activeGuild?.roles ?? const <KaedeRole>[])
        .map((role) => (value: role.ref.wire, label: '@${role.name}'))
        .toList()
      ..sort((left, right) => left.label.compareTo(right.label));

    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: context.kaede.panel,
      builder: (sheetContext) => _ApplicationCommandSheet(
        command: command,
        users: userChoices,
        channels: channels,
        roles: roles,
        attachments: _commandAttachmentChoices(),
        onPickAttachments: _pickCommandFiles,
        onAutocomplete: ({
          required values,
          required path,
          required generation,
        }) =>
            _requestTypedCommandAutocomplete(
          channel: channel,
          command: command,
          values: values,
          focusedOption: path,
          generation: generation,
        ),
        onSubmit: (values) => _submitApplicationCommand(
          channel: channel,
          command: command,
          values: values,
        ),
      ),
    );
  }

  List<_CommandAttachmentChoice> _commandAttachmentChoices() => _uploads
      .map((upload) => (
            key: upload.commandKey,
            label: '${upload.name} · ${formatAttachmentSize(upload.size)}',
          ))
      .toList(growable: false);

  Future<List<_CommandAttachmentChoice>> _pickCommandFiles(
    MobileApplicationCommandOption option,
  ) async {
    final additions = await _chooseFiles(
      10 - _uploads.length,
      fileTypes: option.fileTypes,
    );
    if (additions.isNotEmpty && mounted) {
      setState(() => _uploads.addAll(additions));
    }
    return _commandAttachmentChoices();
  }

  Future<_PreparedCommandFiles> _prepareCommandFiles(
    KaedeChannel channel,
    MobileApplicationCommand command,
    CommandComposerValues values,
  ) async {
    final attachmentKeys = mobileCommandAttachmentKeys(command, values);
    if (attachmentKeys.isNotEmpty &&
        channel.guildRef != null &&
        !channel.allows(Permission.attachFiles)) {
      throw UserInputException(
        'You do not have permission to upload command attachments here.',
      );
    }
    final selectedUploads = <_PendingUpload>[];
    for (final key in attachmentKeys) {
      final upload =
          _uploads.where((item) => item.commandKey == key).firstOrNull;
      if (upload == null) {
        throw UserInputException(
          'A selected file is no longer available. Choose it again.',
        );
      }
      selectedUploads.add(upload);
    }
    final repository = ref.read(mobileControllerProvider.notifier).repository;
    final uploaded = <String, EntityRef>{};
    final manifests = <String, Map<String, Object?>>{};
    for (final item in selectedUploads) {
      if (channel.encryptionMode == 'e2ee') {
        final cached = item.encryptedCommandUploadFor(channel.ref);
        final encrypted = cached ??
            await uploadEncryptedFile(
              repository: repository,
              channel: channel.ref,
              source: item.file,
              filename: item.name,
              contentType: item.contentType,
            );
        item.rememberEncryptedCommandUpload(channel.ref, encrypted);
        uploaded[item.commandKey] = encrypted.attachment;
        manifests[encrypted.attachment.id.value] = encrypted.manifest;
      } else {
        final cached = item.commandUploadFor(channel.ref);
        uploaded[item.commandKey] = cached ??
            await repository
                .uploadAttachmentFile(
              channel: channel.ref,
              filename: item.name,
              contentType: item.contentType,
              file: item.file,
            )
                .then((attachment) {
              item.rememberCommandUpload(channel.ref, attachment);
              return attachment;
            });
      }
    }
    var wireValues = CommandComposerValues.of(values);
    final model = mobileCommandComposerModel(command.options, values);
    for (final field
        in model.fields.where((field) => field.option.type == 'attachment')) {
      final key = values[field.path];
      final attachment = key is String ? uploaded[key] : null;
      if (attachment != null) {
        wireValues = mobileCommandValueChanged(
          wireValues,
          field.path,
          attachment.id.value,
        );
      }
    }
    return (
      values: wireValues,
      uploads: selectedUploads,
      manifests: manifests,
    );
  }

  Future<MobilePreparedEncryptedInteraction?> _prepareEncryptedInteraction(
    KaedeChannel channel, {
    required EntityRef application,
    required String integrationType,
    required String interactionContext,
    required String interactionType,
    String? commandId,
    String? commandName,
    String? commandType,
    Object? componentType,
    String? customId,
    EntityRef? message,
    Object? responseId,
    EntityRef? target,
    Object? viewVersion,
    Object? autocompleteGeneration,
    String? focusedOption,
    Iterable<String> attachmentIds = const <String>[],
    Map<String, Map<String, Object?>> attachmentManifests =
        const <String, Map<String, Object?>>{},
    Map<String, Object?> options = const <String, Object?>{},
    List<String> values = const <String>[],
    List<Map<String, Object?>> components = const <Map<String, Object?>>[],
  }) async {
    if (channel.encryptionMode != 'e2ee') return null;
    return (await ref.read(mobileControllerProvider.notifier).e2eeClient())
        .encryptInteraction(
      channel,
      application: application,
      integrationType: integrationType,
      interactionContext: interactionContext,
      interactionType: interactionType,
      commandId: commandId,
      commandName: commandName,
      commandType: commandType,
      componentType: componentType,
      customId: customId,
      message: message,
      responseId: responseId,
      target: target,
      viewVersion: viewVersion,
      autocompleteGeneration: autocompleteGeneration,
      focusedOption: focusedOption,
      attachmentIds: attachmentIds,
      attachmentManifests: attachmentManifests,
      options: options,
      values: values,
      components: components,
    );
  }

  ({String integrationType, String interactionContext})
      _applicationInteractionAuthority(
    KaedeChannel channel,
    EntityRef application,
    String? integrationType,
  ) {
    final authority = _applicationCommands
        .where((command) =>
            command.application == application &&
            (integrationType == null ||
                command.integrationType == integrationType) &&
            mobileApplicationCommandAllowedByUsePermission(
              command,
              canUseApplicationCommands(channel),
            ))
        .firstOrNull;
    if (authority == null) {
      throw UserInputException(
        'Refresh this channel before using this encrypted app control. Its installation authority is not available.',
      );
    }
    return (
      integrationType: authority.integrationType,
      interactionContext: authority.interactionContext,
    );
  }

  Future<List<MobileApplicationCommandChoice>>
      _requestTypedCommandAutocomplete({
    required KaedeChannel channel,
    required MobileApplicationCommand command,
    required CommandComposerValues values,
    required String focusedOption,
    required int generation,
  }) async {
    if (!mounted ||
        ref.read(mobileControllerProvider).activeChannel?.ref != channel.ref ||
        !mobileApplicationCommandAllowedByChannelPermissions(
          command,
          canUseApplicationCommands(channel),
          _canSendUserContextCommands(channel),
        )) {
      return const <MobileApplicationCommandChoice>[];
    }
    final preparedFiles = await _prepareCommandFiles(channel, command, values);
    final options = mobileCommandOptionPayload(command, preparedFiles.values);
    final attachmentIds = preparedFiles.manifests.keys.toList(growable: false);
    final encrypted = await _prepareEncryptedInteraction(
      channel,
      application: command.application,
      integrationType: command.integrationType,
      interactionContext: command.interactionContext,
      interactionType: 'autocomplete',
      commandId: command.id,
      commandName: command.name,
      commandType: command.type,
      autocompleteGeneration: generation,
      focusedOption: focusedOption,
      attachmentIds: attachmentIds,
      attachmentManifests: preparedFiles.manifests,
      options: options,
    );
    final acknowledged = await ref
        .read(mobileControllerProvider.notifier)
        .repository
        .autocompleteApplicationCommand(
          channel: channel.ref,
          application: command.application,
          commandId: command.id,
          integrationType: command.integrationType,
          dmCapabilityId: command.dmCapabilityId,
          dmCapabilityRevision: command.dmCapabilityRevision,
          name: command.name,
          type: command.type,
          options: options,
          focusedOption: focusedOption,
          generation: generation,
          encryptedPayload: encrypted?.envelope,
          attachmentIds: encrypted?.attachmentIds ?? const <String>[],
        );
    final interactionRef = _interactionAcknowledgementRef(acknowledged);
    final waiter = Completer<List<MobileApplicationCommandChoice>>();
    _autocompleteRequests[interactionRef] = (
      generation: generation,
      path: focusedOption,
      waiter: waiter,
    );
    if (mounted) setState(() {});
    try {
      return await waiter.future.timeout(Duration(seconds: 10));
    } on TimeoutException {
      final request = _autocompleteRequests[interactionRef];
      if (request?.waiter == waiter) {
        _autocompleteRequests.remove(interactionRef);
      }
      return const <MobileApplicationCommandChoice>[];
    }
  }

  Future<void> _submitApplicationCommand({
    required KaedeChannel channel,
    required MobileApplicationCommand command,
    required CommandComposerValues values,
  }) async {
    if (_sending) {
      throw UserInputException('Wait for the current send to finish.');
    }
    if (ref.read(mobileControllerProvider).activeChannel?.ref != channel.ref) {
      throw UserInputException(
        'The active channel changed. Reopen the command and try again.',
      );
    }
    if (!mobileApplicationCommandAllowedByChannelPermissions(
      command,
      canUseApplicationCommands(channel),
      _canSendUserContextCommands(channel),
    )) {
      throw UserInputException(
        'This guild-installed command is unavailable in this channel.',
      );
    }
    final errors = mobileCommandOptionErrors(command, values);
    if (errors.isNotEmpty) throw UserInputException(errors.values.first);

    final account = ref.read(mobileControllerProvider).user?.ref;
    final controller = ref.read(mobileControllerProvider.notifier);
    setState(() => _sending = true);
    try {
      final repository = controller.repository;
      final preparedFiles =
          await _prepareCommandFiles(channel, command, values);
      final options = mobileCommandOptionPayload(command, preparedFiles.values);
      final encrypted = await _prepareEncryptedInteraction(
        channel,
        application: command.application,
        integrationType: command.integrationType,
        interactionContext: command.interactionContext,
        interactionType: 'command',
        commandId: command.id,
        commandName: command.name,
        commandType: command.type,
        attachmentIds: preparedFiles.manifests.keys,
        attachmentManifests: preparedFiles.manifests,
        options: options,
      );
      final acknowledged = await repository.invokeApplicationCommand(
        channel: channel.ref,
        application: command.application,
        commandId: command.id,
        integrationType: command.integrationType,
        dmCapabilityId: command.dmCapabilityId,
        dmCapabilityRevision: command.dmCapabilityRevision,
        name: command.name,
        type: command.type,
        options: options,
        encryptedPayload: encrypted?.envelope,
        attachmentIds: encrypted?.attachmentIds ?? const <String>[],
      );
      controller.rememberInteractionRequest(
        _interactionAcknowledgementRef(acknowledged),
        channel: channel.ref,
        application: command.application,
        integrationType: command.integrationType,
        interactionContext: command.interactionContext,
      );
      await _rememberApplication(account, command.application);
      if (mounted) {
        setState(() {
          _uploads.removeWhere(preparedFiles.uploads.contains);
          _commandQuery = null;
        });
      }
      for (final upload in preparedFiles.uploads) {
        unawaited(upload.deleteIfTemporary());
      }
      if (_composerChannel == channel.ref &&
          RegExp('^/${RegExp.escape(command.name)}(?:\\s|\$)',
                  caseSensitive: false)
              .hasMatch(_composer.text.trim())) {
        _updatingComposer = true;
        _composer.clear();
        _updatingComposer = false;
        controller.setDraft(channel.ref, '');
      }
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text('/${command.name} sent to ${command.applicationName}.'),
        ));
      }
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  void _insertNativeThreadCommand() {
    const text = '/thread name: message:';
    _composer.value = TextEditingValue(
      text: text,
      selection: TextSelection.collapsed(offset: 13),
    );
    setState(() => _commandQuery = null);
    _composerFocus.requestFocus();
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
        when now.difference(last) < Duration(seconds: 8)) {
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
            duration: Duration(milliseconds: 180), curve: Curves.easeOutCubic));
      } else {
        _scroll.jumpTo(target);
      }
    });
  }

  Future<void> _loadEarlier() async {
    final channel = ref.read(mobileControllerProvider).activeChannel;
    if (channel == null || !canReadRetainedChannelHistory(channel)) return;
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
          duration: Duration(milliseconds: 320),
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
        _highlightTimer = Timer(Duration(seconds: 2), () {
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
        channel.type == ChannelType.groupDm ||
        channel.allows(Permission.attachFiles);
    _composerFocus.unfocus();
    final action = await showComposerActionPicker(
      context,
      canAttach: canAttach,
      gifsAllowed: composerAllowsGifs(channel),
      canCreatePoll: canCreatePollInChannel(channel),
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
            channel.type != ChannelType.groupDm &&
            !channel.allows(Permission.attachFiles)) {
          ScaffoldMessenger.of(context).showSnackBar(SnackBar(
            content: Text('You cannot attach files in this channel.'),
          ));
          return;
        }
        await _pickFiles();
        break;
      case ComposerAction.media:
        final state = ref.read(mobileControllerProvider);
        final recent = await _recentReactions(state.user?.ref);
        if (!mounted ||
            ref.read(mobileControllerProvider).activeChannel?.ref !=
                channel.ref) {
          return;
        }
        final selection = await showComposerMediaPicker(
          context,
          repository: ref.read(mobileControllerProvider.notifier).repository,
          channel: channel,
          categories: defaultComposerEmojiCategories,
          recent: recent,
          gifsAllowed: composerAllowsGifs(channel),
        );
        if (!mounted || selection == null) return;
        if (ref.read(mobileControllerProvider).activeChannel?.ref !=
            channel.ref) {
          return;
        }
        switch (selection) {
          case ComposerEmojiSelection(:final value):
            _insertComposerText(value);
          case ComposerStickerSelection(:final sticker):
            await _sendSticker(channel, sticker);
          case ComposerGifSelection(:final gif):
            await _sendGif(channel, gif);
        }
        break;
      case ComposerAction.poll:
        await _createPoll(channel);
        break;
    }
  }

  Future<void> _createPoll(KaedeChannel channel) async {
    if (_sending || !canCreatePollInChannel(channel)) return;
    if (_composer.text.trim().isNotEmpty ||
        _uploads.isNotEmpty ||
        _reply != null) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(
          'Send or clear the current draft, attachments, and reply before creating a poll.',
        ),
      ));
      return;
    }
    final recent = await _recentReactions(
      ref.read(mobileControllerProvider).user?.ref,
    );
    if (!mounted ||
        ref.read(mobileControllerProvider).activeChannel?.ref != channel.ref) {
      return;
    }
    final poll = await showDialog<RichPollDraft>(
      context: context,
      barrierDismissible: false,
      builder: (context) => _PollCreateDialog(
        channel: channel,
        repository: ref.read(mobileControllerProvider.notifier).repository,
        emojiCategories: defaultComposerEmojiCategories,
        recentEmoji: recent,
      ),
    );
    if (!mounted || poll == null) return;
    setState(() => _sending = true);
    try {
      await ref
          .read(mobileControllerProvider.notifier)
          .createPoll(channel.ref, poll);
      if (channel.slowModeSeconds > 0) {
        _startSlowMode(channel, Duration(seconds: channel.slowModeSeconds));
      }
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Poll created.')),
        );
      }
    } on Object catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(
              userFacingError(error, summary: 'Could not create the poll')),
        ));
      }
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  void _insertComposerText(String insertion) {
    final next = insertComposerText(_composer.value, insertion);
    if (next == null) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
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
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(
        'GIF search is unavailable in end-to-end encrypted conversations.',
      ),
    ));
  }

  Future<void> _sendSticker(
    KaedeChannel channel,
    ComposerSticker sticker,
  ) async {
    final active = ref.read(mobileControllerProvider).activeChannel;
    if (_sending ||
        active?.ref != channel.ref ||
        _composerChannel != channel.ref) {
      return;
    }
    final remaining = _slowModeRemaining(channel);
    if (remaining > Duration.zero) {
      final seconds = (remaining.inMilliseconds / 1000).ceil();
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text('Slow mode is active. Try again in $seconds seconds.'),
      ));
      return;
    }
    setState(() => _sending = true);
    try {
      await ref.read(mobileControllerProvider.notifier).send(
        channel.ref,
        '',
        stickerIds: <EntityRef>[sticker.ref],
        stickerItems: <KaedeStickerItem>[
          KaedeStickerItem(
            ref: sticker.ref,
            name: sticker.name,
            formatType: sticker.animated ? 2 : 1,
            mediaHash: sticker.mediaHash,
          ),
        ],
      );
      if (channel.slowModeSeconds > 0) {
        _startSlowMode(channel, Duration(seconds: channel.slowModeSeconds));
      }
      await WidgetsBinding.instance.endOfFrame;
      if (_composerChannel == channel.ref && _scroll.hasClients) {
        await _scroll.animateTo(
          _scroll.position.minScrollExtent,
          duration: Duration(milliseconds: 250),
          curve: Curves.easeOut,
        );
      }
    } on Object catch (error) {
      if (error is KaedeException && error.retryAfter != null) {
        _startSlowMode(channel, error.retryAfter!);
      }
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(userFacingError(
            error,
            summary: 'Could not send that sticker',
          )),
        ));
      }
    } finally {
      if (mounted) setState(() => _sending = false);
    }
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
    final remaining = _slowModeRemaining(channel);
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
        _startSlowMode(channel, Duration(seconds: channel.slowModeSeconds));
      }
      await WidgetsBinding.instance.endOfFrame;
      if (_composerChannel == channel.ref && _scroll.hasClients) {
        await _scroll.animateTo(
          _scroll.position.minScrollExtent,
          duration: Duration(milliseconds: 250),
          curve: Curves.easeOut,
        );
      }
    } on Object catch (error) {
      if (error is KaedeException && error.retryAfter != null) {
        _startSlowMode(channel, error.retryAfter!);
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
    final additions = await _chooseFiles(10 - _uploads.length);
    if (additions.isNotEmpty && mounted) {
      setState(() => _uploads.addAll(additions));
    }
  }

  Future<List<_PendingUpload>> _chooseFiles(
    int capacity, {
    List<String> fileTypes = const <String>[],
  }) async {
    if (capacity <= 0) return const <_PendingUpload>[];
    final extensions = fileTypes
        .where((value) => value.startsWith('.'))
        .map((value) => value.substring(1))
        .toList(growable: false);
    final categories =
        fileTypes.where(const {'image', 'video', 'audio'}.contains).toSet();
    final FileType pickerType;
    if (extensions.length == fileTypes.length && extensions.isNotEmpty) {
      pickerType = FileType.custom;
    } else if (categories.length == 1 && categories.contains('image')) {
      pickerType = FileType.image;
    } else if (categories.length == 1 && categories.contains('video')) {
      pickerType = FileType.video;
    } else if (categories.length == 1 && categories.contains('audio')) {
      pickerType = FileType.audio;
    } else if (categories.length == 2 &&
        categories.containsAll(const {'image', 'video'})) {
      pickerType = FileType.media;
    } else {
      pickerType = FileType.any;
    }
    final result = await FilePicker.platform.pickFiles(
      allowMultiple: true,
      withData: false,
      withReadStream: true,
      type: pickerType,
      allowedExtensions: pickerType == FileType.custom ? extensions : null,
    );
    if (result == null || !mounted) return const <_PendingUpload>[];
    final additions = <_PendingUpload>[];
    for (final file in result.files.take(capacity)) {
      final contentType = _contentType(file.name);
      if (!mobileFileMatchesCommandTypes(fileTypes, file.name, contentType)) {
        continue;
      }
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
        commandKey:
            '${DateTime.now().microsecondsSinceEpoch}-${additions.length}-${_safeName(file.name)}',
        name: file.name,
        file: source,
        size: await source.length(),
        contentType: contentType,
        temporary: temporary,
      ));
    }
    if (fileTypes.isNotEmpty && result.files.isNotEmpty && additions.isEmpty) {
      throw UserInputException(
        'Choose a file matching ${fileTypes.join(', ')} for this command option.',
      );
    }
    return additions;
  }

  void _showVoiceMessageError(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(message)),
    );
  }

  Future<void> _sendVoiceRecording(
    KaedeChannel expectedChannel,
    VoiceRecording recording,
  ) async {
    if (_sending || _composerChannel != expectedChannel.ref) {
      await recording.delete();
      return;
    }
    final channel = ref.read(mobileControllerProvider).activeChannel;
    if (channel == null ||
        channel.ref != expectedChannel.ref ||
        !canSendVoiceMessage(channel) ||
        _slowModeRemaining(channel) > Duration.zero) {
      await recording.delete();
      _showVoiceMessageError('Voice messages are unavailable in this channel.');
      return;
    }
    final reply = _reply;
    setState(() => _sending = true);
    try {
      final controller = ref.read(mobileControllerProvider.notifier);
      final encryptedUpload = channel.encryptionMode == 'e2ee'
          ? await uploadEncryptedFile(
              repository: controller.repository,
              channel: channel.ref,
              source: recording.file,
              filename: recording.filename,
              contentType: recording.contentType,
              durationMillis: (recording.durationSecs * 1000).round(),
              waveform: recording.waveform,
            )
          : null;
      final attachment = encryptedUpload?.attachment ??
          await controller.repository.uploadAttachmentFile(
            channel: channel.ref,
            filename: recording.filename,
            contentType: recording.contentType,
            file: recording.file,
            durationSecs: recording.durationSecs,
            waveform: recording.waveform,
          );
      await controller.send(
        channel.ref,
        '',
        attachments: <EntityRef>[attachment],
        encryptedAttachments: encryptedUpload == null
            ? const <Map<String, Object?>>[]
            : <Map<String, Object?>>[encryptedUpload.manifest],
        replyTo: reply?.ref,
        notify: false,
        voiceMessage: true,
      );
      if (mounted && _composerChannel == channel.ref) {
        setState(() {
          if (_reply == reply) _reply = null;
        });
      }
      if (channel.slowModeSeconds > 0) {
        _startSlowMode(channel, Duration(seconds: channel.slowModeSeconds));
      }
      await WidgetsBinding.instance.endOfFrame;
      if (mounted && _composerChannel == channel.ref && _scroll.hasClients) {
        await _scroll.animateTo(
          _scroll.position.minScrollExtent,
          duration: Duration(milliseconds: 250),
          curve: Curves.easeOut,
        );
      }
    } on Object catch (error) {
      if (error is KaedeException && error.retryAfter != null) {
        _startSlowMode(channel, error.retryAfter!);
      }
      _showVoiceMessageError(userFacingError(
        error,
        summary: 'Could not send this voice message',
      ));
    } finally {
      await recording.delete();
      if (mounted) setState(() => _sending = false);
    }
  }

  Future<void> _send() async {
    if (_sending) return;
    final state = ref.read(mobileControllerProvider);
    final channel = state.activeChannel;
    if (channel == null || _composerChannel != channel.ref) return;
    if (_slowModeRemaining(channel) > Duration.zero) return;
    unawaited(HapticFeedback.lightImpact());
    final content = _composer.text;
    final reply = _reply;
    final notifyReply = _notifyReply;
    final pendingUploads = List<_PendingUpload>.of(_uploads);
    setState(() => _sending = true);
    try {
      final controller = ref.read(mobileControllerProvider.notifier);
      final ttsCommand = parseTtsCommand(content);
      final tts = ttsCommand.matched;
      if (tts) {
        if (ttsCommand.content.isEmpty) {
          throw UserInputException('Enter a message after /tts.');
        }
        if (!mobileTextToSpeech.preferences.enabled) {
          throw UserInputException(
            'Enable “Allow playback and usage of /tts command” in Accessibility first.',
          );
        }
        if (channel.guildRef != null &&
            !channel.allows(Permission.sendTtsMessages)) {
          throw UserInputException(
            'You do not have permission to send Text-to-Speech messages here.',
          );
        }
      }
      final outgoingContent = tts ? ttsCommand.content : content;
      final commandMatch = tts
          ? null
          : RegExp(r'^/([^\s/]{1,32})(?:\s+([\s\S]*))?$')
              .firstMatch(content.trim());
      final nativeThread = tts ? null : parseNativeThreadCommand(content);
      if (commandMatch?.group(1)?.toLowerCase() == 'thread') {
        if (nativeThread == null) {
          throw UserInputException(
            'Use /thread name:<thread name> message:<first message>.',
          );
        }
        if (channel.type != ChannelType.text &&
            channel.type != ChannelType.announcement) {
          throw UserInputException(
            'Threads can only be created from a text or announcement channel.',
          );
        }
        if (!canCreatePublicThread(channel) ||
            !hasSendMessagesInThreads(channel)) {
          throw UserInputException(
            'You do not have permission to create a thread with a first message here.',
          );
        }
        if (reply != null) {
          throw UserInputException(
            'Create the thread without replying to another message.',
          );
        }
        if (pendingUploads.isNotEmpty &&
            !channel.allows(Permission.attachFiles)) {
          throw UserInputException(
            'You do not have permission to attach files here.',
          );
        }
        final encryptedParent = deferThreadStarterUntilE2eeActive(channel);
        final uploaded = <EntityRef>[];
        final encryptedAttachments = <Map<String, Object?>>[];
        if (!encryptedParent) {
          for (final item in pendingUploads) {
            uploaded.add(await controller.repository.uploadAttachmentFile(
              channel: channel.ref,
              filename: item.name,
              contentType: item.contentType,
              file: item.file,
            ));
          }
        }
        final created = await controller.createThread(
          parent: channel,
          name: nativeThread.name,
          content: encryptedParent ? null : nativeThread.message,
          type: channel.type == ChannelType.announcement ? 10 : 11,
          attachments: encryptedParent ? const <EntityRef>[] : uploaded,
        );
        if (encryptedParent && channelEncryptionPaused(created)) {
          throw UserInputException(
            'The thread was created, but end-to-end encryption could not be activated. Its first message was not sent.',
          );
        }
        if (encryptedParent) {
          for (final item in pendingUploads) {
            final encrypted = await uploadEncryptedFile(
              repository: controller.repository,
              channel: created.ref,
              source: item.file,
              filename: item.name,
              contentType: item.contentType,
            );
            uploaded.add(encrypted.attachment);
            encryptedAttachments.add(encrypted.manifest);
          }
          await controller.send(
            created.ref,
            nativeThread.message,
            attachments: uploaded,
            encryptedAttachments: encryptedAttachments,
            mentionUsers: mentionReferences(nativeThread.message),
          );
        }
        controller.setDraft(channel.ref, '');
        if (_composerChannel == channel.ref) {
          _updatingComposer = true;
          _composer.clear();
          _updatingComposer = false;
        }
        setState(() {
          _uploads.removeWhere(pendingUploads.contains);
          _reply = null;
        });
        for (final upload in pendingUploads) {
          unawaited(upload.deleteIfTemporary());
        }
        await controller.selectChannel(created);
        return;
      }
      final matchingCommands = commandMatch == null
          ? const <MobileApplicationCommand>[]
          : mobileChatInputCommandMatches(
              _applicationCommands.where((command) =>
                  mobileApplicationCommandAllowedByChannelPermissions(
                    command,
                    canUseApplicationCommands(channel),
                    _canSendUserContextCommands(channel),
                  )),
              commandMatch.group(1) ?? '',
              Localizations.localeOf(context).toLanguageTag(),
            );
      if (matchingCommands.length > 1) {
        throw UserInputException(
          'More than one app provides /${commandMatch?.group(1) ?? 'command'}. Choose the app from the Apps button.',
        );
      }
      if (matchingCommands.length == 1) {
        final command = matchingCommands.single;
        final raw = commandMatch!.group(2)?.trim() ?? '';
        setState(() => _sending = false);
        if (raw.isNotEmpty && mounted) {
          ScaffoldMessenger.of(context).showSnackBar(SnackBar(
            content: Text(
              'Choose command options in the typed fields. Free-form arguments are not sent.',
            ),
          ));
        }
        await _openApplicationCommandComposer(command);
        return;
      }
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
        outgoingContent,
        attachments: uploaded,
        encryptedAttachments: encryptedAttachments,
        mentionUsers: mentionReferences(outgoingContent),
        replyTo: reply?.ref,
        replyAuthor: reply?.authorRef,
        notify: notifyReply,
        tts: tts,
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
        _startSlowMode(channel, Duration(seconds: channel.slowModeSeconds));
      }
      await WidgetsBinding.instance.endOfFrame;
      if (_composerChannel == channel.ref && _scroll.hasClients) {
        await _scroll.animateTo(_scroll.position.minScrollExtent,
            duration: Duration(milliseconds: 250), curve: Curves.easeOut);
      }
    } on Object catch (error) {
      if (error is KaedeException && error.retryAfter != null) {
        _startSlowMode(channel, error.retryAfter!);
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

  Duration _slowModeRemaining(KaedeChannel channel) {
    if (canBypassSlowmode(channel)) return Duration.zero;
    final deadline = _slowModeUntil[channel.ref];
    if (deadline == null) return Duration.zero;
    final remaining = deadline.difference(DateTime.now());
    return remaining.isNegative ? Duration.zero : remaining;
  }

  void _startSlowMode(KaedeChannel channel, Duration duration) {
    if (duration <= Duration.zero || !mounted || canBypassSlowmode(channel)) {
      return;
    }
    _slowModeUntil[channel.ref] = DateTime.now().add(duration);
    _slowModeTimer?.cancel();
    _slowModeTimer = Timer.periodic(Duration(milliseconds: 250), (timer) {
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
    final canonical = tryParseReactionEmoji(emoji)?.value;
    if (canonical == null) return;
    final preferences = await SharedPreferences.getInstance();
    final key = _reactionHistoryKey(user);
    final history = preferences.getStringList(key) ?? <String>[];
    history.add(canonical);
    if (history.length > 100) history.removeRange(0, history.length - 100);
    await preferences.setStringList(key, history);
  }

  Future<List<String>> _contextCommandHistory(EntityRef? user) async {
    if (user == null) return const <String>[];
    try {
      final preferences = await SharedPreferences.getInstance();
      return preferences.getStringList(
            mobileContextCommandHistoryStorageKey(user),
          ) ??
          const <String>[];
    } on Object {
      return const <String>[];
    }
  }

  Future<void> _rememberContextCommand(
    EntityRef? user,
    MobileApplicationCommand command,
  ) async {
    if (user == null) return;
    try {
      final preferences = await SharedPreferences.getInstance();
      final key = mobileContextCommandHistoryStorageKey(user);
      final history = mobileRememberContextCommand(
        preferences.getStringList(key) ?? const <String>[],
        command,
      );
      await preferences.setStringList(key, history);
    } on Object {
      // Optional local ranking must never change a successful invocation.
    }
  }

  Future<List<String>> _recentApplications(EntityRef account) async {
    try {
      final preferences = await SharedPreferences.getInstance();
      if (ref.read(mobileControllerProvider).user?.ref != account) {
        return const <String>[];
      }
      return preferences.getStringList(
            mobileAppRecentStorageKey(account),
          ) ??
          const <String>[];
    } on Object {
      return const <String>[];
    }
  }

  Future<void> _rememberApplication(
    EntityRef? account,
    EntityRef application,
  ) async {
    if (account == null ||
        ref.read(mobileControllerProvider).user?.ref != account) {
      return;
    }
    try {
      final preferences = await SharedPreferences.getInstance();
      if (ref.read(mobileControllerProvider).user?.ref != account) return;
      final key = mobileAppRecentStorageKey(account);
      await preferences.setStringList(
        key,
        mobileRememberRecentApplication(
          preferences.getStringList(key) ?? const <String>[],
          application,
        ),
      );
    } on Object {
      // Optional app ranking must never change a successful invocation.
    }
  }

  Future<void> _toggleReaction(KaedeMessage message, String emoji) async {
    final state = ref.read(mobileControllerProvider);
    final channel = state.activeChannel;
    if (channel == null) return;
    final decision = reactionToggleDecision(channel, message, emoji);
    if (decision == null) return;
    final canonical = decision.emoji;
    unawaited(HapticFeedback.selectionClick());
    final controller = ref.read(mobileControllerProvider.notifier);
    try {
      if (decision.removing) {
        await controller.repository
            .removeReaction(message.channelRef, message.ref, canonical);
      } else {
        await controller.repository
            .react(message.channelRef, message.ref, canonical);
        await _rememberReaction(state.user?.ref, canonical);
      }
    } on Object catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(
            userFacingError(error, summary: 'Could not update that reaction')),
      ));
    }
  }

  Future<void> _quickReact(KaedeMessage message) async {
    final state = ref.read(mobileControllerProvider);
    final emoji = (await _recentReactions(state.user?.ref)).firstOrNull;
    if (!mounted || emoji == null) return;
    final current = ref.read(mobileControllerProvider);
    if (current.activeChannel?.ref != message.channelRef) return;
    await _toggleReaction(message, emoji);
  }

  Future<void> _invokeMessageComponent(
    KaedeMessage message,
    RichComponent component,
    List<String> values,
  ) async {
    final application = message.applicationRef;
    final customId = component.customId;
    if (application == null || customId == null) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text('This control is no longer connected to its app.'),
        ));
      }
      return;
    }
    final controller = ref.read(mobileControllerProvider.notifier);
    try {
      final channel = ref.read(mobileControllerProvider).activeChannel;
      if (channel == null || channel.ref != message.channelRef) {
        throw UserInputException('This channel is no longer active.');
      }
      final integrationType = message.interactionIntegrationType ??
          message.interactionMetadata?.integrationType;
      if (!mobileApplicationIntegrationAllowedByUsePermission(
        integrationType,
        canUseApplicationCommands(channel),
      )) {
        throw UserInputException(
          'This guild-installed app control is unavailable in this channel.',
        );
      }
      final authority = channel.encryptionMode == 'e2ee'
          ? _applicationInteractionAuthority(
              channel,
              application,
              integrationType,
            )
          : null;
      final encrypted = authority == null
          ? null
          : await _prepareEncryptedInteraction(
              channel,
              application: application,
              integrationType: authority.integrationType,
              interactionContext: authority.interactionContext,
              interactionType: 'component',
              componentType: component.type,
              customId: customId,
              message: message.ref,
              viewVersion: message.viewVersion > 0 ? message.viewVersion : null,
              values: values,
            );
      final acknowledged = await controller.repository.invokeMessageComponent(
        channel: message.channelRef,
        message: message.ref,
        application: application,
        viewVersion: message.viewVersion,
        customId: customId,
        values: values,
        encryptedPayload: encrypted?.envelope,
      );
      controller.rememberInteractionRequest(
        _interactionAcknowledgementRef(acknowledged),
        channel: message.channelRef,
        application: application,
        integrationType: authority?.integrationType,
        interactionContext: authority?.interactionContext,
      );
    } on Object catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(userFacingError(
          error,
          summary: 'The bot did not receive that interaction',
        )),
      ));
    }
  }

  void _scheduleInteractionResponses(MobileState state) {
    final controller = ref.read(mobileControllerProvider.notifier);
    final openEphemeral = _activeEphemeralResponse;
    final openValue = openEphemeral?.value;
    final openInteractionRef = openValue?.interactionRef;
    if (openEphemeral != null &&
        openValue != null &&
        openInteractionRef != null) {
      final remaining = _privateResponses(
        state,
        openInteractionRef,
      );
      final changed = remaining.length != openValue.responses.length ||
          remaining.indexed.any((item) =>
              item.$2.storageKey != openValue.responses[item.$1].storageKey);
      if (changed) {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (mounted && identical(openEphemeral, _activeEphemeralResponse)) {
            openEphemeral.value = remaining.isEmpty
                ? null
                : (
                    interactionRef: openInteractionRef,
                    responses: remaining,
                    request: openValue.request,
                    state: state,
                  );
          }
        });
      }
    }
    for (final entry in state.interactionResponses.entries) {
      final response = entry.value;
      if (response.deletedAt != null) continue;
      final interactionRef = response.interactionRef.wire;
      final request = state.interactionRequests[interactionRef];
      final responseKey =
          '${response.storageKey}:${response.callbackType}:${response.data.toString().hashCode}';
      final autocompleteRequest = _autocompleteRequests[interactionRef];
      if (response.callbackType == 8 && autocompleteRequest != null) {
        if (_handledAutocompleteResponses.contains(responseKey)) continue;
        _handledAutocompleteResponses.add(responseKey);
        final richChoices = switch (response.data['choices']) {
          final value? => () {
              try {
                return mobileAutocompleteChoices(value);
              } on FormatException {
                return const <MobileApplicationCommandChoice>[];
              }
            }(),
          _ => const <MobileApplicationCommandChoice>[],
        };
        final responseGenerationMatches = response.autocompleteGeneration ==
                null ||
            response.autocompleteGeneration == autocompleteRequest.generation;
        final waiter = autocompleteRequest.waiter;
        WidgetsBinding.instance.addPostFrameCallback((_) {
          _autocompleteRequests.remove(interactionRef);
          if (!waiter.isCompleted) {
            waiter.complete(responseGenerationMatches
                ? richChoices
                : const <MobileApplicationCommandChoice>[]);
          }
        });
        continue;
      }
      if (request == null ||
          !controller.claimInteractionResponse(responseKey)) {
        continue;
      }
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted) {
          controller.releaseInteractionResponse(responseKey);
          return;
        }
        unawaited(
          _showInteractionResponse(interactionRef, response, request, state),
        );
      });
    }
  }

  List<MobileInteractionResponse> _privateResponses(
    MobileState state,
    String interactionRef,
  ) =>
      state.interactionResponses.values
          .where((response) =>
              response.interactionRef.wire == interactionRef &&
              response.ephemeral &&
              response.deletedAt == null &&
              const <int>{4, 5}.contains(response.callbackType) &&
              response.hasMessageContent &&
              !ref
                  .read(mobileControllerProvider.notifier)
                  .interactionResponseDismissed(response.storageKey))
          .toList(growable: false)
        ..sort((left, right) => left.sequence.compareTo(right.sequence));

  Future<void> _showInteractionResponse(
    String interactionRef,
    MobileInteractionResponse response,
    MobileInteractionRequest request,
    MobileState state,
  ) async {
    final controller = ref.read(mobileControllerProvider.notifier);
    if (response.modal case final modal?) {
      await showDialog<void>(
        context: context,
        barrierDismissible: false,
        builder: (context) => _InteractionModalDialog(
          modal: modal,
          state: state,
          encrypted: state.activeChannel?.ref == request.channel &&
              state.activeChannel?.encryptionMode == 'e2ee',
          onSubmit: (submission) async {
            final channel = ref.read(mobileControllerProvider).activeChannel;
            if (channel == null || channel.ref != request.channel) {
              throw UserInputException(
                'Return to the originating channel before submitting this form.',
              );
            }
            if (!mobileApplicationIntegrationAllowedByUsePermission(
              request.integrationType,
              canUseApplicationCommands(channel),
            )) {
              throw UserInputException(
                'This guild-installed app form is unavailable in this channel.',
              );
            }
            MobilePreparedEncryptedInteraction? encrypted;
            if (channel.encryptionMode == 'e2ee') {
              if (request.integrationType == null ||
                  request.interactionContext == null) {
                throw UserInputException(
                  'Run the command again before submitting this encrypted form.',
                );
              }
              encrypted = await _prepareEncryptedInteraction(
                channel,
                application: request.application,
                integrationType: request.integrationType!,
                interactionContext: request.interactionContext!,
                interactionType: 'modal_submit',
                customId: modal.customId,
                responseId: response.responseId,
                attachmentIds: submission.attachmentIds,
                attachmentManifests: submission.manifests,
                components: submission.components,
              );
            }
            final acknowledged =
                await controller.repository.submitInteractionModal(
              channel: request.channel,
              application: request.application,
              responseId: response.responseId,
              customId: modal.customId,
              components: submission.components,
              encryptedPayload: encrypted?.envelope,
              attachmentIds: encrypted?.attachmentIds ?? const <String>[],
            );
            controller.rememberInteractionRequest(
              _interactionAcknowledgementRef(acknowledged),
              channel: request.channel,
              application: request.application,
              integrationType: request.integrationType,
              interactionContext: request.interactionContext,
            );
          },
        ),
      );
      return;
    }
    if (response.ephemeral &&
        response.callbackType == 5 &&
        !response.hasMessageContent &&
        mounted) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Row(children: [
          SizedBox.square(
            dimension: 14,
            child: CircularProgressIndicator(strokeWidth: 2),
          ),
          SizedBox(width: 10),
          Text('Bot is thinking…'),
        ]),
        duration: Duration(seconds: 10),
      ));
      return;
    }
    if (response.ephemeral &&
        const <int>{4, 5}.contains(response.callbackType) &&
        response.hasMessageContent &&
        mounted) {
      final responses = _privateResponses(state, interactionRef);
      if (responses.isEmpty) return;
      final _ActiveEphemeralResponse value = (
        interactionRef: interactionRef,
        responses: responses,
        request: request,
        state: state,
      );
      if (_activeEphemeralResponse case final active?) {
        active.value = value;
        return;
      }
      final liveResponse = ValueNotifier<_ActiveEphemeralResponse?>(value);
      _activeEphemeralResponse = liveResponse;
      try {
        await showModalBottomSheet<void>(
          context: context,
          isScrollControlled: true,
          showDragHandle: true,
          builder: (sheetContext) => ValueListenableBuilder(
            valueListenable: liveResponse,
            builder: (context, current, _) {
              if (current == null) {
                WidgetsBinding.instance.addPostFrameCallback((_) {
                  if (sheetContext.mounted && Navigator.canPop(sheetContext)) {
                    Navigator.pop(sheetContext);
                  }
                });
                return SizedBox.shrink();
              }
              return SafeArea(
                child: ConstrainedBox(
                  constraints: BoxConstraints(
                    maxHeight:
                        min(MediaQuery.sizeOf(context).height * .86, 760),
                  ),
                  child: ListView.separated(
                    shrinkWrap: true,
                    padding: EdgeInsets.fromLTRB(
                      18,
                      4,
                      18,
                      max(18, MediaQuery.viewInsetsOf(context).bottom + 18),
                    ),
                    itemCount: current.responses.length,
                    separatorBuilder: (_, __) => Padding(
                      padding: EdgeInsets.symmetric(vertical: 12),
                      child: Divider(height: 1),
                    ),
                    itemBuilder: (context, index) {
                      final response = current.responses[index];
                      return _EphemeralInteractionResponse(
                        data: response.data,
                        state: current.state,
                        allowExternalMedia:
                            current.request.encryptionChannel == null,
                        onPollVote: (answerId, selected) =>
                            _setInteractionPollVote(
                          response,
                          answerId,
                          selected,
                        ),
                        onPollVoters: () =>
                            _showInteractionPollVoters(response),
                        onComponent: (component, values) =>
                            _invokeEphemeralComponent(
                          response,
                          current.request,
                          component,
                          values,
                        ),
                      );
                    },
                  ),
                ),
              );
            },
          ),
        );
      } finally {
        if (identical(_activeEphemeralResponse, liveResponse)) {
          if (liveResponse.value case final dismissed?) {
            ref
                .read(mobileControllerProvider.notifier)
                .dismissInteractionResponses(
                  dismissed.responses.map((response) => response.storageKey),
                );
          }
          _activeEphemeralResponse = null;
        }
        liveResponse.dispose();
      }
    }
  }

  Future<void> _invokeEphemeralComponent(
    MobileInteractionResponse response,
    MobileInteractionRequest request,
    RichComponent component,
    List<String> values,
  ) async {
    final responseId = response.responseId;
    final customId = component.customId;
    final viewVersion = switch (response.data['view_version']) {
      final int value => value,
      final num value => value.toInt(),
      final Object value => int.tryParse('$value'),
      null => null,
    };
    final controller = ref.read(mobileControllerProvider.notifier);
    try {
      if (customId == null || viewVersion == null) {
        throw UserInputException(
          'This private control expired. Run the command again.',
        );
      }
      final channel = ref.read(mobileControllerProvider).activeChannel;
      if (channel == null || channel.ref != request.channel) {
        throw UserInputException(
            'Return to this channel before using the control.');
      }
      if (!mobileApplicationIntegrationAllowedByUsePermission(
        request.integrationType,
        canUseApplicationCommands(channel),
      )) {
        throw UserInputException(
          'This guild-installed app control is unavailable in this channel.',
        );
      }
      MobilePreparedEncryptedInteraction? encrypted;
      if (channel.encryptionMode == 'e2ee') {
        if (request.integrationType == null ||
            request.interactionContext == null) {
          throw UserInputException(
            'Run the command again before using this encrypted control.',
          );
        }
        encrypted = await _prepareEncryptedInteraction(
          channel,
          application: request.application,
          integrationType: request.integrationType!,
          interactionContext: request.interactionContext!,
          interactionType: 'component',
          componentType: component.type,
          customId: customId,
          responseId: responseId,
          viewVersion: viewVersion,
          values: values,
        );
      }
      final acknowledged = await controller.repository.invokeEphemeralComponent(
        channel: request.channel,
        application: request.application,
        responseId: responseId,
        viewVersion: viewVersion,
        customId: customId,
        values: values,
        encryptedPayload: encrypted?.envelope,
      );
      controller.rememberInteractionRequest(
        _interactionAcknowledgementRef(acknowledged),
        channel: request.channel,
        application: request.application,
        integrationType: request.integrationType,
        interactionContext: request.interactionContext,
      );
    } on Object catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(userFacingError(
          error,
          summary: 'The bot did not receive that private interaction',
        )),
      ));
    }
  }

  Future<void> _setPollVote(
    KaedeMessage message,
    int answerId,
    bool selected,
  ) async {
    try {
      await ref.read(mobileControllerProvider.notifier).repository.setPollVote(
            channel: message.channelRef,
            message: message.ref,
            answerId: answerId,
            selected: selected,
          );
    } on Object catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content:
            Text(userFacingError(error, summary: 'Could not update your vote')),
      ));
    }
  }

  Future<void> _showPollVoters(KaedeMessage message) async {
    if (message.poll == null || !mounted) return;
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      builder: (context) => _PollVotersSheet(
        poll: message.poll!,
        load: (answerId, after) =>
            ref.read(mobileControllerProvider.notifier).repository.pollVoters(
                  channel: message.channelRef,
                  message: message.ref,
                  answerId: answerId,
                  after: after,
                ),
      ),
    );
  }

  Future<void> _setInteractionPollVote(
    MobileInteractionResponse response,
    int answerId,
    bool selected,
  ) async {
    try {
      await ref
          .read(mobileControllerProvider.notifier)
          .repository
          .setInteractionPollVote(
            interactionId: response.interactionRef.wire,
            responseId: response.responseRef.wire,
            answerId: answerId,
            selected: selected,
          );
    } on Object catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(userFacingError(
          error,
          summary: 'Could not update your private poll vote',
        )),
      ));
    }
  }

  Future<void> _showInteractionPollVoters(
    MobileInteractionResponse response,
  ) async {
    final poll = interactionResponsePoll(response.data);
    if (poll == null || !mounted) return;
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      builder: (context) => _PollVotersSheet(
        poll: poll,
        load: (answerId, after) => ref
            .read(mobileControllerProvider.notifier)
            .repository
            .interactionPollVoters(
              interactionId: response.interactionRef.wire,
              responseId: response.responseRef.wire,
              answerId: answerId,
              after: after,
            ),
      ),
    );
  }

  Future<({List<KaedeChannel> destinations, String note})?>
      _forwardDestinationPicker(
    KaedeChannel source,
    List<KaedeChannel> destinations,
  ) async {
    final selected = <EntityRef>{};
    final note = TextEditingController();
    try {
      return await showModalBottomSheet<
          ({List<KaedeChannel> destinations, String note})>(
        context: context,
        isScrollControlled: true,
        useSafeArea: true,
        showDragHandle: true,
        builder: (context) => StatefulBuilder(
          builder: (context, setSheetState) => ConstrainedBox(
            constraints: BoxConstraints(
              maxHeight: min(MediaQuery.sizeOf(context).height * .8, 680),
            ),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                ListTile(
                  title: Text(
                    'Forward message',
                    style: TextStyle(fontWeight: FontWeight.w800),
                  ),
                  subtitle: Text(
                    'Choose up to 5 destinations · ${selected.length}/5. '
                    'The snapshot does not include the original author.',
                  ),
                ),
                Flexible(
                  child: ListView.builder(
                    shrinkWrap: true,
                    itemCount: destinations.length,
                    itemBuilder: (context, index) {
                      final destination = destinations[index];
                      final checked = selected.contains(destination.ref);
                      return CheckboxListTile(
                        key: ValueKey(
                            'forward-destination-${destination.ref.wire}'),
                        value: checked,
                        secondary: Icon(destination.isThread
                            ? Icons.forum_outlined
                            : destination.guildRef == null
                                ? Icons.chat_bubble_outline_rounded
                                : Icons.tag_rounded),
                        title: Text(_forwardDestinationLabel(destination)),
                        subtitle: destination.ref == source.ref
                            ? Text('Current conversation')
                            : destination.isThread
                                ? Text('Thread')
                                : null,
                        onChanged: !checked && selected.length >= 5
                            ? null
                            : (value) => setSheetState(() {
                                  if (value == true) {
                                    selected.add(destination.ref);
                                  } else {
                                    selected.remove(destination.ref);
                                  }
                                }),
                      );
                    },
                  ),
                ),
                Padding(
                  padding: EdgeInsets.fromLTRB(
                    16,
                    8,
                    16,
                    12 + MediaQuery.viewInsetsOf(context).bottom,
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      TextField(
                        controller: note,
                        maxLength: 4000,
                        minLines: 1,
                        maxLines: 3,
                        decoration: InputDecoration(
                          labelText: 'Add a note (optional)',
                        ),
                      ),
                      SizedBox(height: 8),
                      FilledButton(
                        onPressed: selected.isEmpty
                            ? null
                            : () => Navigator.pop(
                                  context,
                                  (
                                    destinations: destinations
                                        .where((item) =>
                                            selected.contains(item.ref))
                                        .toList(growable: false),
                                    note: note.text,
                                  ),
                                ),
                        child: Text(selected.length > 1
                            ? 'Send (${selected.length})'
                            : 'Send'),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
      );
    } finally {
      note.dispose();
    }
  }

  String _forwardDestinationLabel(KaedeChannel channel) {
    final name = channel.name?.trim();
    if (name?.isNotEmpty == true) {
      return channel.guildRef == null ? name! : '#$name';
    }
    if (channel.guildRef == null) {
      final recipients = channel.recipients.map((user) => user.name).join(', ');
      return recipients.isEmpty ? 'This conversation' : recipients;
    }
    return channel.isThread ? 'Thread' : 'Channel';
  }

  Future<void> _addReactionFromPicker(KaedeMessage message) async {
    final emoji =
        await _showReactionPicker(ref.read(mobileControllerProvider).user?.ref);
    if (!mounted || emoji == null) return;
    await _toggleReaction(message, emoji);
  }

  Future<String?> _showReactionPicker(EntityRef? user) async {
    final channel = ref.read(mobileControllerProvider).activeChannel;
    if (channel == null) return null;
    final recent = await _recentReactions(user);
    if (!mounted ||
        ref.read(mobileControllerProvider).activeChannel?.ref != channel.ref) {
      return null;
    }
    final selected = await showReactionEmojiPicker(
      context,
      repository: ref.read(mobileControllerProvider.notifier).repository,
      channel: channel,
      categories: defaultComposerEmojiCategories,
      recent: recent,
    );
    if (!mounted ||
        ref.read(mobileControllerProvider).activeChannel?.ref != channel.ref) {
      return null;
    }
    return selected;
  }

  Future<void> _showChannelUserProfile(KaedeUser user) async {
    final state = ref.read(mobileControllerProvider);
    final channel = state.activeChannel;
    final commands = channel != null && !channel.archived
        ? mobileUserContextCommands(_applicationCommands.where(
            (command) => mobileApplicationCommandAllowedByChannelPermissions(
                  command,
                  canUseApplicationCommands(channel),
                  _canSendUserContextCommands(channel),
                )))
        : const <MobileApplicationCommand>[];
    await showUserProfile(
      context,
      user,
      ref.read(mobileControllerProvider.notifier).presenceFor(user),
      actions: [
        if (channel != null && commands.isNotEmpty)
          OutlinedButton.icon(
            icon: Icon(Icons.apps_rounded),
            label: Text('Apps'),
            onPressed: () {
              Navigator.of(context).pop();
              unawaited(Future<void>.delayed(Duration.zero, () async {
                if (!mounted) return;
                await _chooseAndInvokeContextCommand(
                  channel,
                  commands,
                  user: user.ref,
                );
              }));
            },
          ),
      ],
    );
  }

  Future<void> _chooseAndInvokeContextCommand(
    KaedeChannel channel,
    List<MobileApplicationCommand> commands, {
    required EntityRef user,
    EntityRef? message,
  }) async {
    if (commands.isEmpty || !mounted) return;
    final account = ref.read(mobileControllerProvider).user?.ref;
    if (account == null) return;
    final history = await _contextCommandHistory(account);
    var current = ref.read(mobileControllerProvider);
    if (!mounted ||
        current.user?.ref != account ||
        current.activeChannel?.ref != channel.ref) {
      return;
    }
    final command = await showModalBottomSheet<MobileApplicationCommand>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: context.kaede.panel,
      builder: (context) => _ApplicationCommandPickerSheet(
        commands: commands,
        contextCommands: true,
        contextHistory: history,
      ),
    );
    current = ref.read(mobileControllerProvider);
    if (command == null ||
        !mounted ||
        current.user?.ref != account ||
        current.activeChannel?.ref != channel.ref) {
      return;
    }
    try {
      if (!mobileApplicationCommandAllowedByChannelPermissions(
        command,
        canUseApplicationCommands(channel),
        _canSendUserContextCommands(channel),
      )) {
        throw UserInputException(
          'This guild-installed command is unavailable in this channel.',
        );
      }
      final target = mobileContextCommandTarget(
        command,
        user: user,
        message: message,
      );
      final encrypted = await _prepareEncryptedInteraction(
        channel,
        application: command.application,
        integrationType: command.integrationType,
        interactionContext: command.interactionContext,
        interactionType: 'command',
        commandId: command.id,
        commandName: command.name,
        commandType: command.type,
        target: target,
      );
      final controller = ref.read(mobileControllerProvider.notifier);
      final acknowledged = await controller.repository.invokeApplicationCommand(
        channel: channel.ref,
        application: command.application,
        commandId: command.id,
        integrationType: command.integrationType,
        dmCapabilityId: command.dmCapabilityId,
        dmCapabilityRevision: command.dmCapabilityRevision,
        name: command.name,
        type: command.type,
        target: target,
        encryptedPayload: encrypted?.envelope,
      );
      controller.rememberInteractionRequest(
        _interactionAcknowledgementRef(acknowledged),
        channel: channel.ref,
        application: command.application,
        integrationType: command.integrationType,
        interactionContext: command.interactionContext,
      );
      await _rememberContextCommand(account, command);
      await _rememberApplication(account, command.application);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(
            '${command.displayName(Localizations.localeOf(context).toLanguageTag())} sent to ${command.applicationName}.',
          ),
        ));
      }
    } on Object catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(userFacingError(
          error,
          summary: 'The app command could not be delivered',
        )),
      ));
    }
  }

  Future<void> _showMessageActions(
    KaedeMessage message, {
    KaedeAttachment? attachment,
    Map<String, Object?>? attachmentManifest,
    File? decryptedAttachment,
  }) async {
    if (message.deletedAt != null) return;
    final displayedAttachment = attachment == null
        ? null
        : _manifestAttachment(attachment, attachmentManifest);
    final mobileState = ref.read(mobileControllerProvider);
    final me = mobileState.user?.ref;
    final channel = mobileState.activeChannel!;
    final channelFollowNotice = message.messageType == 12;
    final canReact = !channelFollowNotice &&
        canAddMessageReaction(channel, emojiExists: false);
    final canManage = channel.type == ChannelType.dm ||
        channel.type == ChannelType.groupDm ||
        channel.allows(Permission.manageMessages);
    final canPublish = !channelFollowNotice &&
        !_publishingMessages.contains(message.ref) &&
        mobileState.activeGuild != null &&
        canPublishAnnouncementMessage(
          mobileState.activeGuild!,
          channel,
          message,
          mobileState.user,
        );
    final canManageReactions =
        !channelFollowNotice && canClearMessageReactions(channel);
    final canPin = canReadRetainedChannelHistory(channel) &&
        canPinMessage(channel, message);
    final canDelete = (message.authorRef == me || canManage) &&
        (!channel.archived || !channel.locked || canManageThreads(channel));
    final canStartThread =
        !channelFollowNotice && canStartThreadFromMessage(channel);
    final forwardUnavailable = forwardMessageUnavailableReason(message);
    final forwardDestinations = forwardUnavailable == null
        ? forwardDestinationChannels(mobileState, channel)
        : const <KaedeChannel>[];
    final poll = channelFollowNotice ? null : message.poll;
    final canEndPoll =
        poll != null && !poll.isClosed() && message.authorRef == me;
    final recent = canReact ? await _recentReactions(me) : const <String>[];
    final sentGif =
        channelFollowNotice ? null : composerGifFromMessage(message.content);
    final gifFavorite = sentGif == null
        ? false
        : (await loadComposerGifFavorites()).any((item) =>
            item.id == sentGif.id ||
            item.url.toString() == sentGif.url.toString());
    final contextCommands = !channelFollowNotice
        ? mobileMessageContextCommands(_applicationCommands.where(
            (command) => mobileApplicationCommandAllowedByChannelPermissions(
                  command,
                  canUseApplicationCommands(channel),
                  _canSendUserContextCommands(channel),
                )))
        : const <MobileApplicationCommand>[];
    if (!mounted) return;
    final action = await showModalBottomSheet<String>(
      context: context,
      showDragHandle: true,
      builder: (context) => SafeArea(
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              _MessageActionsHeader(
                message: message,
                attachment: displayedAttachment,
              ),
              if (displayedAttachment != null) ...[
                ListTile(
                  leading: Icon(Icons.link_rounded),
                  title: Text('Copy media link'),
                  onTap: () => Navigator.pop(context, 'copy-media-link'),
                ),
                ListTile(
                  leading: Icon(Icons.info_outline_rounded),
                  title: Text(displayedAttachment.filename),
                  subtitle: Text(
                    '${displayedAttachment.contentType} · '
                    '${formatAttachmentSize(displayedAttachment.size)}',
                  ),
                ),
              ],
              if (canReact && recent.isNotEmpty)
                Padding(
                  padding: EdgeInsets.fromLTRB(12, 4, 12, 8),
                  child: Row(
                    children: [
                      for (final emoji in recent)
                        Expanded(
                          child: Padding(
                            padding: EdgeInsets.symmetric(horizontal: 3),
                            child: Material(
                              color: context.kaede.raised,
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
                                        Border.all(color: context.kaede.border),
                                  ),
                                  child: ReactionEmojiGlyph(
                                    emoji: emoji,
                                    size: 22,
                                  ),
                                ),
                              ),
                            ),
                          ),
                        ),
                      Padding(
                        padding: EdgeInsets.symmetric(horizontal: 3),
                        child: IconButton(
                          tooltip: 'More emoji',
                          onPressed: () =>
                              Navigator.pop(context, 'react-picker'),
                          icon: Icon(Icons.add_reaction_outlined),
                        ),
                      ),
                    ],
                  ),
                ),
              if (!channelFollowNotice)
                ListTile(
                    leading: Icon(Icons.reply_rounded),
                    title: Text('Reply'),
                    onTap: () => Navigator.pop(context, 'reply')),
              if (canStartThread)
                ListTile(
                  leading: Icon(Icons.forum_outlined),
                  title: Text('Create Thread'),
                  onTap: () => Navigator.pop(context, 'create-thread'),
                ),
              if (!channelFollowNotice && message.content?.isNotEmpty == true)
                ListTile(
                    leading: Icon(Icons.copy_rounded),
                    title: Text('Copy text'),
                    onTap: () => Navigator.pop(context, 'copy')),
              ListTile(
                  leading: Icon(Icons.link_rounded),
                  title: Text('Copy message link'),
                  onTap: () => Navigator.pop(context, 'copy-link')),
              if (forwardDestinations.isNotEmpty)
                ListTile(
                  leading: Icon(Icons.forward_rounded),
                  title: Text('Forward'),
                  subtitle: Text('Share an author-free snapshot copy'),
                  onTap: () => Navigator.pop(context, 'forward'),
                ),
              if (!channelFollowNotice && forwardUnavailable != null)
                ListTile(
                  enabled: false,
                  leading: Icon(Icons.forward_rounded),
                  title: Text('Forward'),
                  subtitle: Text(forwardUnavailable),
                ),
              if (canPublish)
                ListTile(
                  leading: Icon(Icons.campaign_outlined),
                  title: Text('Publish message'),
                  subtitle: Text(
                    'Send to this announcement channel’s followers',
                  ),
                  onTap: () => Navigator.pop(context, 'publish'),
                ),
              if (sentGif != null)
                ListTile(
                  leading: Icon(gifFavorite
                      ? Icons.star_rounded
                      : Icons.star_border_rounded),
                  title: Text(gifFavorite
                      ? 'Remove from GIF favorites'
                      : 'Add to GIF favorites'),
                  onTap: () => Navigator.pop(context, 'gif-favorite'),
                ),
              if (canReact)
                ListTile(
                    leading: Icon(Icons.add_reaction_outlined),
                    title: Text('Add reaction'),
                    trailing: Icon(Icons.chevron_right_rounded),
                    onTap: () => Navigator.pop(context, 'react-picker')),
              if (!channelFollowNotice && message.reactionCounts.isNotEmpty)
                ListTile(
                    leading: Icon(Icons.people_outline_rounded),
                    title: Text('View reactions'),
                    trailing: Icon(Icons.chevron_right_rounded),
                    onTap: () => Navigator.pop(context, 'view-reactions')),
              if (poll != null && poll.totalVotes > 0)
                ListTile(
                  leading: Icon(Icons.how_to_vote_outlined),
                  title: Text('View poll voters'),
                  trailing: Icon(Icons.chevron_right_rounded),
                  onTap: () => Navigator.pop(context, 'poll-voters'),
                ),
              if (canEndPoll)
                ListTile(
                  leading: Icon(Icons.stop_circle_outlined),
                  title: Text('End poll'),
                  onTap: () => Navigator.pop(context, 'end-poll'),
                ),
              if (contextCommands.isNotEmpty && !channel.archived)
                ListTile(
                  leading: Icon(Icons.apps_rounded),
                  title: Text('Apps'),
                  trailing: Icon(Icons.chevron_right_rounded),
                  onTap: () => Navigator.pop(context, 'app-command'),
                ),
              if (canPin)
                ListTile(
                    leading: Icon(message.pinned
                        ? Icons.push_pin_rounded
                        : Icons.push_pin_outlined),
                    title:
                        Text(message.pinned ? 'Unpin message' : 'Pin message'),
                    onTap: () => Navigator.pop(context, 'pin')),
              if (!channel.archived &&
                  (!channel.locked || canManageThreads(channel)) &&
                  !channelFollowNotice &&
                  message.authorRef == me &&
                  message.clientContentAvailable)
                ListTile(
                    leading: Icon(Icons.edit_outlined),
                    title: Text('Edit message'),
                    onTap: () => Navigator.pop(context, 'edit')),
              if (!channelFollowNotice && message.authorRef != me)
                ListTile(
                  leading: Icon(Icons.flag_outlined),
                  title: Text('Report message'),
                  subtitle: displayedAttachment == null
                      ? null
                      : Text(
                          'Include ${displayedAttachment.filename} as context',
                        ),
                  onTap: () => Navigator.pop(context, 'report'),
                ),
              if (mobileState.developerMode)
                ListTile(
                  leading: Icon(Icons.badge_outlined),
                  title: Text('Copy message ID'),
                  onTap: () => Navigator.pop(context, 'copy-id'),
                ),
              if (canDelete)
                ListTile(
                  leading: Icon(Icons.delete_outline_rounded,
                      color: context.kaede.danger),
                  title: Text('Delete message',
                      style: TextStyle(color: context.kaede.danger)),
                  onTap: () => Navigator.pop(context, 'delete'),
                ),
              SizedBox(height: 6),
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
        case 'create-thread':
          final name = await _threadNameDialog();
          if (name == null) break;
          final created = await controller.createThreadFromMessage(
            parent: channel,
            message: message,
            name: name,
          );
          await controller.selectChannel(created);
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
                SnackBar(content: Text('Message link copied.')),
              );
            }
          }
          break;
        case 'copy-media-link':
          final instance = controller.api.tokens?.instance.value;
          if (instance != null && attachment != null) {
            await Clipboard.setData(ClipboardData(
              text: 'https://$instance${attachmentMediaPath(
                attachment.ref,
                historyMediaUrl: attachment.historyMediaUrl,
              )}',
            ));
          }
          break;
        case 'copy-id':
          await copyDeveloperId(
            context,
            value: message.ref.wire,
            label: 'Message',
          );
          break;
        case 'forward':
          final selection = await _forwardDestinationPicker(
            channel,
            forwardDestinations,
          );
          if (selection == null || !mounted) break;
          final disclosesEncryptedSnapshot = channel.encryptionMode == 'e2ee' &&
              selection.destinations.any(
                (item) => item.encryptionMode == 'plaintext',
              );
          if (disclosesEncryptedSnapshot) {
            final confirmed = await showDialog<bool>(
              context: context,
              builder: (context) => AlertDialog(
                title: Text('Share decrypted snapshot?'),
                content: Text(
                  'At least one destination is not end-to-end encrypted. '
                  'Kaede will disclose an author-free copy of this message '
                  'and its files to those conversations.',
                ),
                actions: [
                  TextButton(
                    onPressed: () => Navigator.pop(context, false),
                    child: Text('Cancel'),
                  ),
                  FilledButton(
                    onPressed: () => Navigator.pop(context, true),
                    child: Text('Share snapshot'),
                  ),
                ],
              ),
            );
            if (confirmed != true || !mounted) break;
          }
          final result = await controller.forwardMessage(
            message,
            selection.destinations.map((item) => item.ref).toList(),
            content: selection.note,
            discloseEncryptedToPlaintext: disclosesEncryptedSnapshot,
          );
          if (mounted) {
            ScaffoldMessenger.of(context).showSnackBar(SnackBar(
              content: Text(
                result.failures.isEmpty
                    ? 'Forwarded to ${result.forwards.length} '
                        'destination${result.forwards.length == 1 ? '' : 's'}.'
                    : 'Forwarded to ${result.forwards.length}; '
                        '${result.failures.length} destination'
                        '${result.failures.length == 1 ? '' : 's'} failed.',
              ),
            ));
          }
          break;
        case 'publish':
          if (_publishingMessages.add(message.ref)) {
            if (mounted) setState(() {});
            try {
              await controller.publishAnnouncement(message);
              if (mounted) {
                ScaffoldMessenger.of(context).showSnackBar(
                  SnackBar(
                    content: Text(
                      'Message published to this announcement channel’s followers.',
                    ),
                  ),
                );
              }
            } finally {
              _publishingMessages.remove(message.ref);
              if (mounted) setState(() {});
            }
          }
          break;
        case 'react-picker':
          final emoji = await _showReactionPicker(me);
          if (emoji != null) await _toggleReaction(message, emoji);
          break;
        case 'gif-favorite':
          if (sentGif != null) {
            await toggleComposerGifFavorite(sentGif);
            if (mounted) {
              ScaffoldMessenger.of(context).showSnackBar(SnackBar(
                content: Text(gifFavorite
                    ? 'GIF removed from favorites.'
                    : 'GIF added to favorites.'),
              ));
            }
          }
          break;
        case 'app-command':
          await _chooseAndInvokeContextCommand(
            channel,
            contextCommands,
            user: message.authorRef,
            message: message.ref,
          );
          break;
        case 'view-reactions':
          await showModalBottomSheet<void>(
            context: context,
            isScrollControlled: true,
            showDragHandle: true,
            builder: (context) => _ReactionViewerSheet(
              message: message,
              repository: controller.repository,
              canManage: canManageReactions,
              onClear: canManageReactions
                  ? (emoji) => controller.clearMessageReactions(
                        message,
                        emoji: emoji,
                      )
                  : null,
            ),
          );
          break;
        case 'poll-voters':
          await _showPollVoters(message);
          break;
        case 'end-poll':
          final confirmed = await showDialog<bool>(
            context: context,
            builder: (context) => AlertDialog(
              title: Text('End this poll?'),
              content: Text(
                'Voting will close immediately and the final results will remain visible.',
              ),
              actions: [
                TextButton(
                  onPressed: () => Navigator.pop(context, false),
                  child: Text('Keep open'),
                ),
                FilledButton(
                  onPressed: () => Navigator.pop(context, true),
                  child: Text('End poll'),
                ),
              ],
            ),
          );
          if (confirmed == true) {
            await controller.finalizePoll(message);
            if (mounted) {
              ScaffoldMessenger.of(context).showSnackBar(
                SnackBar(content: Text('Poll ended.')),
              );
            }
          }
          break;
        case 'pin':
          final pinning = !message.pinned;
          final confirmed = await showDialog<bool>(
            context: context,
            builder: (context) => AlertDialog(
              title: Text(pinning ? 'Pin this message?' : 'Remove this pin?'),
              content: Text(pinning
                  ? 'Everyone in this conversation will be able to find it in Pins.'
                  : 'The message will remain in the conversation.'),
              actions: [
                TextButton(
                  onPressed: () => Navigator.pop(context, false),
                  child: Text('Cancel'),
                ),
                FilledButton(
                  onPressed: () => Navigator.pop(context, true),
                  child: Text(pinning ? 'Pin' : 'Remove pin'),
                ),
              ],
            ),
          );
          if (confirmed == true) {
            await controller.setMessagePinned(message, pinning);
          }
          break;
        case 'edit':
          final edited = await _editDialog(message.content ?? '');
          if (edited != null) await controller.replaceMessage(message, edited);
          break;
        case 'delete':
          await controller.removeMessage(message);
          break;
        case 'report':
          await _reportMessageDialog(
            message,
            attachment: attachment,
            attachmentManifest: attachmentManifest,
            decryptedAttachment: decryptedAttachment,
          );
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
        title: Text('Edit message'),
        content: TextField(
            controller: input, autofocus: true, minLines: 2, maxLines: 8),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context), child: Text('Cancel')),
          FilledButton(
              onPressed: () => Navigator.pop(context, input.text.trim()),
              child: Text('Save')),
        ],
      ),
    );
  }

  Future<String?> _threadNameDialog() {
    final input = TextEditingController();
    return showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('Create Thread'),
        content: TextField(
          controller: input,
          autofocus: true,
          maxLength: 100,
          decoration: InputDecoration(
            labelText: 'Thread name',
            counterText: '',
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: Text('Cancel'),
          ),
          ValueListenableBuilder<TextEditingValue>(
            valueListenable: input,
            builder: (context, value, _) => FilledButton(
              onPressed: value.text.trim().isEmpty
                  ? null
                  : () => Navigator.pop(context, value.text.trim()),
              child: Text('Create'),
            ),
          ),
        ],
      ),
    );
  }

  Future<void> _reportMessageDialog(
    KaedeMessage message, {
    KaedeAttachment? attachment,
    Map<String, Object?>? attachmentManifest,
    File? decryptedAttachment,
  }) async {
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
    var activity = '';
    var uploadProgress = 0.0;
    String? createdReportId;
    Map<String, Object?>? evidenceTicket;
    var evidenceUploaded = false;
    File? ownedEvidenceFile;
    final encryptedEvidenceAvailable =
        encryptedReportEvidenceAvailable(message);
    final focusedAttachment = attachment != null;
    final requiresAttachmentDisclosure = attachmentManifest != null;
    final attachmentDisclosureAvailable = !requiresAttachmentDisclosure ||
        decryptedAttachment != null ||
        ('${attachmentManifest['attachment_id']}' == attachment!.ref.id.value &&
            '${attachmentManifest['attachment_domain']}' ==
                attachment.ref.domain.value);
    final requiresDisclosure =
        message.e2ee != null || requiresAttachmentDisclosure;
    final attachmentLabel =
        '${attachmentManifest?['filename'] ?? attachment?.filename ?? 'Attachment'}';
    final description = TextEditingController();
    try {
      await showDialog<void>(
        context: context,
        barrierDismissible: false,
        builder: (dialogContext) => StatefulBuilder(
          builder: (context, setDialogState) => AlertDialog(
            title: Text('Report message'),
            content: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (focusedAttachment) ...[
                    ListTile(
                      contentPadding: EdgeInsets.zero,
                      leading: Icon(
                        attachment.contentType.startsWith('video/')
                            ? Icons.movie_outlined
                            : attachment.contentType.startsWith('image/')
                                ? Icons.image_outlined
                                : Icons.attach_file_rounded,
                      ),
                      title: Text(attachmentLabel),
                      subtitle: Text(
                        '${attachmentManifest?['content_type'] ?? attachment.contentType} · '
                        '${formatAttachmentSize((attachmentManifest?['plaintext_size'] as num?)?.toInt() ?? attachment.size)}',
                      ),
                    ),
                    SizedBox(height: 4),
                  ],
                  if (requiresAttachmentDisclosure) ...[
                    Text(
                      !attachmentDisclosureAvailable
                          ? 'This encrypted attachment is not available on this device yet. Wait for it to decrypt, then try again.'
                          : message.e2ee != null && !encryptedEvidenceAvailable
                              ? 'The selected attachment is available, but the encrypted message text has not decrypted on this device. Wait for it to decrypt, then try again.'
                              : 'Reporting shares the complete message and uploads an unencrypted copy of $attachmentLabel to the channel’s moderation authority. Encryption keys and other messages are never sent.',
                    ),
                  ] else if (message.e2ee != null) ...[
                    Text(
                      encryptedEvidenceAvailable
                          ? 'This message is end-to-end encrypted. Reporting shares the decrypted message text shown on this device and metadata for all of its attachments with the channel’s moderation authority. Attachment-only messages have empty disclosed text and can still be reported. Encryption keys and decrypted file contents are not sent unless an attachment is selected directly.'
                          : 'This encrypted message has not decrypted on this device. Wait for its authenticated message evidence to decrypt, then try again.',
                    ),
                  ] else if (focusedAttachment)
                    Text(
                      'This reports the complete message and all of its attachments. The selected attachment will be highlighted for moderators.',
                    )
                  else
                    Text(
                      'The message text and metadata for all of its attachments will be sent to the channel’s moderation authority.',
                    ),
                  if (requiresDisclosure) ...[
                    SizedBox(height: 12),
                    CheckboxListTile(
                      contentPadding: EdgeInsets.zero,
                      value: disclose,
                      onChanged: submitting ||
                              (message.e2ee != null &&
                                  !encryptedEvidenceAvailable) ||
                              !attachmentDisclosureAvailable
                          ? null
                          : (value) =>
                              setDialogState(() => disclose = value == true),
                      title: Text(
                        requiresAttachmentDisclosure
                            ? 'I understand the decrypted message and selected file will be disclosed unencrypted.'
                            : 'I understand the decrypted message text will be disclosed.',
                      ),
                    ),
                  ],
                  SizedBox(height: 12),
                  DropdownButtonFormField<String>(
                    initialValue: category,
                    decoration: InputDecoration(labelText: 'Reason'),
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
                  SizedBox(height: 12),
                  TextField(
                    controller: description,
                    enabled: !submitting,
                    maxLength: 2000,
                    maxLines: 4,
                    decoration: InputDecoration(
                      labelText: 'Additional details (optional)',
                    ),
                  ),
                  if (submitting) ...[
                    SizedBox(height: 8),
                    LinearProgressIndicator(
                      value: activity == 'Uploading decrypted evidence…'
                          ? uploadProgress
                          : null,
                    ),
                    SizedBox(height: 6),
                    Text(
                      activity,
                      style: TextStyle(
                        color: context.kaede.muted,
                        fontSize: 12,
                      ),
                    ),
                  ],
                ],
              ),
            ),
            actions: [
              TextButton(
                onPressed:
                    submitting ? null : () => Navigator.pop(dialogContext),
                child: Text('Cancel'),
              ),
              FilledButton(
                onPressed: submitting ||
                        !canSubmitMessageReport(
                          message,
                          disclosureAcknowledged: disclose,
                          requiresAttachmentDisclosure:
                              requiresAttachmentDisclosure,
                          attachmentDisclosureAvailable:
                              attachmentDisclosureAvailable,
                        )
                    ? null
                    : () async {
                        setDialogState(() => submitting = true);
                        try {
                          final repository = ref
                              .read(mobileControllerProvider.notifier)
                              .repository;
                          var evidenceFile = decryptedAttachment;
                          if (requiresAttachmentDisclosure &&
                              evidenceFile == null) {
                            setDialogState(() =>
                                activity = 'Decrypting selected attachment…');
                            final directory = await getTemporaryDirectory();
                            ownedEvidenceFile = File(
                              '${directory.path}/kaede-report-${attachment!.ref.id.value}-${DateTime.now().microsecondsSinceEpoch}.evidence',
                            );
                            evidenceFile = await downloadEncryptedFile(
                              repository: repository,
                              manifest: attachmentManifest,
                              destination: ownedEvidenceFile!,
                              historyMediaUrl: attachment.historyMediaUrl,
                            );
                          }
                          if (createdReportId == null) {
                            setDialogState(() => activity = 'Creating report…');
                            final created = await repository.reportMessage(
                              message.ref,
                              category: category,
                              focusedAttachment: attachment?.ref,
                              description: description.text,
                              disclosedContent:
                                  message.e2ee == null ? null : message.content,
                              disclosureAcknowledged: disclose,
                            );
                            createdReportId = '${created['id']}';
                          }
                          if (requiresAttachmentDisclosure) {
                            final filename =
                                '${attachmentManifest['filename'] ?? attachmentLabel}';
                            final contentType =
                                '${attachmentManifest['content_type'] ?? 'application/octet-stream'}';
                            evidenceTicket ??= await repository
                                .createReportAttachmentEvidenceTicket(
                              createdReportId!,
                              filename: filename,
                              contentType: contentType,
                              size: await evidenceFile!.length(),
                            );
                            if (!evidenceUploaded) {
                              setDialogState(() {
                                activity = 'Uploading decrypted evidence…';
                                uploadProgress = 0;
                              });
                              await repository.uploadReportAttachmentEvidence(
                                evidenceTicket!,
                                evidenceFile!,
                                contentType: contentType,
                                onProgress: (sent, total) {
                                  if (!dialogContext.mounted || total <= 0) {
                                    return;
                                  }
                                  setDialogState(
                                      () => uploadProgress = sent / total);
                                },
                              );
                              evidenceUploaded = true;
                            }
                            setDialogState(
                                () => activity = 'Finalizing evidence…');
                            await repository.commitReportAttachmentEvidence(
                              createdReportId!,
                              attachmentId: '${evidenceTicket!['id']}',
                            );
                          }
                          if (dialogContext.mounted) {
                            Navigator.pop(dialogContext);
                          }
                          if (mounted) {
                            ScaffoldMessenger.of(this.context).showSnackBar(
                              SnackBar(content: Text('Report submitted.')),
                            );
                          }
                        } on Object catch (error) {
                          if (dialogContext.mounted) {
                            setDialogState(() {
                              submitting = false;
                              activity = '';
                            });
                            ScaffoldMessenger.of(dialogContext).showSnackBar(
                              SnackBar(
                                content: Text(userFacingError(
                                  error,
                                  summary: createdReportId == null
                                      ? 'Could not submit the report'
                                      : 'The report was submitted, but its decrypted attachment evidence could not be added',
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
      final evidence = ownedEvidenceFile;
      if (evidence != null && await evidence.exists()) {
        try {
          await evidence.delete();
        } on FileSystemException {
          // The operating system will remove the temporary file later.
        }
      }
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
        padding: EdgeInsets.fromLTRB(14, 20, 14, 8),
        child: Row(
          children: [
            Expanded(child: Divider(color: context.kaede.border)),
            Padding(
              padding: EdgeInsets.symmetric(horizontal: 10),
              child: Text(
                transcriptDayLabel(day).toUpperCase(),
                style: TextStyle(
                  color: context.kaede.muted,
                  fontSize: 11,
                  fontWeight: FontWeight.w700,
                  letterSpacing: .7,
                ),
              ),
            ),
            Expanded(child: Divider(color: context.kaede.border)),
          ],
        ),
      );
}

/// Compact reminder of which message an action sheet applies to.
final class _MessageActionsHeader extends StatelessWidget {
  const _MessageActionsHeader({required this.message, this.attachment});

  final KaedeMessage message;
  final KaedeAttachment? attachment;

  @override
  Widget build(BuildContext context) {
    final author = message.author;
    final content = message.content?.trim();
    final preview = attachment?.filename ??
        (content?.isNotEmpty == true
            ? spoilerSafeReplyPreview(content!)
            : message.attachments.isNotEmpty
                ? '${message.attachments.length} attachment'
                    '${message.attachments.length == 1 ? '' : 's'}'
                : 'Message');
    return Padding(
      padding: EdgeInsets.fromLTRB(20, 0, 20, 10),
      child: Row(
        children: [
          if (author != null) ...[
            UserAvatar(
                user: author, radius: 15, ringColor: context.kaede.panel),
            SizedBox(width: 10),
          ],
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  author?.name ?? 'Unknown author',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontWeight: FontWeight.w700,
                    fontSize: 14,
                  ),
                ),
                Text(
                  preview,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    color: context.kaede.muted,
                    fontSize: 12.5,
                  ),
                ),
              ],
            ),
          ),
          Text(
            DateFormat.jm().format(message.createdAt.toLocal()),
            style: TextStyle(color: context.kaede.muted, fontSize: 11.5),
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
          duration: Duration(milliseconds: 160),
          child: AnimatedSlide(
            offset: Offset(0, visible ? 0 : .35),
            duration: Duration(milliseconds: 180),
            curve: Curves.easeOutCubic,
            child: Material(
              color: context.kaede.raised,
              borderRadius: BorderRadius.circular(KaedeRadius.pill),
              elevation: 4,
              shadowColor: Colors.black45,
              child: InkWell(
                onTap: onTap,
                borderRadius: BorderRadius.circular(KaedeRadius.pill),
                child: Container(
                  padding: EdgeInsets.fromLTRB(12, 8, 14, 8),
                  decoration: BoxDecoration(
                    borderRadius: BorderRadius.circular(KaedeRadius.pill),
                    border: Border.all(color: context.kaede.border),
                  ),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Icon(Icons.arrow_downward_rounded,
                          size: 16, color: context.kaede.coralText),
                      SizedBox(width: 7),
                      Text(
                        unread > 0
                            ? '$unread new message${unread == 1 ? '' : 's'}'
                            : 'Jump to present',
                        style: TextStyle(
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
        padding: EdgeInsets.fromLTRB(14, 10, 14, 11),
        decoration: BoxDecoration(
          color: context.kaede.warningSoft,
          border: Border(bottom: BorderSide(color: context.kaede.border)),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: EdgeInsets.only(top: 1, right: 10),
              child: Icon(Icons.info_outline_rounded,
                  size: 17, color: context.kaede.warning),
            ),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    title,
                    style: TextStyle(
                      color: context.kaede.warning,
                      fontWeight: FontWeight.w700,
                      fontSize: 13,
                    ),
                  ),
                  SizedBox(height: 2),
                  Text(
                    message,
                    style: TextStyle(
                      color: context.kaede.textSoft,
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
        padding: EdgeInsets.fromLTRB(24, 20, 24, 12),
        child: Column(
          children: [
            Icon(
              complete ? Icons.flag_outlined : Icons.cloud_download_outlined,
              size: 20,
              color: context.kaede.muted,
            ),
            SizedBox(height: 8),
            Text(
              complete
                  ? 'Beginning of conversation'
                  : 'Recent history starts here',
              textAlign: TextAlign.center,
              style: TextStyle(
                fontWeight: FontWeight.w700,
                fontSize: 13.5,
              ),
            ),
            SizedBox(height: 4),
            Text(
              complete
                  ? 'You have reached the oldest message available from the '
                      'conversation home.'
                  : 'This instance keeps a rolling cache of this remote '
                      'conversation. Older messages load on demand from its '
                      'home instance; retry if that instance is temporarily '
                      'unavailable.',
              textAlign: TextAlign.center,
              style: TextStyle(
                color: context.kaede.muted,
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
      padding: EdgeInsets.fromLTRB(_messageGutter, 4, 12, 4),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (content.isNotEmpty)
            Opacity(
              opacity: failed ? .85 : .55,
              child: Text(content, style: TextStyle(fontSize: 15.5)),
            ),
          Padding(
            padding: EdgeInsets.only(top: 2),
            child: Row(
              children: [
                if (failed)
                  Icon(Icons.error_outline_rounded,
                      size: 14, color: context.kaede.danger)
                else
                  SizedBox.square(
                    dimension: 11,
                    child: CircularProgressIndicator(
                      strokeWidth: 1.6,
                      color: context.kaede.muted,
                    ),
                  ),
                SizedBox(width: 7),
                Expanded(
                  child: Text(
                    failed
                        ? userFacingError(
                            item.lastError ?? 'Message could not be sent.',
                          )
                        : 'Sending…',
                    style: TextStyle(
                      color:
                          failed ? context.kaede.danger : context.kaede.muted,
                      fontSize: 12,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
                if (failed) ...[
                  TextButton(
                    onPressed: onRetry,
                    style: TextButton.styleFrom(
                      minimumSize: Size(0, 32),
                      padding: EdgeInsets.symmetric(horizontal: 10),
                    ),
                    child: Text('Retry'),
                  ),
                  IconButton(
                    onPressed: onDiscard,
                    tooltip: 'Discard message',
                    visualDensity: VisualDensity.compact,
                    icon: Icon(Icons.close_rounded, size: 17),
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
        decoration: BoxDecoration(
          color: context.kaede.dangerSoft,
          border: Border(bottom: BorderSide(color: context.kaede.border)),
        ),
        child: Padding(
          padding: EdgeInsets.fromLTRB(14, 6, 6, 6),
          child: Row(
            children: [
              Icon(Icons.error_outline_rounded,
                  size: 17, color: context.kaede.danger),
              SizedBox(width: 10),
              Expanded(
                child: Text(
                  message,
                  maxLines: 3,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    color: context.kaede.danger,
                    fontSize: 12.5,
                    fontWeight: FontWeight.w600,
                    height: 1.3,
                  ),
                ),
              ),
              TextButton(
                onPressed: onRetry,
                style: TextButton.styleFrom(
                  minimumSize: Size(0, 34),
                  padding: EdgeInsets.symmetric(horizontal: 12),
                ),
                child: Text('Retry'),
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
    required this.canManage,
    required this.onClear,
  });

  final KaedeMessage message;
  final KaedeRepository repository;
  final bool canManage;
  final Future<void> Function(String? emoji)? onClear;

  @override
  State<_ReactionViewerSheet> createState() => _ReactionViewerSheetState();
}

final class _ReactionViewerSheetState extends State<_ReactionViewerSheet> {
  late String selectedEmoji;
  late Map<String, int> reactionCounts;
  final Map<String, List<KaedeUser>> users = <String, List<KaedeUser>>{};
  final Map<String, EntityRef?> nextAfter = <String, EntityRef?>{};
  final Map<String, String> errors = <String, String>{};
  final Set<String> loading = <String>{};
  bool clearing = false;
  String? managementError;

  List<MapEntry<String, int>> get reactions => reactionCounts.entries
      .where((entry) => entry.value > 0)
      .toList(growable: false);

  @override
  void initState() {
    super.initState();
    reactionCounts = Map<String, int>.of(
      canonicalReactionCounts(widget.message.reactionCounts),
    );
    selectedEmoji = reactions.firstOrNull?.key ?? '';
    if (selectedEmoji.isNotEmpty) _load(selectedEmoji);
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
    if (emoji == selectedEmoji || clearing) return;
    setState(() => selectedEmoji = emoji);
    if (!users.containsKey(emoji) && !loading.contains(emoji)) _load(emoji);
  }

  Future<void> _requestClear({String? emoji}) async {
    final onClear = widget.onClear;
    if (!widget.canManage || onClear == null || clearing) return;
    final count = emoji == null
        ? reactionCounts.values.fold<int>(0, (total, item) => total + item)
        : reactionCounts[emoji] ?? 0;
    if (count <= 0) return;
    final emojiLabel = emoji == null ? null : parseReactionEmoji(emoji).label;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(
          emoji == null
              ? 'Clear every reaction?'
              : 'Clear all $emojiLabel reactions?',
        ),
        content: Text(
          emoji == null
              ? 'All $count reactions will be removed from this message. This cannot be undone.'
              : '$count ${count == 1 ? 'reaction' : 'reactions'} will be removed from this message. This cannot be undone.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: Text('Cancel'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: context.kaede.danger,
            ),
            onPressed: () => Navigator.pop(dialogContext, true),
            child: Text(
              emoji == null ? 'Clear all reactions' : 'Clear $emojiLabel',
            ),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    setState(() {
      clearing = true;
      managementError = null;
    });
    try {
      await onClear(emoji);
      if (!mounted) return;
      final reconciled = reconcileClearedReactions(
        reactionCounts,
        const <String>{},
        emoji: emoji,
      );
      if (emoji == null) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('All reactions cleared.')),
        );
        Navigator.pop(context);
        return;
      }
      setState(() {
        reactionCounts = reconciled.counts;
        users.remove(emoji);
        nextAfter.remove(emoji);
        errors.remove(emoji);
        selectedEmoji = reactions.firstOrNull?.key ?? '';
      });
      if (selectedEmoji.isEmpty) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Reaction group cleared.')),
        );
        Navigator.pop(context);
        return;
      }
      if (!users.containsKey(selectedEmoji)) _load(selectedEmoji);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('$emojiLabel reactions cleared.')),
      );
    } on Object catch (error) {
      if (!mounted) return;
      setState(() {
        managementError = userFacingError(
          error,
          summary: emoji == null
              ? 'Could not clear reactions from this message'
              : 'Could not clear the $emojiLabel reactions',
        );
      });
    } finally {
      if (mounted) setState(() => clearing = false);
    }
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
          padding: EdgeInsets.fromLTRB(16, 0, 16, 12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      'Reactions',
                      style:
                          TextStyle(fontSize: 20, fontWeight: FontWeight.w800),
                    ),
                  ),
                  IconButton(
                    tooltip: 'Close reactions',
                    onPressed: () => Navigator.pop(context),
                    icon: Icon(Icons.close_rounded),
                  ),
                ],
              ),
              SizedBox(
                height: 44,
                child: ListView.separated(
                  scrollDirection: Axis.horizontal,
                  itemCount: reactions.length,
                  separatorBuilder: (_, __) => SizedBox(width: 6),
                  itemBuilder: (context, index) {
                    final reaction = reactions[index];
                    return ChoiceChip(
                      selected: selectedEmoji == reaction.key,
                      label: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          ReactionEmojiGlyph(
                            emoji: reaction.key,
                            size: 20,
                          ),
                          SizedBox(width: 6),
                          Text('${reaction.value}'),
                        ],
                      ),
                      onSelected: (_) => _select(reaction.key),
                    );
                  },
                ),
              ),
              if (widget.canManage && widget.onClear != null) ...[
                SizedBox(height: 6),
                Wrap(
                  alignment: WrapAlignment.end,
                  spacing: 8,
                  runSpacing: 6,
                  children: [
                    OutlinedButton.icon(
                      onPressed: clearing || selectedEmoji.isEmpty
                          ? null
                          : () => _requestClear(emoji: selectedEmoji),
                      icon: Icon(Icons.remove_circle_outline_rounded),
                      label: Text(
                        'Clear ${tryParseReactionEmoji(selectedEmoji)?.label ?? 'reaction'}',
                      ),
                    ),
                    TextButton.icon(
                      style: TextButton.styleFrom(
                        foregroundColor: context.kaede.danger,
                      ),
                      onPressed: clearing ? null : _requestClear,
                      icon: Icon(Icons.delete_sweep_outlined),
                      label: Text('Clear all'),
                    ),
                  ],
                ),
              ],
              if (managementError case final error?)
                Padding(
                  padding: EdgeInsets.only(top: 6),
                  child: Text(
                    error,
                    style: TextStyle(color: context.kaede.danger),
                    textAlign: TextAlign.right,
                  ),
                ),
              Divider(height: 20),
              Expanded(
                child: error != null && selectedUsers.isEmpty
                    ? Center(
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Text(error, textAlign: TextAlign.center),
                            SizedBox(height: 8),
                            TextButton(
                              onPressed: () => _load(selectedEmoji),
                              child: Text('Try again'),
                            ),
                          ],
                        ),
                      )
                    : selectedUsers.isEmpty && isLoading
                        ? Center(child: CircularProgressIndicator())
                        : selectedUsers.isEmpty
                            ? Center(
                                child: Text('No reactions to show.'),
                              )
                            : ListView.builder(
                                itemCount: selectedUsers.length +
                                    (nextAfter[selectedEmoji] == null ? 0 : 1),
                                itemBuilder: (context, index) {
                                  if (index == selectedUsers.length) {
                                    return Padding(
                                      padding: EdgeInsets.only(top: 8),
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
                                    contentPadding:
                                        EdgeInsets.symmetric(horizontal: 4),
                                    leading: UserAvatar(user: user, radius: 19),
                                    title: Text(user.name),
                                    subtitle: user.profileResolved
                                        ? Text(user.handle)
                                        : Text(
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
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) => Material(
        color: mine ? context.kaede.coralSoft : context.kaede.raised,
        borderRadius: BorderRadius.circular(KaedeRadius.small),
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(KaedeRadius.small),
          child: Container(
            padding: EdgeInsets.symmetric(horizontal: 8, vertical: 4),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(KaedeRadius.small),
              border: Border.all(
                color: mine ? context.kaede.coral : context.kaede.border,
              ),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                ReactionEmojiGlyph(emoji: emoji, size: 16),
                SizedBox(width: 5),
                Text(
                  '$count',
                  style: TextStyle(
                    fontSize: 12.5,
                    fontWeight: FontWeight.w700,
                    color:
                        mine ? context.kaede.coralText : context.kaede.textSoft,
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
          color: context.kaede.raised,
          borderRadius: BorderRadius.circular(KaedeRadius.small),
          child: InkWell(
            onTap: onTap,
            borderRadius: BorderRadius.circular(KaedeRadius.small),
            child: Container(
              padding: EdgeInsets.symmetric(horizontal: 8, vertical: 4),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(KaedeRadius.small),
                border: Border.all(color: context.kaede.border),
              ),
              child: Icon(Icons.add_reaction_outlined,
                  size: 15, color: context.kaede.muted),
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
    duration: Duration(milliseconds: 1100),
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
              if (index > 0) SizedBox(width: 3),
              Opacity(
                opacity: .35 +
                    .65 *
                        (1 -
                            ((_controller.value * 3 - index) % 3)
                                .clamp(0.0, 1.0)),
                child: Container(
                  width: 5,
                  height: 5,
                  decoration: BoxDecoration(
                    color: context.kaede.muted,
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
        padding: EdgeInsets.fromLTRB(14, 8, 6, 8),
        decoration: BoxDecoration(
          border: Border(bottom: BorderSide(color: context.kaede.border)),
        ),
        child: Row(
          children: [
            Icon(Icons.reply_rounded, size: 15, color: context.kaede.muted),
            SizedBox(width: 8),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text.rich(
                    TextSpan(
                      text: 'Replying to ',
                      style: TextStyle(
                        color: context.kaede.muted,
                        fontSize: 12.5,
                      ),
                      children: [
                        TextSpan(
                          text: author,
                          style: TextStyle(
                            color: context.kaede.text,
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
                      style: TextStyle(
                        color: context.kaede.muted,
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
                  minimumSize: Size(0, 34),
                  visualDensity: VisualDensity.compact,
                  padding: EdgeInsets.symmetric(horizontal: 8),
                  foregroundColor:
                      notify ? context.kaede.coralText : context.kaede.muted,
                  textStyle: TextStyle(
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
              icon: Icon(Icons.close_rounded, size: 18),
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
        color: context.kaede.panel,
        borderRadius: BorderRadius.circular(KaedeRadius.medium),
        border: Border.all(color: context.kaede.border),
      ),
      child: Stack(
        fit: StackFit.expand,
        children: [
          if (isImage)
            Image.file(
              item.file,
              fit: BoxFit.cover,
              errorBuilder: (_, __, ___) => ColoredBox(
                color: context.kaede.raised,
                child: Icon(Icons.image_outlined,
                    size: 20, color: context.kaede.muted),
              ),
            )
          else
            Padding(
              padding: EdgeInsets.fromLTRB(10, 8, 30, 8),
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
                        color: context.kaede.muted,
                      ),
                      SizedBox(width: 6),
                      Expanded(
                        child: Text(
                          item.name,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            fontSize: 12,
                            fontWeight: FontWeight.w600,
                            height: 1.25,
                          ),
                        ),
                      ),
                    ],
                  ),
                  SizedBox(height: 3),
                  Text(
                    formatAttachmentSize(item.size),
                    style: TextStyle(
                      fontSize: 11,
                      color: context.kaede.muted,
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
              shape: CircleBorder(),
              child: InkWell(
                onTap: onRemove,
                customBorder: CircleBorder(),
                child: Padding(
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

final class _ThreadCreatedRow extends StatelessWidget {
  const _ThreadCreatedRow({required this.message, this.onOpen});

  final KaedeMessage message;
  final VoidCallback? onOpen;

  @override
  Widget build(BuildContext context) {
    final thread = message.thread;
    final name = thread?.name?.trim().isNotEmpty == true
        ? thread!.name!.trim()
        : message.content?.trim().isNotEmpty == true
            ? message.content!.trim()
            : 'Thread unavailable';
    return InkWell(
      onTap: onOpen,
      child: Padding(
        padding: EdgeInsets.fromLTRB(20, 7, 12, 7),
        child: Row(
          children: [
            SizedBox(
              width: 30,
              child: Icon(Icons.forum_outlined,
                  size: 18, color: context.kaede.muted),
            ),
            SizedBox(width: 10),
            Expanded(
              child: Text.rich(
                TextSpan(
                  text:
                      '${message.author?.name ?? 'A member'} started a thread: ',
                  style: TextStyle(
                    color: context.kaede.muted,
                    fontSize: 13,
                  ),
                  children: [
                    TextSpan(
                      text: name,
                      style: TextStyle(
                        color: context.kaede.text,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ],
                ),
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
              ),
            ),
            if (onOpen != null)
              Icon(Icons.chevron_right_rounded,
                  size: 18, color: context.kaede.muted),
          ],
        ),
      ),
    );
  }
}

final class _MessageThreadPreview extends StatelessWidget {
  const _MessageThreadPreview({required this.thread, this.onTap});

  final KaedeChannel thread;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) => Padding(
        padding: EdgeInsets.only(top: 8),
        child: Material(
          color: context.kaede.panel,
          borderRadius: BorderRadius.circular(KaedeRadius.small),
          child: InkWell(
            onTap: onTap,
            borderRadius: BorderRadius.circular(KaedeRadius.small),
            child: Container(
              constraints: BoxConstraints(maxWidth: 380),
              padding: EdgeInsets.symmetric(horizontal: 11, vertical: 9),
              decoration: BoxDecoration(
                border: Border.all(color: context.kaede.border),
                borderRadius: BorderRadius.circular(KaedeRadius.small),
              ),
              child: Row(
                children: [
                  Icon(
                    thread.locked
                        ? Icons.lock_outline_rounded
                        : Icons.forum_outlined,
                    size: 17,
                    color: context.kaede.muted,
                  ),
                  SizedBox(width: 8),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          thread.name ?? 'Thread',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(fontWeight: FontWeight.w700),
                        ),
                        Text(
                          '${thread.messageCount} message${thread.messageCount == 1 ? '' : 's'}',
                          style: TextStyle(
                            color: context.kaede.muted,
                            fontSize: 12,
                          ),
                        ),
                      ],
                    ),
                  ),
                  if (onTap != null)
                    Icon(Icons.chevron_right_rounded,
                        size: 18, color: context.kaede.muted),
                ],
              ),
            ),
          ),
        ),
      );
}

/// Owns the two Discord-mobile message gestures without coupling them to row
/// presentation: long-press opens actions and double-tap uses quick reaction.
final class MessageGestureSurface extends StatelessWidget {
  const MessageGestureSurface({
    required this.child,
    this.onLongPress,
    this.onDoubleTapReaction,
    super.key,
  });

  final Widget child;
  final VoidCallback? onLongPress;
  final VoidCallback? onDoubleTapReaction;

  @override
  Widget build(BuildContext context) => InkWell(
        onLongPress: onLongPress,
        onDoubleTap: onDoubleTapReaction,
        child: child,
      );
}

final class _MessageTile extends StatelessWidget {
  const _MessageTile(
      {required this.message,
      required this.state,
      required this.compact,
      this.onQuickReaction,
      this.onMenu,
      this.onReaction,
      this.onComponent,
      this.onPollVote,
      this.onPollVoters,
      this.onAddReaction,
      this.onAuthorTap,
      this.onOpenThread,
      this.onAttachmentActions,
      this.referenced,
      this.onJump});
  final KaedeMessage message;
  final MobileState state;
  final KaedeMessage? referenced;
  final bool compact;
  final VoidCallback? onQuickReaction;
  final VoidCallback? onMenu;
  final ValueChanged<String>? onReaction;
  final Future<void> Function(RichComponent component, List<String> values)?
      onComponent;
  final Future<void> Function(int answerId, bool selected)? onPollVote;
  final VoidCallback? onPollVoters;
  final VoidCallback? onAddReaction;
  final VoidCallback? onAuthorTap;
  final VoidCallback? onOpenThread;
  final _OpenAttachmentActions? onAttachmentActions;
  final VoidCallback? onJump;

  void _openMenu() {
    if (onMenu == null) return;
    HapticFeedback.mediumImpact();
    onMenu!();
  }

  Future<void> _openAttachmentActions(
    KaedeAttachment attachment,
    Map<String, Object?>? manifest,
    File? decryptedFile,
  ) async {
    if (onAttachmentActions == null) return;
    await HapticFeedback.mediumImpact();
    await onAttachmentActions!(attachment, manifest, decryptedFile);
  }

  @override
  Widget build(BuildContext context) {
    if (message.messageType == 18) {
      return _ThreadCreatedRow(
        message: message,
        onOpen: onOpenThread,
      );
    }
    if ((message.messageType >= 3 && message.messageType <= 6) ||
        message.messageType == 12 ||
        const <int>{27, 28, 29, 31}.contains(message.messageType)) {
      return _SystemMessageRow(
        message: message,
        knownChannels: state.guilds.expand((guild) => guild.channels),
        onJump: onJump,
        onMenu: message.messageType == 12 && onMenu != null ? _openMenu : null,
      );
    }
    final displayedMessage = threadStarterDisplayMessage(message);
    final reactionCounts = canonicalReactionCounts(
      displayedMessage.reactionCounts,
    );
    final reactedEmoji = canonicalReactedEmoji(
      displayedMessage.reactedEmoji,
    );
    final author = displayedMessage.author;
    final guild = state.activeGuild;
    final authorMember = author == null
        ? null
        : state.activeGuildMembers
            .where((member) => member.user.ref == author.ref)
            .firstOrNull;
    final authorColor = guild == null || authorMember == null
        ? null
        : memberRoleColor(guild, authorMember);
    final authorIconRole = guild == null || authorMember == null
        ? null
        : highestIconRole(guild, authorMember);
    final deleted = displayedMessage.deletedAt != null;
    final encrypted = displayedMessage.e2ee != null;
    final richPresentationVerified =
        !encrypted || displayedMessage.e2eeVerified;
    final mediaPreview = automaticMessageMediaPreview(
      displayedMessage.content,
      encrypted: encrypted,
    );
    final linkPreview = automaticMessageLinkPreview(
      displayedMessage.content,
      encrypted: encrypted,
    );
    final failed = message.deliveryStatus == 'failed';
    final legacySticker = messageSticker(displayedMessage.content);
    final stickers = messageStickers(displayedMessage);
    final interactionAttribution = interactionAttributionText(
      displayedMessage.interactionMetadata,
      deleted: deleted,
    );
    final pollResult = resolvedMessagePollResult(displayedMessage, referenced);
    return MessageGestureSurface(
      onLongPress: onMenu == null ? null : _openMenu,
      onDoubleTapReaction: deleted ? null : onQuickReaction,
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
                      ? CircleAvatar(
                          radius: 20,
                          backgroundColor: context.kaede.raised,
                          foregroundColor: context.kaede.muted,
                          child: Text('?',
                              style: TextStyle(fontWeight: FontWeight.w700)),
                        )
                      : GestureDetector(
                          onTap: onAuthorTap,
                          child: UserAvatar(user: author, radius: 20),
                        ),
            ),
            SizedBox(width: 8),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (message.reference != null)
                    _ReplyReference(
                      referenced: referenced,
                      onTap: onJump,
                    ),
                  if (interactionAttribution != null)
                    Semantics(
                      label: interactionAttribution,
                      child: Padding(
                        padding: EdgeInsets.only(bottom: 3),
                        child: Row(
                          children: [
                            Icon(
                              Icons.subdirectory_arrow_right_rounded,
                              size: 13,
                              color: context.kaede.muted,
                            ),
                            SizedBox(width: 4),
                            Expanded(
                              child: Text(
                                interactionAttribution,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: TextStyle(
                                  color: context.kaede.muted,
                                  fontSize: 11,
                                  height: 1.2,
                                ),
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                  if (!compact)
                    Padding(
                      padding: EdgeInsets.only(bottom: 2),
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
                                style: TextStyle(
                                  color: authorColor,
                                  fontWeight: FontWeight.w700,
                                  fontSize: 15,
                                  height: 1.2,
                                  letterSpacing: -.1,
                                ),
                              ),
                            ),
                          ),
                          if (author?.isApplication == true) ...[
                            const SizedBox(width: 5),
                            const ApplicationTag(compact: true),
                          ],
                          if (authorIconRole?.iconHash
                              case final iconHash?) ...[
                            SizedBox(width: 5),
                            Baseline(
                              baseline: 14,
                              baselineType: TextBaseline.alphabetic,
                              child: Tooltip(
                                message: authorIconRole!.name,
                                child: CachedNetworkImage(
                                  imageUrl: publicAssetUri(
                                    authorIconRole.ref.domain,
                                    iconHash,
                                    variant: 'thumbnail_128',
                                  )!
                                      .toString(),
                                  width: 18,
                                  height: 18,
                                  fit: BoxFit.contain,
                                ),
                              ),
                            ),
                          ],
                          if (displayedMessage.createdAtAvailable) ...[
                            SizedBox(width: 8),
                            Text(
                              DateFormat.jm()
                                  .format(displayedMessage.createdAt.toLocal()),
                              style: TextStyle(
                                color: context.kaede.muted,
                                fontSize: 11.5,
                                fontWeight: FontWeight.w500,
                              ),
                            ),
                          ],
                          if (displayedMessage.editedAt != null) ...[
                            SizedBox(width: 6),
                            Text(
                              '(edited)',
                              style: TextStyle(
                                color: context.kaede.muted,
                                fontSize: 11,
                              ),
                            ),
                          ],
                          if (displayedMessage.pinned) ...[
                            SizedBox(width: 6),
                            Icon(Icons.push_pin_rounded,
                                size: 11, color: context.kaede.muted),
                          ],
                          if (isPublishedAnnouncement(displayedMessage)) ...[
                            SizedBox(width: 6),
                            Icon(
                              Icons.campaign_outlined,
                              size: 12,
                              color: context.kaede.muted,
                            ),
                            SizedBox(width: 3),
                            Text(
                              'Published',
                              style: TextStyle(
                                color: context.kaede.muted,
                                fontSize: 11,
                              ),
                            ),
                          ],
                        ],
                      ),
                    ),
                  if (deleted)
                    Text(
                      'Message deleted',
                      style: TextStyle(
                        color: context.kaede.muted,
                        fontStyle: FontStyle.italic,
                      ),
                    )
                  else if (displayedMessage.contentUnavailable)
                    Text(
                      'Message unavailable',
                      style: TextStyle(
                        color: context.kaede.muted,
                        fontStyle: FontStyle.italic,
                      ),
                    )
                  else if (encrypted && !displayedMessage.e2eeVerified)
                    const _UndecryptableNotice()
                  else if (displayedMessage.content case final content?
                      when content.isNotEmpty && legacySticker == null)
                    KaedeMessageMarkdown(
                      content: content,
                      state: state,
                      omitMediaUrl: mediaPreview,
                    ),
                  if (!deleted &&
                      richPresentationVerified &&
                      stickers.isNotEmpty)
                    Wrap(
                      spacing: 6,
                      runSpacing: 6,
                      children: [
                        for (final sticker in stickers)
                          _StickerMessage(
                            sticker: sticker,
                            size: stickers.length == 1 ? 240 : 150,
                          ),
                      ],
                    ),
                  if (!deleted &&
                      richPresentationVerified &&
                      (displayedMessage.forwardSnapshot != null ||
                          (displayedMessage.forwardedMessageRef != null &&
                              displayedMessage.flags & messageFlagIsCrosspost ==
                                  0)))
                    _ForwardedMessageCard(
                      message: displayedMessage,
                      state: state,
                    ),
                  if (!deleted && displayedMessage.messageType == 46)
                    if (pollResult == null)
                      const _PollResultUnavailable()
                    else
                      _PollResultCard(result: pollResult),
                  if (!deleted &&
                      richPresentationVerified &&
                      displayedMessage.messageType != 46)
                    for (final embed in displayedMessage.embeds)
                      _RichEmbedCard(
                        embed: embed,
                        message: displayedMessage,
                        state: state,
                        allowExternalMedia: !encrypted,
                      ),
                  if (displayedMessage.poll case final poll?
                      when !deleted && richPresentationVerified)
                    _MessagePollCard(
                      poll: poll,
                      onVote: onPollVote,
                      onViewVoters: onPollVoters,
                    ),
                  if (!deleted &&
                      richPresentationVerified &&
                      displayedMessage.components.isNotEmpty)
                    _RichMessageComponents(
                      rows: displayedMessage.components,
                      state: state,
                      attachments: displayedMessage.attachments,
                      viewVersion: displayedMessage.viewVersion,
                      allowExternalMedia: !encrypted,
                      onInvoke: onComponent,
                    ),
                  if (!deleted &&
                      displayedMessage.e2ee == null &&
                      mediaPreview == null &&
                      linkPreview != null)
                    _LinkPreviewCard(url: linkPreview),
                  if (!deleted && mediaPreview != null)
                    _RemoteMediaPreview(uri: mediaPreview),
                  for (final attachment in deleted
                      ? const <KaedeAttachment>[]
                      : richPresentationVerified
                          ? displayedMessage.attachments
                          : const <KaedeAttachment>[])
                    _AttachmentCard(
                      attachment: attachment,
                      pollStatus: canPollAttachmentStatus(
                        attachment: attachment.ref,
                        messageAuthor: displayedMessage.authorRef,
                        currentUser: state.user?.ref,
                      ),
                      encryptedManifest:
                          _encryptedManifestFor(displayedMessage, attachment),
                      onActions: onAttachmentActions == null
                          ? null
                          : _openAttachmentActions,
                    ),
                  if (!deleted)
                    if (message.thread case final thread?)
                      _MessageThreadPreview(
                        thread: thread,
                        onTap: onOpenThread,
                      ),
                  if (!deleted && reactionCounts.isNotEmpty)
                    Padding(
                      padding: EdgeInsets.only(top: 6, bottom: 2),
                      child: Wrap(
                        spacing: 5,
                        runSpacing: 5,
                        children: [
                          for (final reaction in reactionCounts.entries)
                            _ReactionChip(
                              emoji: reaction.key,
                              count: reaction.value,
                              mine: reactedEmoji.contains(reaction.key),
                              onTap: onReaction == null
                                  ? null
                                  : () => onReaction!(reaction.key),
                            ),
                          if (onAddReaction != null)
                            _AddReactionChip(onTap: onAddReaction!),
                        ],
                      ),
                    ),
                  if (failed || message.deliveryStatus == 'retrying')
                    Padding(
                      padding: EdgeInsets.only(top: 4),
                      child: Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Icon(
                            failed
                                ? Icons.error_outline_rounded
                                : Icons.sync_rounded,
                            size: 14,
                            color: failed
                                ? context.kaede.danger
                                : context.kaede.muted,
                          ),
                          SizedBox(width: 6),
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
                                    ? context.kaede.danger
                                    : context.kaede.muted,
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

List<({String value, String label})> _richEntityOptions(
  RichComponent component,
  MobileState state,
) {
  final users = <EntityRef, KaedeUser>{
    if (state.user case final currentUser?) currentUser.ref: currentUser,
    if (state.activeGuild != null)
      for (final member in state.activeGuildMembers)
        member.user.ref: state.userProfiles[member.user.ref] ?? member.user
    else
      for (final recipient
          in state.activeChannel?.recipients ?? const <KaedeUser>[])
        recipient.ref: state.userProfiles[recipient.ref] ?? recipient,
  };
  if (component.type == 5) {
    return users.values
        .map((user) => (value: user.ref.wire, label: user.name))
        .toList();
  }
  if (component.type == 6) {
    return (state.activeGuild?.roles ?? const <KaedeRole>[])
        .map((role) => (value: role.ref.wire, label: '@${role.name}'))
        .toList();
  }
  if (component.type == 7) {
    return <({String value, String label})>[
      ...users.values.map((user) => (value: user.ref.wire, label: user.name)),
      ...(state.activeGuild?.roles ?? const <KaedeRole>[])
          .map((role) => (value: role.ref.wire, label: '@${role.name}')),
    ];
  }
  return (state.activeGuild?.channels ?? const <KaedeChannel>[])
      .where((channel) =>
          component.channelTypes.isEmpty ||
          component.channelTypes.contains(switch (channel.type) {
            ChannelType.text => 0,
            ChannelType.dm => 1,
            ChannelType.groupDm => 3,
            ChannelType.voice => 2,
            ChannelType.stage => 13,
            ChannelType.category => 4,
            ChannelType.announcement => 5,
            ChannelType.announcementThread => 10,
            ChannelType.publicThread => 11,
            ChannelType.privateThread => 12,
            ChannelType.forum => 15,
            ChannelType.tracker => 17,
            ChannelType.unknown => -1,
          }))
      .map((channel) =>
          (value: channel.ref.wire, label: '#${channel.name ?? 'channel'}'))
      .toList();
}

final class _InteractionModalDialog extends StatefulWidget {
  const _InteractionModalDialog({
    required this.modal,
    required this.state,
    required this.encrypted,
    required this.onSubmit,
  });

  final InteractionModal modal;
  final MobileState state;
  final bool encrypted;
  final Future<void> Function(_ModalInteractionSubmission submission) onSubmit;

  @override
  State<_InteractionModalDialog> createState() =>
      _InteractionModalDialogState();
}

final class _InteractionModalDialogState
    extends State<_InteractionModalDialog> {
  final _formKey = GlobalKey<FormState>();
  late final Map<String, TextEditingController> _controllers;
  late final Map<String, bool> _checkboxes;
  late final Map<String, Set<String>> _selections;
  final Map<String, List<String>> _fileNames = <String, List<String>>{};
  final Map<String, Map<String, Object?>> _fileManifests =
      <String, Map<String, Object?>>{};
  final Set<String> _uploading = <String>{};
  var _submitting = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _controllers = <String, TextEditingController>{
      for (final row in widget.modal.rows)
        for (final component in row.components)
          if (component.isTextInput && component.customId != null)
            component.customId!: TextEditingController(text: component.value),
    };
    _checkboxes = <String, bool>{
      for (final row in widget.modal.rows)
        for (final component in row.components)
          if (component.isCheckboxV2 && component.customId != null)
            component.customId!: component.checked,
    };
    _selections = <String, Set<String>>{
      for (final row in widget.modal.rows)
        for (final component in row.components)
          if ((component.isStringSelect ||
                  component.isEntitySelect ||
                  component.isRadioGroup ||
                  component.isCheckboxGroup ||
                  component.isFileUpload) &&
              component.customId != null)
            component.customId!: component.isStringSelect ||
                    component.isRadioGroup ||
                    component.isCheckboxGroup
                ? component.options
                    .where((option) => option.isDefault)
                    .map((option) => option.value)
                    .toSet()
                : component.defaultValues.map((item) => item.ref.wire).toSet(),
    };
  }

  @override
  void dispose() {
    for (final controller in _controllers.values) {
      controller.dispose();
    }
    super.dispose();
  }

  Future<void> _submit() async {
    if (_submitting || !(_formKey.currentState?.validate() ?? false)) return;
    for (final row in widget.modal.rows) {
      for (final component in row.components) {
        if (!component.isStringSelect &&
            !component.isEntitySelect &&
            !component.isCheckboxGroup &&
            !component.isFileUpload) {
          if (component.isRadioGroup &&
              component.required &&
              (_selections[component.customId]?.isEmpty ?? true)) {
            setState(
                () => _error = '${row.label ?? 'This field'} is required.');
            return;
          }
          continue;
        }
        final count = _selections[component.customId]?.length ?? 0;
        if (count < component.minValues || count > component.maxValues) {
          setState(() {
            _error = 'Choose between ${component.minValues} and '
                '${component.maxValues} items for '
                '${component.placeholder ?? 'this field'}.';
          });
          return;
        }
      }
    }
    setState(() {
      _submitting = true;
      _error = null;
    });
    try {
      final components = <Map<String, Object?>>[
        for (final row in widget.modal.rows)
          if (row.type != 10)
            if (row.type == 18)
              <String, Object?>{
                'type': 18,
                if (row.id != null) 'id': row.id,
                if (row.components.isNotEmpty)
                  'component': _modalSubmission(row.components.first),
              }
            else
              <String, Object?>{
                'type': 1,
                'components': <Map<String, Object?>>[
                  for (final component in row.components)
                    if (component.customId != null) _modalSubmission(component),
                ],
              },
      ];
      final attachmentIds = <String>[
        for (final row in widget.modal.rows)
          for (final component in row.components)
            if (component.isFileUpload)
              ...(_selections[component.customId] ?? const <String>{}),
      ];
      await widget.onSubmit((
        components: components,
        attachmentIds: attachmentIds,
        manifests: <String, Map<String, Object?>>{
          for (final id in attachmentIds)
            if (_fileManifests[id] case final manifest?) id: manifest,
        },
      ));
      if (mounted) Navigator.pop(context);
    } on Object catch (error) {
      if (mounted) {
        setState(() {
          _error = userFacingError(
            error,
            summary: 'The bot did not receive this form',
          );
        });
      }
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  Map<String, Object?> _modalSubmission(RichComponent component) =>
      <String, Object?>{
        'type': component.type,
        'custom_id': component.customId,
        if (component.isStringSelect ||
            component.isEntitySelect ||
            component.isCheckboxGroup ||
            component.isFileUpload)
          'values': (_selections[component.customId] ?? <String>{})
              .toList(growable: false)
        else if (component.isRadioGroup)
          'value': (_selections[component.customId] ?? <String>{}).firstOrNull
        else
          'value': component.isCheckboxV2
              ? (_checkboxes[component.customId] ?? false)
              : (_controllers[component.customId]?.text ?? ''),
      };

  Future<void> _pickFiles(RichComponent component) async {
    final customId = component.customId;
    final channel = widget.state.activeChannel;
    if (customId == null || channel == null || _uploading.contains(customId)) {
      return;
    }
    final mobileController = ProviderScope.containerOf(context, listen: false)
        .read(mobileControllerProvider.notifier);
    final extensions = component.fileTypes
        .where((value) => value.startsWith('.'))
        .map((value) => value.substring(1))
        .toList(growable: false);
    final categories = component.fileTypes
        .where(const {'image', 'video', 'audio'}.contains)
        .toSet();
    final FileType pickerType;
    if (extensions.length == component.fileTypes.length &&
        extensions.isNotEmpty) {
      pickerType = FileType.custom;
    } else if (categories.length == 1 && categories.contains('image')) {
      pickerType = FileType.image;
    } else if (categories.length == 1 && categories.contains('video')) {
      pickerType = FileType.video;
    } else if (categories.length == 1 && categories.contains('audio')) {
      pickerType = FileType.audio;
    } else if (categories.length == 2 &&
        categories.containsAll(const {'image', 'video'})) {
      pickerType = FileType.media;
    } else {
      pickerType = FileType.any;
    }
    final picked = await FilePicker.platform.pickFiles(
      allowMultiple: component.maxValues > 1,
      withData: kIsWeb,
      type: pickerType,
      allowedExtensions: pickerType == FileType.custom ? extensions : null,
    );
    if (picked == null || !mounted) return;
    final files = picked.files.take(component.maxValues).toList();
    setState(() {
      _uploading.add(customId);
      _error = null;
    });
    try {
      final refs = <String>{};
      final manifests = <String, Map<String, Object?>>{};
      for (final selected in files) {
        EntityRef ref;
        if (widget.encrypted) {
          File? temporary;
          late final File source;
          if (selected.path != null) {
            source = File(selected.path!);
          } else {
            temporary = File(
              '${(await getTemporaryDirectory()).path}/kaede-modal-${DateTime.now().microsecondsSinceEpoch}-${_safeName(selected.name)}',
            );
            source = temporary;
          }
          if (selected.path == null) {
            await source.writeAsBytes(
              selected.bytes ?? const <int>[],
              flush: true,
            );
          }
          try {
            final encrypted = await uploadEncryptedFile(
              repository: mobileController.repository,
              channel: channel.ref,
              source: source,
              filename: selected.name,
              contentType: _contentType(selected.name),
            );
            ref = encrypted.attachment;
            manifests[ref.id.value] = encrypted.manifest;
          } finally {
            if (temporary != null && await temporary.exists()) {
              await temporary.delete();
            }
          }
        } else {
          ref = selected.path != null
              ? await mobileController.repository.uploadAttachmentFile(
                  channel: channel.ref,
                  filename: selected.name,
                  contentType: _contentType(selected.name),
                  file: File(selected.path!),
                )
              : await mobileController.repository.uploadAttachment(
                  channel: channel.ref,
                  filename: selected.name,
                  contentType: _contentType(selected.name),
                  bytes: selected.bytes ?? const <int>[],
                );
        }
        // The channel authority issued this ticket, so modal values carry the
        // Discord-style attachment ID rather than a replica-qualified ref.
        refs.add(ref.id.value);
      }
      if (mounted) {
        setState(() {
          _selections[customId] = refs;
          _fileNames[customId] = files.map((file) => file.name).toList();
          _fileManifests.addAll(manifests);
        });
      }
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = userFacingError(error,
            summary: 'One or more files could not be uploaded'));
      }
    } finally {
      if (mounted) setState(() => _uploading.remove(customId));
    }
  }

  List<({String value, String label})> _optionsFor(
    RichComponent component,
  ) =>
      component.isStringSelect ||
              component.isRadioGroup ||
              component.isCheckboxGroup
          ? component.options
              .map((option) => (
                    value: option.value,
                    label: <String>[
                      if (option.emoji != null) option.emoji!.label,
                      option.label,
                      if (option.description != null) option.description!,
                    ].join(' · '),
                  ))
              .toList(growable: false)
          : _richEntityOptions(component, widget.state);

  Future<void> _choose(RichComponent component) async {
    final customId = component.customId;
    if (_submitting || component.disabled || customId == null) return;
    final options = _optionsFor(component);
    if (options.isEmpty) return;
    final minimum =
        component.isRadioGroup && !component.required ? 0 : component.minValues;
    final allowed = options.map((option) => option.value).toSet();
    final selected = <String>{...?_selections[customId]}..retainAll(allowed);
    if (!mounted) return;
    final result = await showModalBottomSheet<Set<String>>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (context) => StatefulBuilder(
        builder: (context, setSheetState) => SafeArea(
          child: ConstrainedBox(
            constraints: BoxConstraints(
              maxHeight: min(MediaQuery.sizeOf(context).height * .72, 560),
            ),
            child: Column(
              children: [
                Padding(
                  padding: EdgeInsets.fromLTRB(18, 4, 10, 10),
                  child: Row(
                    children: [
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              component.placeholder ?? 'Choose an option',
                              style: TextStyle(
                                fontWeight: FontWeight.w800,
                                fontSize: 17,
                              ),
                            ),
                            Text(
                              'Choose $minimum–'
                              '${component.maxValues} · ${selected.length} selected',
                              style: TextStyle(
                                color: context.kaede.muted,
                                fontSize: 12,
                              ),
                            ),
                          ],
                        ),
                      ),
                      TextButton(
                        onPressed: () => Navigator.pop(context),
                        child: Text('Cancel'),
                      ),
                      FilledButton(
                        onPressed: selected.length < minimum ||
                                selected.length > component.maxValues
                            ? null
                            : () => Navigator.pop(context, selected),
                        child: Text('Done'),
                      ),
                    ],
                  ),
                ),
                Expanded(
                  child: ListView.builder(
                    itemCount: options.length,
                    itemBuilder: (context, index) {
                      final option = options[index];
                      final active = selected.contains(option.value);
                      return CheckboxListTile(
                        value: active,
                        title: Text(option.label),
                        onChanged: (checked) => setSheetState(() {
                          if (checked == true) {
                            if (component.maxValues == 1) selected.clear();
                            if (selected.length < component.maxValues) {
                              selected.add(option.value);
                            }
                          } else if (selected.length > minimum) {
                            selected.remove(option.value);
                          }
                        }),
                      );
                    },
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
    if (result != null && mounted) {
      setState(() {
        _selections[customId] = result;
        _error = null;
      });
    }
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: Text(widget.modal.title),
        content: SizedBox(
          width: min(MediaQuery.sizeOf(context).width - 64, 460),
          child: Form(
            key: _formKey,
            child: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  for (final row in widget.modal.rows)
                    if (row.type == 10)
                      Padding(
                        padding: EdgeInsets.only(bottom: 12),
                        child: KaedeMessageMarkdown(
                          content: row.content ?? '',
                          state: widget.state,
                        ),
                      )
                    else
                      for (final component in row.components)
                        if (component.isTextInput && component.customId != null)
                          Padding(
                            padding: EdgeInsets.only(bottom: 12),
                            child: TextFormField(
                              controller: _controllers[component.customId],
                              minLines: component.inputStyle == 2 ? 3 : 1,
                              maxLines: component.inputStyle == 2 ? 8 : 1,
                              maxLength: component.maxLength,
                              enabled: !_submitting,
                              decoration: InputDecoration(
                                labelText:
                                    row.label ?? component.label ?? 'Response',
                                helperText: row.description,
                                hintText: component.placeholder,
                              ),
                              validator: (value) {
                                final length = value?.length ?? 0;
                                if (component.required && length == 0) {
                                  return 'This field is required.';
                                }
                                if (component.minLength case final minimum?
                                    when length < minimum) {
                                  return 'Enter at least $minimum characters.';
                                }
                                return null;
                              },
                            ),
                          )
                        else if (component.isCheckboxV2 &&
                            component.customId != null)
                          CheckboxListTile(
                            contentPadding: EdgeInsets.zero,
                            value: _checkboxes[component.customId] ?? false,
                            title:
                                Text(row.label ?? component.label ?? 'Option'),
                            subtitle: row.description == null
                                ? null
                                : Text(row.description!),
                            onChanged: _submitting || component.disabled
                                ? null
                                : (value) => setState(() =>
                                    _checkboxes[component.customId!] =
                                        value ?? false),
                          )
                        else if ((component.isStringSelect ||
                                component.isEntitySelect ||
                                component.isRadioGroup ||
                                component.isCheckboxGroup) &&
                            component.customId != null)
                          Builder(builder: (context) {
                            final options = _optionsFor(component);
                            final selected =
                                _selections[component.customId] ?? <String>{};
                            final optionLabels = <String, String>{
                              for (final option in options)
                                option.value: option.label,
                            };
                            final summary = selected.isEmpty
                                ? (component.placeholder ?? 'Choose')
                                : selected
                                    .map(
                                        (value) => optionLabels[value] ?? value)
                                    .join(', ');
                            return Padding(
                              padding: EdgeInsets.only(bottom: 12),
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  if (row.label != null) ...[
                                    Text(row.label!,
                                        style: TextStyle(
                                            fontWeight: FontWeight.w700)),
                                    if (row.description != null)
                                      Text(row.description!,
                                          style: TextStyle(
                                              color: context.kaede.muted,
                                              fontSize: 12)),
                                    SizedBox(height: 5),
                                  ],
                                  OutlinedButton.icon(
                                    onPressed: _submitting ||
                                            component.disabled ||
                                            options.isEmpty
                                        ? null
                                        : () => _choose(component),
                                    icon: Icon(
                                      Icons.expand_more_rounded,
                                      size: 18,
                                    ),
                                    label: Align(
                                      alignment: Alignment.centerLeft,
                                      child: Text(
                                        summary,
                                        maxLines: 2,
                                        overflow: TextOverflow.ellipsis,
                                      ),
                                    ),
                                  ),
                                  Padding(
                                    padding: EdgeInsets.only(left: 12, top: 3),
                                    child: Text(
                                      options.isEmpty
                                          ? 'No matching items are available in this channel.'
                                          : 'Choose ${component.minValues}–${component.maxValues}',
                                      style: TextStyle(
                                        color: context.kaede.muted,
                                        fontSize: 12,
                                      ),
                                    ),
                                  ),
                                ],
                              ),
                            );
                          })
                        else if (component.isFileUpload &&
                            component.customId != null)
                          Padding(
                            padding: EdgeInsets.only(bottom: 12),
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(row.label ?? 'Upload files',
                                    style:
                                        TextStyle(fontWeight: FontWeight.w700)),
                                if (row.description != null)
                                  Text(row.description!,
                                      style: TextStyle(
                                          color: context.kaede.muted,
                                          fontSize: 12)),
                                SizedBox(height: 5),
                                OutlinedButton.icon(
                                  onPressed: _submitting ||
                                          _uploading
                                              .contains(component.customId)
                                      ? null
                                      : () => _pickFiles(component),
                                  icon: _uploading.contains(component.customId)
                                      ? SizedBox.square(
                                          dimension: 16,
                                          child: CircularProgressIndicator(
                                              strokeWidth: 2))
                                      : Icon(Icons.attach_file_rounded),
                                  label: Text(
                                    _fileNames[component.customId]
                                            ?.join(', ') ??
                                        'Choose ${component.minValues}–${component.maxValues} files',
                                  ),
                                ),
                              ],
                            ),
                          ),
                  if (_error case final error?)
                    Text(error,
                        style: TextStyle(
                            color: context.kaede.danger, fontSize: 12)),
                ],
              ),
            ),
          ),
        ),
        actions: [
          TextButton(
            onPressed: _submitting ? null : () => Navigator.pop(context),
            child: Text('Cancel'),
          ),
          FilledButton(
            onPressed: _submitting ? null : _submit,
            child: Text(_submitting ? 'Sending…' : 'Submit'),
          ),
        ],
      );
}

final class _EphemeralInteractionResponse extends StatefulWidget {
  const _EphemeralInteractionResponse({
    required this.data,
    required this.state,
    this.allowExternalMedia = true,
    this.onComponent,
    this.onPollVote,
    this.onPollVoters,
  });

  final Map<String, Object?> data;
  final MobileState state;
  final bool allowExternalMedia;
  final Future<void> Function(RichComponent component, List<String> values)?
      onComponent;
  final Future<void> Function(int answerId, bool selected)? onPollVote;
  final VoidCallback? onPollVoters;

  @override
  State<_EphemeralInteractionResponse> createState() =>
      _EphemeralInteractionResponseState();
}

final class _EphemeralInteractionResponseState
    extends State<_EphemeralInteractionResponse> {
  Timer? _expiryTimer;
  var _viewExpired = false;

  @override
  void initState() {
    super.initState();
    _scheduleExpiry();
  }

  @override
  void didUpdateWidget(covariant _EphemeralInteractionResponse oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.data['view_expires_at'] != widget.data['view_expires_at']) {
      _scheduleExpiry();
    }
  }

  void _scheduleExpiry() {
    _expiryTimer?.cancel();
    final expiry = DateTime.tryParse(
      widget.data['view_expires_at'] is String
          ? widget.data['view_expires_at']! as String
          : '',
    )?.toUtc();
    final remaining = expiry?.difference(DateTime.now().toUtc());
    _viewExpired =
        remaining != null && !remaining.isNegative ? false : expiry != null;
    if (remaining != null && !remaining.isNegative) {
      _expiryTimer = Timer(remaining, () {
        if (mounted) setState(() => _viewExpired = true);
      });
    }
  }

  @override
  void dispose() {
    _expiryTimer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final data = widget.data;
    final content = data['content'] is String ? data['content']! as String : '';
    final attachments = interactionResponseAttachments(data);
    final poll = interactionResponsePoll(data);
    final embeds = _richObjects(data['embeds'])
        .map(RichEmbed.fromJson)
        .toList(growable: false);
    final components = _richObjects(data['components'])
        .map(RichMessageLayout.fromJson)
        .toList(growable: false);
    final presentationMessage = KaedeMessage(
      ref: EntityRef(Snowflake('1'), Domain('ephemeral.invalid')),
      channelRef: EntityRef(Snowflake('1'), Domain('ephemeral.invalid')),
      authorRef: EntityRef(Snowflake('1'), Domain('ephemeral.invalid')),
      createdAt: DateTime.fromMillisecondsSinceEpoch(0, isUtc: true),
      attachments: attachments,
    );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text('Only you can see this',
            style: TextStyle(
                color: context.kaede.muted,
                fontSize: 11,
                fontWeight: FontWeight.w800)),
        if (content.isNotEmpty) ...[
          SizedBox(height: 8),
          KaedeMessageMarkdown(content: content, state: widget.state),
        ],
        for (final embed in embeds)
          _RichEmbedCard(
            embed: embed,
            message: presentationMessage,
            state: widget.state,
            allowExternalMedia: widget.allowExternalMedia,
          ),
        for (final attachment in attachments)
          _AttachmentCard(
            attachment: attachment,
            pollStatus: false,
            encryptedManifest:
                interactionResponseEncryptedManifest(data, attachment),
          ),
        if (poll != null)
          _MessagePollCard(
            poll: poll,
            onVote: widget.onPollVote,
            onViewVoters: widget.onPollVoters,
          ),
        if (components.isNotEmpty) ...[
          SizedBox(height: 10),
          _RichMessageComponents(
            rows: components,
            state: widget.state,
            attachments: attachments,
            viewVersion: switch (data['view_version']) {
              final int value => value,
              final num value => value.toInt(),
              final Object value => int.tryParse('$value'),
              null => null,
            },
            allowExternalMedia: widget.allowExternalMedia,
            onInvoke: _viewExpired ? null : widget.onComponent,
          ),
          if (_viewExpired)
            Padding(
              padding: EdgeInsets.only(top: 5),
              child: Text(
                'These private bot controls expired. Run the command again.',
                style: TextStyle(color: context.kaede.muted, fontSize: 11),
              ),
            ),
        ],
      ],
    );
  }
}

final class _PollResultUnavailable extends StatelessWidget {
  const _PollResultUnavailable();

  @override
  Widget build(BuildContext context) => Container(
        margin: EdgeInsets.only(top: 5),
        padding: EdgeInsets.all(10),
        decoration: BoxDecoration(
          color: context.kaede.raised,
          borderRadius: BorderRadius.circular(KaedeRadius.medium),
          border: Border.all(color: context.kaede.border),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.poll_outlined, size: 18, color: context.kaede.muted),
            SizedBox(width: 8),
            Flexible(
              child: Text(
                'Poll results are unavailable because they could not be verified.',
                style: TextStyle(color: context.kaede.muted),
              ),
            ),
          ],
        ),
      );
}

final class _PollResultCard extends StatelessWidget {
  const _PollResultCard({required this.result});

  final RichPollResultMessage result;

  @override
  Widget build(BuildContext context) {
    final emoji = result.victorAnswerEmoji?.label.trim();
    final winner = result.victorAnswerText?.trim().isNotEmpty == true
        ? result.victorAnswerText!.trim()
        : emoji?.isNotEmpty == true
            ? emoji!
            : result.victorAnswerId == null
                ? null
                : 'Answer ${result.victorAnswerId}';
    final summary = result.totalVotes == 0
        ? 'No votes were cast.'
        : result.victorAnswerId == null
            ? 'The poll ended in a tie · ${result.totalVotes} votes'
            : '$winner won with ${result.victorAnswerVotes} of '
                '${result.totalVotes} votes.';
    return Semantics(
      label: 'Poll results',
      child: Container(
        margin: EdgeInsets.only(top: 5),
        padding: EdgeInsets.all(10),
        decoration: BoxDecoration(
          color: context.kaede.raised,
          borderRadius: BorderRadius.circular(KaedeRadius.medium),
          border: Border(
            left: BorderSide(color: context.kaede.coral, width: 3),
            top: BorderSide(color: context.kaede.border),
            right: BorderSide(color: context.kaede.border),
            bottom: BorderSide(color: context.kaede.border),
          ),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(Icons.check_circle_outline_rounded,
                size: 18, color: context.kaede.coral),
            SizedBox(width: 8),
            Flexible(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    result.questionText?.trim().isNotEmpty == true
                        ? result.questionText!.trim()
                        : 'Poll ended',
                    style: TextStyle(fontWeight: FontWeight.w700),
                  ),
                  SizedBox(height: 2),
                  Text(summary, style: TextStyle(color: context.kaede.muted)),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

final class _RichEmbedCard extends StatelessWidget {
  const _RichEmbedCard({
    required this.embed,
    required this.state,
    this.message,
    this.attachments = const <KaedeAttachment>[],
    this.allowExternalMedia = true,
  });

  final RichEmbed embed;
  final KaedeMessage? message;
  final List<KaedeAttachment> attachments;
  final MobileState state;
  final bool allowExternalMedia;

  List<KaedeAttachment> get _attachments => message?.attachments ?? attachments;

  Future<void> _open(String? value) async {
    final uri = Uri.tryParse(value ?? '');
    if (uri != null && const {'http', 'https'}.contains(uri.scheme)) {
      await launchUrl(uri, mode: LaunchMode.externalApplication);
    }
  }

  KaedeAttachment? _attachment(String? url) {
    if (url == null || !url.startsWith('attachment://')) return null;
    final filename = url.substring('attachment://'.length);
    return _attachments
        .where((attachment) => attachment.filename == filename)
        .firstOrNull;
  }

  Widget? _compactMedia(String? value, {required BoxFit fit}) {
    final attachment = _attachment(value);
    if (attachment != null) {
      if (!attachment.contentType.startsWith('image/')) return null;
      return _AttachmentCard(
        attachment: attachment,
        compact: true,
        compactFit: fit,
      );
    }
    final external = richEmbedExternalMediaUri(value);
    if (external == null) return null;
    if (allowExternalMedia) {
      return _ProxiedEmbedImage(url: external.toString(), fit: fit);
    }
    return IconButton(
      tooltip: 'Open external image',
      onPressed: () => _open(external.toString()),
      icon: Icon(Icons.open_in_new_rounded, size: 16),
    );
  }

  @override
  Widget build(BuildContext context) {
    final accent = Color(0xff000000 | (embed.color ?? 0x6d7078));
    final imageAttachment = _attachment(embed.imageUrl);
    final externalImage = richEmbedExternalMediaUri(embed.imageUrl);
    final authorIcon = _compactMedia(embed.authorIconUrl, fit: BoxFit.cover);
    final footerIcon = _compactMedia(embed.footerIconUrl, fit: BoxFit.cover);
    final thumbnail = _compactMedia(embed.thumbnailUrl, fit: BoxFit.cover);
    return Container(
      margin: EdgeInsets.only(top: 8),
      constraints: BoxConstraints(maxWidth: 540),
      decoration: BoxDecoration(
        color: context.kaede.raised,
        borderRadius: BorderRadius.circular(8),
        border: Border(
          left: BorderSide(color: accent, width: 4),
          top: BorderSide(color: context.kaede.border),
          right: BorderSide(color: context.kaede.border),
          bottom: BorderSide(color: context.kaede.border),
        ),
      ),
      padding: EdgeInsets.all(12),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (embed.authorName case final author?) ...[
                  Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      if (authorIcon != null) ...[
                        ClipOval(
                          child: SizedBox.square(
                            dimension: 18,
                            child: authorIcon,
                          ),
                        ),
                        SizedBox(width: 6),
                      ],
                      Flexible(
                        child: GestureDetector(
                          onTap: embed.authorUrl == null
                              ? null
                              : () => _open(embed.authorUrl),
                          child: Text(
                            author,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              fontSize: 12,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        ),
                      ),
                    ],
                  ),
                  SizedBox(height: 6),
                ],
                if (embed.title case final title?) ...[
                  GestureDetector(
                    onTap: embed.url == null ? null : () => _open(embed.url),
                    child: Text(
                      title,
                      style: TextStyle(
                        color: embed.url == null
                            ? context.kaede.text
                            : context.kaede.coral,
                        fontWeight: FontWeight.w800,
                        fontSize: 15,
                      ),
                    ),
                  ),
                  SizedBox(height: 5),
                ],
                if (embed.description case final description?)
                  KaedeMessageMarkdown(content: description, state: state),
                if (embed.fields.isNotEmpty) ...[
                  SizedBox(height: 9),
                  LayoutBuilder(builder: (context, constraints) {
                    final inlineWidth = (constraints.maxWidth - 16) / 3;
                    return Wrap(
                      spacing: 8,
                      runSpacing: 9,
                      children: [
                        for (final field in embed.fields)
                          SizedBox(
                            width: field.inline
                                ? inlineWidth
                                : constraints.maxWidth,
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(field.name,
                                    style: TextStyle(
                                        fontSize: 12,
                                        fontWeight: FontWeight.w800)),
                                SizedBox(height: 2),
                                KaedeMessageMarkdown(
                                    content: field.value, state: state),
                              ],
                            ),
                          ),
                      ],
                    );
                  }),
                ],
                if (imageAttachment != null)
                  _AttachmentCard(attachment: imageAttachment)
                else if (externalImage != null && allowExternalMedia)
                  Padding(
                    padding: EdgeInsets.only(top: 8),
                    child: ClipRRect(
                      borderRadius: BorderRadius.circular(8),
                      child: AspectRatio(
                        aspectRatio: 16 / 9,
                        child: _ProxiedEmbedImage(
                          url: externalImage.toString(),
                          fit: BoxFit.contain,
                        ),
                      ),
                    ),
                  ),
                if (imageAttachment == null &&
                    externalImage != null &&
                    !allowExternalMedia)
                  Padding(
                    padding: EdgeInsets.only(top: 8),
                    child: OutlinedButton.icon(
                      onPressed: () => _open(externalImage.toString()),
                      icon: Icon(Icons.open_in_new_rounded, size: 16),
                      label: Text('Open external embed image'),
                    ),
                  ),
                if (embed.footerText != null || embed.timestamp != null) ...[
                  SizedBox(height: 9),
                  Row(
                    children: [
                      if (footerIcon != null) ...[
                        ClipRRect(
                          borderRadius: BorderRadius.circular(3),
                          child: SizedBox.square(
                            dimension: 18,
                            child: footerIcon,
                          ),
                        ),
                        SizedBox(width: 6),
                      ],
                      Flexible(
                        child: Text(
                          <String>[
                            if (embed.footerText case final footer?) footer,
                            if (embed.timestamp case final timestamp?)
                              DateFormat.yMMMd()
                                  .add_jm()
                                  .format(timestamp.toLocal()),
                          ].join(' · '),
                          style: TextStyle(
                            color: context.kaede.muted,
                            fontSize: 10.5,
                          ),
                        ),
                      ),
                    ],
                  ),
                ],
              ],
            ),
          ),
          if (thumbnail != null) ...[
            SizedBox(width: 9),
            ClipRRect(
              borderRadius: BorderRadius.circular(8),
              child: SizedBox.square(
                dimension: 86,
                child: thumbnail,
              ),
            ),
          ],
        ],
      ),
    );
  }
}

final class _PollAnswerEditor {
  _PollAnswerEditor() : text = TextEditingController();

  final TextEditingController text;
  RichEmoji? emoji;

  void dispose() => text.dispose();
}

final class _PollCreateDialog extends StatefulWidget {
  const _PollCreateDialog({
    required this.channel,
    required this.repository,
    required this.emojiCategories,
    required this.recentEmoji,
  });

  final KaedeChannel channel;
  final KaedeRepository repository;
  final Map<String, List<String>> emojiCategories;
  final List<String> recentEmoji;

  @override
  State<_PollCreateDialog> createState() => _PollCreateDialogState();
}

final class _PollCreateDialogState extends State<_PollCreateDialog> {
  static const _durations = <int>[1, 4, 8, 24, 72, 168];

  final _question = TextEditingController();
  final _answers = <_PollAnswerEditor>[
    _PollAnswerEditor(),
    _PollAnswerEditor(),
  ];
  var _durationHours = 24;
  var _allowMultiselect = false;
  String? _error;

  @override
  void dispose() {
    _question.dispose();
    for (final answer in _answers) {
      answer.dispose();
    }
    super.dispose();
  }

  Future<void> _chooseEmoji(_PollAnswerEditor answer) async {
    final value = await showComposerEmojiPicker(
      context,
      repository: widget.repository,
      channel: widget.channel,
      categories: widget.emojiCategories,
      recent: widget.recentEmoji,
    );
    if (!mounted || value == null) return;
    try {
      setState(() {
        answer.emoji = richPollEmojiFromComposerValue(value);
        _error = null;
      });
    } on ArgumentError catch (error) {
      setState(() => _error = '${error.message}');
    }
  }

  void _remove(_PollAnswerEditor answer) {
    if (_answers.length <= 2) return;
    setState(() => _answers.remove(answer));
    answer.dispose();
  }

  void _submit() {
    try {
      final draft = RichPollDraft(
        question: _question.text,
        answers: _answers
            .map((answer) => RichPollDraftAnswer(
                  text: answer.text.text,
                  emoji: answer.emoji,
                ))
            .toList(growable: false),
        durationHours: _durationHours,
        allowMultiselect: _allowMultiselect,
      );
      Navigator.pop(context, draft);
    } on ArgumentError catch (error) {
      setState(() => _error = '${error.message}');
    }
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: Text('Create a poll'),
        content: SizedBox(
          width: min(MediaQuery.sizeOf(context).width - 64, 500),
          child: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                TextField(
                  key: ValueKey('poll-question'),
                  controller: _question,
                  autofocus: true,
                  maxLength: 300,
                  minLines: 1,
                  maxLines: 4,
                  decoration: InputDecoration(labelText: 'Question'),
                ),
                SizedBox(height: 8),
                Text('Answers', style: TextStyle(fontWeight: FontWeight.w800)),
                SizedBox(height: 6),
                for (var index = 0; index < _answers.length; index++)
                  Padding(
                    padding: EdgeInsets.only(bottom: 8),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        IconButton(
                          key: ValueKey('poll-answer-emoji-$index'),
                          tooltip: _answers[index].emoji == null
                              ? 'Add emoji'
                              : 'Change emoji',
                          onPressed: () => _chooseEmoji(_answers[index]),
                          icon: _pollEmojiIcon(_answers[index].emoji),
                        ),
                        Expanded(
                          child: TextField(
                            key: ValueKey('poll-answer-$index'),
                            controller: _answers[index].text,
                            maxLength: 55,
                            decoration: InputDecoration(
                              labelText: 'Answer ${index + 1}',
                              counterText: '',
                            ),
                          ),
                        ),
                        if (_answers[index].emoji != null)
                          IconButton(
                            tooltip: 'Remove emoji',
                            onPressed: () =>
                                setState(() => _answers[index].emoji = null),
                            icon: Icon(Icons.emoji_emotions_outlined, size: 18),
                          ),
                        if (_answers.length > 2)
                          IconButton(
                            tooltip: 'Remove answer',
                            onPressed: () => _remove(_answers[index]),
                            icon: Icon(Icons.close_rounded),
                          ),
                      ],
                    ),
                  ),
                if (_answers.length < 10)
                  Align(
                    alignment: Alignment.centerLeft,
                    child: TextButton.icon(
                      key: ValueKey('poll-add-answer'),
                      onPressed: () =>
                          setState(() => _answers.add(_PollAnswerEditor())),
                      icon: Icon(Icons.add_rounded),
                      label: Text('Add answer'),
                    ),
                  ),
                SizedBox(height: 8),
                DropdownButtonFormField<int>(
                  key: ValueKey('poll-duration'),
                  initialValue: _durationHours,
                  decoration: InputDecoration(labelText: 'Duration'),
                  items: [
                    for (final hours in _durations)
                      DropdownMenuItem(
                        value: hours,
                        child: Text(_pollDurationLabel(hours)),
                      ),
                  ],
                  onChanged: (value) =>
                      setState(() => _durationHours = value ?? 24),
                ),
                SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  value: _allowMultiselect,
                  title: Text('Allow multiple answers'),
                  onChanged: (value) =>
                      setState(() => _allowMultiselect = value),
                ),
                if (_error case final error?)
                  Text(
                    error,
                    key: ValueKey('poll-create-error'),
                    style: TextStyle(
                      color: context.kaede.danger,
                      fontSize: 12,
                    ),
                  ),
              ],
            ),
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: Text('Cancel'),
          ),
          FilledButton(
            key: ValueKey('poll-submit'),
            onPressed: _submit,
            child: Text('Create poll'),
          ),
        ],
      );

  Widget _pollEmojiIcon(RichEmoji? emoji) {
    if (emoji?.ref case final ref?) {
      return CustomEmojiImage(ref: ref, label: emoji!.label, size: 22);
    }
    return Text(
      emoji?.name ?? '+',
      style: TextStyle(fontSize: 20, fontWeight: FontWeight.w700),
    );
  }

  String _pollDurationLabel(int hours) {
    if (hours < 24) return '$hours hour${hours == 1 ? '' : 's'}';
    if (hours % 24 == 0) {
      final days = hours ~/ 24;
      return '$days day${days == 1 ? '' : 's'}';
    }
    return '$hours hours';
  }
}

final class _PollVotersSheet extends StatefulWidget {
  const _PollVotersSheet({required this.poll, required this.load});

  final RichPoll poll;
  final Future<PollVoterPage> Function(int answerId, EntityRef? after) load;

  @override
  State<_PollVotersSheet> createState() => _PollVotersSheetState();
}

final class _PollVotersSheetState extends State<_PollVotersSheet> {
  final _users = <KaedeUser>[];
  late int _answerId;
  EntityRef? _nextAfter;
  var _loading = false;
  var _generation = 0;
  String? _error;

  RichPoll get _poll => widget.poll;

  @override
  void initState() {
    super.initState();
    _answerId = _poll.answers.first.id;
    unawaited(_load(reset: true));
  }

  Future<void> _select(int answerId) async {
    if (_answerId == answerId) return;
    setState(() => _answerId = answerId);
    await _load(reset: true);
  }

  Future<void> _load({required bool reset}) async {
    if (_loading && !reset) return;
    final generation = ++_generation;
    final after = reset ? null : _nextAfter;
    setState(() {
      _loading = true;
      _error = null;
      if (reset) {
        _users.clear();
        _nextAfter = null;
      }
    });
    try {
      final page = await widget.load(_answerId, after);
      if (!mounted || generation != _generation) return;
      setState(() {
        final seen = _users.map((user) => user.ref).toSet();
        _users.addAll(page.items.where((user) => seen.add(user.ref)));
        _nextAfter = page.nextAfter;
      });
    } on Object catch (error) {
      if (mounted && generation == _generation) {
        setState(() => _error = userFacingError(
              error,
              summary: 'Could not load poll voters',
            ));
      }
    } finally {
      if (mounted && generation == _generation) {
        setState(() => _loading = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) => SizedBox(
        height: min(MediaQuery.sizeOf(context).height * .78, 680),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            ListTile(
              title: Text('Poll voters',
                  style: TextStyle(fontWeight: FontWeight.w800)),
              subtitle: Text('Select an answer to see who voted for it.'),
            ),
            SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              padding: EdgeInsets.symmetric(horizontal: 12),
              child: Row(
                children: [
                  for (final answer in _poll.answers)
                    Padding(
                      padding: EdgeInsets.only(right: 8),
                      child: ChoiceChip(
                        key: ValueKey('poll-voters-answer-${answer.id}'),
                        selected: _answerId == answer.id,
                        label: Text(<String>[
                          if (answer.media.emoji case final emoji?) emoji.label,
                          answer.media.text ?? 'Answer ${answer.id}',
                          '(${_poll.resultFor(answer.id).count})',
                        ].join(' ')),
                        onSelected: (_) => _select(answer.id),
                      ),
                    ),
                ],
              ),
            ),
            SizedBox(height: 8),
            Divider(height: 1),
            Expanded(
              child: _users.isEmpty
                  ? Center(
                      child: Padding(
                        padding: EdgeInsets.all(24),
                        child: _loading
                            ? CircularProgressIndicator()
                            : Text(
                                _error ?? 'No one has voted for this answer.',
                                textAlign: TextAlign.center,
                                style: TextStyle(
                                  color: _error == null
                                      ? context.kaede.muted
                                      : context.kaede.danger,
                                ),
                              ),
                      ),
                    )
                  : ListView.builder(
                      itemCount: _users.length +
                          (_nextAfter != null || _loading || _error != null
                              ? 1
                              : 0),
                      itemBuilder: (context, index) {
                        if (index < _users.length) {
                          final user = _users[index];
                          return ListTile(
                            leading: UserAvatar(user: user, radius: 18),
                            title: Text(user.name),
                            subtitle: Text(user.handle),
                          );
                        }
                        if (_loading) {
                          return Padding(
                            padding: EdgeInsets.all(18),
                            child: Center(child: CircularProgressIndicator()),
                          );
                        }
                        return Padding(
                          padding: EdgeInsets.all(12),
                          child: OutlinedButton(
                            onPressed: () => _load(reset: false),
                            child: Text(_error == null ? 'Load more' : 'Retry'),
                          ),
                        );
                      },
                    ),
            ),
          ],
        ),
      );
}

final class _MessagePollCard extends StatefulWidget {
  const _MessagePollCard({
    required this.poll,
    this.onVote,
    this.onViewVoters,
  });

  final RichPoll poll;
  final Future<void> Function(int answerId, bool selected)? onVote;
  final VoidCallback? onViewVoters;

  @override
  State<_MessagePollCard> createState() => _MessagePollCardState();
}

final class _MessagePollCardState extends State<_MessagePollCard> {
  int? _busy;

  Future<void> _toggle(RichPollAnswer answer) async {
    if (_busy != null || widget.onVote == null || widget.poll.isClosed()) {
      return;
    }
    setState(() => _busy = answer.id);
    try {
      await widget.onVote!(
          answer.id, !widget.poll.resultFor(answer.id).meVoted);
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  @override
  Widget build(BuildContext context) {
    final poll = widget.poll;
    final closed = poll.isClosed();
    return Container(
      margin: EdgeInsets.only(top: 8),
      constraints: BoxConstraints(maxWidth: 520),
      padding: EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: context.kaede.raised,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: context.kaede.border),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(
            poll.question.text ?? 'Poll',
            style: TextStyle(fontWeight: FontWeight.w800),
          ),
          SizedBox(height: 9),
          for (final answer in poll.answers) ...[
            Builder(builder: (context) {
              final result = poll.resultFor(answer.id);
              final percent = poll.percentFor(answer.id);
              return Semantics(
                button: widget.onVote != null,
                selected: result.meVoted,
                label:
                    '${answer.media.text ?? answer.media.emoji?.label ?? 'Option'}, ${result.count} votes, $percent percent',
                child: InkWell(
                  onTap: closed || widget.onVote == null || _busy != null
                      ? null
                      : () => _toggle(answer),
                  borderRadius: BorderRadius.circular(8),
                  child: Container(
                    margin: EdgeInsets.only(bottom: 6),
                    padding: EdgeInsets.symmetric(horizontal: 10, vertical: 9),
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(8),
                      border: Border.all(
                        color: result.meVoted
                            ? context.kaede.coral
                            : context.kaede.border,
                      ),
                      gradient: LinearGradient(
                        colors: [
                          context.kaede.coral.withValues(alpha: .15),
                          context.kaede.coral.withValues(alpha: .15),
                          Colors.transparent,
                          Colors.transparent,
                        ],
                        stops: <double>[
                          0,
                          percent / 100,
                          percent / 100,
                          1,
                        ],
                      ),
                    ),
                    child: Row(children: [
                      if (answer.media.emoji case final emoji?) ...[
                        _RichEmojiIcon(emoji),
                        SizedBox(width: 6),
                      ],
                      Expanded(
                        child: Text(
                          answer.media.text ?? 'Option',
                          style: TextStyle(fontWeight: FontWeight.w700),
                        ),
                      ),
                      if (_busy == answer.id)
                        SizedBox.square(
                          dimension: 15,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      else
                        Text(
                          '${result.count} · $percent%',
                          style: TextStyle(
                            color: context.kaede.muted,
                            fontSize: 11,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                    ]),
                  ),
                ),
              );
            }),
          ],
          Text(
            <String>[
              '${poll.totalVotes} vote${poll.totalVotes == 1 ? '' : 's'}',
              poll.allowMultiselect ? 'Choose one or more' : 'Choose one',
              closed
                  ? 'Poll closed'
                  : poll.expiry == null
                      ? 'End time unavailable'
                      : 'Ends ${DateFormat.yMMMd().add_jm().format(poll.expiry!.toLocal())}',
            ].join(' · '),
            style: TextStyle(color: context.kaede.muted, fontSize: 10.5),
          ),
          if (widget.onViewVoters != null && poll.totalVotes > 0)
            Align(
              alignment: Alignment.centerLeft,
              child: TextButton.icon(
                key: ValueKey('poll-view-voters'),
                onPressed: widget.onViewVoters,
                icon: Icon(Icons.people_outline_rounded, size: 17),
                label: Text('View voters'),
              ),
            ),
        ],
      ),
    );
  }
}

final class _RichMessageComponents extends StatefulWidget {
  const _RichMessageComponents({
    required this.rows,
    required this.state,
    this.attachments = const <KaedeAttachment>[],
    this.viewVersion,
    this.allowExternalMedia = true,
    this.onInvoke,
  });

  final List<RichMessageLayout> rows;
  final MobileState state;
  final List<KaedeAttachment> attachments;
  final int? viewVersion;
  final bool allowExternalMedia;
  final Future<void> Function(RichComponent component, List<String> values)?
      onInvoke;

  @override
  State<_RichMessageComponents> createState() => _RichMessageComponentsState();
}

final class _RichMessageComponentsState extends State<_RichMessageComponents> {
  String? _busy;

  Future<void> _invoke(RichComponent component, List<String> values) async {
    if (_busy != null ||
        widget.onInvoke == null ||
        component.customId == null) {
      return;
    }
    setState(() => _busy = component.customId);
    try {
      await widget.onInvoke!(component, values);
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  List<({String value, String label})> _entityOptions(
    RichComponent component,
  ) =>
      _richEntityOptions(component, widget.state);

  Future<void> _choose(
    RichComponent component,
    List<({String value, String label})> options,
  ) async {
    final defaults = component.isStringSelect
        ? component.options
            .where((option) => option.isDefault)
            .map((option) => option.value)
            .toSet()
        : component.defaultValues.map((item) => item.ref.wire).toSet();
    final selected = <String>{...defaults};
    if (!mounted) return;
    final result = await showModalBottomSheet<List<String>>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (context) => StatefulBuilder(
        builder: (context, setSheetState) => SafeArea(
          child: ConstrainedBox(
            constraints: BoxConstraints(
              maxHeight: min(MediaQuery.sizeOf(context).height * .72, 560),
            ),
            child: Column(
              children: [
                Padding(
                  padding: EdgeInsets.fromLTRB(18, 4, 18, 10),
                  child: Row(children: [
                    Expanded(
                      child: Text(
                        component.placeholder ?? 'Choose an option',
                        style: TextStyle(
                          fontWeight: FontWeight.w800,
                          fontSize: 17,
                        ),
                      ),
                    ),
                    FilledButton(
                      onPressed: selected.length < component.minValues ||
                              selected.length > component.maxValues
                          ? null
                          : () => Navigator.pop(context, selected.toList()),
                      child: Text('Submit'),
                    ),
                  ]),
                ),
                Expanded(
                  child: ListView.builder(
                    itemCount: options.length,
                    itemBuilder: (context, index) {
                      final option = options[index];
                      final active = selected.contains(option.value);
                      return CheckboxListTile(
                        value: active,
                        title: Text(option.label),
                        onChanged: (checked) => setSheetState(() {
                          if (checked == true) {
                            if (component.maxValues == 1) selected.clear();
                            if (selected.length < component.maxValues) {
                              selected.add(option.value);
                            }
                          } else if (selected.length > component.minValues) {
                            selected.remove(option.value);
                          }
                        }),
                      );
                    },
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
    if (result != null) await _invoke(component, result);
  }

  Color _buttonColor(RichComponent component) => switch (component.style) {
        3 => context.kaede.mint,
        4 => context.kaede.danger,
        2 || 5 => context.kaede.raised,
        _ => context.kaede.coral,
      };

  @override
  Widget build(BuildContext context) => Padding(
        padding: EdgeInsets.only(top: 8),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            for (final layout in widget.rows)
              Padding(
                padding: EdgeInsets.only(bottom: 6),
                child: layout.actionRow == null
                    ? _RichV2Layout(
                        layout: layout,
                        state: widget.state,
                        attachments: widget.attachments,
                        viewVersion: widget.viewVersion,
                        allowExternalMedia: widget.allowExternalMedia,
                        onInvoke: widget.onInvoke,
                      )
                    : Wrap(
                        spacing: 6,
                        runSpacing: 6,
                        children: [
                          for (final component in layout.actionRow!.components)
                            if (component.isButton)
                              FilledButton.tonalIcon(
                                style: FilledButton.styleFrom(
                                  backgroundColor: _buttonColor(component),
                                  foregroundColor: readableForeground(
                                    _buttonColor(component),
                                  ),
                                ),
                                onPressed: component.disabled ||
                                        _busy != null ||
                                        component.style == 6 ||
                                        widget.onInvoke == null
                                    ? null
                                    : component.style == 5 &&
                                            component.url != null
                                        ? () async {
                                            final uri =
                                                Uri.tryParse(component.url!);
                                            if (uri != null &&
                                                const {'http', 'https'}
                                                    .contains(uri.scheme)) {
                                              await launchUrl(uri,
                                                  mode: LaunchMode
                                                      .externalApplication);
                                            }
                                          }
                                        : widget.onInvoke == null
                                            ? null
                                            : () =>
                                                _invoke(component, const []),
                                icon: component.emoji == null
                                    ? SizedBox.shrink()
                                    : _RichEmojiIcon(component.emoji!),
                                label: Text(component.label ??
                                    (component.style == 5
                                        ? 'Open link'
                                        : 'Button')),
                              )
                            else if (component.isStringSelect ||
                                component.isEntitySelect)
                              OutlinedButton.icon(
                                onPressed: component.disabled ||
                                        _busy != null ||
                                        widget.onInvoke == null
                                    ? null
                                    : () {
                                        final options = component.isStringSelect
                                            ? component.options
                                                .map((option) => (
                                                      value: option.value,
                                                      label: <String>[
                                                        if (option.emoji !=
                                                            null)
                                                          option.emoji!.label,
                                                        option.label,
                                                        if (option
                                                                .description !=
                                                            null)
                                                          option.description!,
                                                      ].join(' · '),
                                                    ))
                                                .toList()
                                            : _entityOptions(component);
                                        if (options.isNotEmpty) {
                                          _choose(component, options);
                                        }
                                      },
                                icon: Icon(Icons.expand_more_rounded, size: 18),
                                label: Text(component.placeholder ?? 'Choose'),
                              )
                        ],
                      ),
              ),
          ],
        ),
      );
}

final class _RichEmojiIcon extends StatelessWidget {
  const _RichEmojiIcon(this.emoji);

  final RichEmoji emoji;

  @override
  Widget build(BuildContext context) {
    final ref = emoji.ref;
    if (ref == null) return Text(emoji.name ?? '');
    return CachedNetworkImage(
      imageUrl: publicEmojiUri(ref).toString(),
      width: 18,
      height: 18,
      fit: BoxFit.contain,
      errorWidget: (_, __, ___) => Text(emoji.label),
    );
  }
}

Map<String, Object?> _richObject(Object? value) => value is Map
    ? value.map((key, child) => MapEntry('$key', child))
    : const <String, Object?>{};

List<Map<String, Object?>> _richObjects(Object? value) {
  if (value == null) return const <Map<String, Object?>>[];
  if (value is! List) return const <Map<String, Object?>>[];
  final result = <Map<String, Object?>>[];
  for (final item in value) {
    if (item is! Map || item.keys.any((key) => key is! String)) {
      return const <Map<String, Object?>>[];
    }
    result.add(Map<String, Object?>.from(item));
  }
  return List<Map<String, Object?>>.unmodifiable(result);
}

final class _RichV2Layout extends StatelessWidget {
  const _RichV2Layout({
    required this.layout,
    required this.state,
    required this.attachments,
    this.viewVersion,
    this.allowExternalMedia = true,
    this.onInvoke,
  });

  final RichMessageLayout layout;
  final MobileState state;
  final List<KaedeAttachment> attachments;
  final int? viewVersion;
  final bool allowExternalMedia;
  final Future<void> Function(RichComponent component, List<String> values)?
      onInvoke;

  Widget _child(RichMessageLayout child) => _RichV2Layout(
        layout: child,
        state: state,
        attachments: attachments,
        viewVersion: viewVersion,
        allowExternalMedia: allowExternalMedia,
        onInvoke: onInvoke,
      );

  @override
  Widget build(BuildContext context) {
    final raw = layout.raw;
    switch (layout.type) {
      case 1:
        return _RichMessageComponents(
          rows: <RichMessageLayout>[layout],
          state: state,
          attachments: attachments,
          viewVersion: viewVersion,
          allowExternalMedia: allowExternalMedia,
          onInvoke: onInvoke,
        );
      case 10:
        return KaedeMessageMarkdown(
          content: '${raw['content'] ?? ''}',
          state: state,
        );
      case 9:
        final accessory = _richObject(raw['accessory']);
        final accessoryType = switch (accessory['type']) {
          final num value => value.toInt(),
          final Object value => int.tryParse('$value') ?? 0,
          null => 0,
        };
        return Row(
          crossAxisAlignment: CrossAxisAlignment.center,
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  for (final item in _richObjects(raw['components']))
                    KaedeMessageMarkdown(
                      content: '${item['content'] ?? ''}',
                      state: state,
                    ),
                ],
              ),
            ),
            SizedBox(width: 10),
            SizedBox(
              width: 132,
              child: accessoryType == 2
                  ? _RichMessageComponents(
                      rows: <RichMessageLayout>[
                        RichMessageLayout.fromJson(<String, Object?>{
                          'type': 1,
                          'components': <Object?>[accessory],
                        }),
                      ],
                      state: state,
                      attachments: attachments,
                      viewVersion: viewVersion,
                      allowExternalMedia: allowExternalMedia,
                      onInvoke: onInvoke,
                    )
                  : _RichV2Media(
                      media: _richObject(accessory['media']),
                      description: accessory['description'] as String?,
                      spoiler: accessory['spoiler'] == true,
                      attachments: attachments,
                      allowExternalMedia: allowExternalMedia,
                    ),
            ),
          ],
        );
      case 12:
        final items = _richObjects(raw['items']);
        return Wrap(
          spacing: 4,
          runSpacing: 4,
          children: [
            for (final item in items)
              SizedBox(
                width: items.length == 1 ? 320 : 156,
                child: _RichV2Media(
                  media: _richObject(item['media']),
                  description: item['description'] as String?,
                  spoiler: item['spoiler'] == true,
                  attachments: attachments,
                  allowExternalMedia: allowExternalMedia,
                ),
              ),
          ],
        );
      case 13:
        final filename = '${_richObject(raw['file'])['url'] ?? ''}'
            .replaceFirst('attachment://', '');
        final attachment =
            attachments.where((item) => item.filename == filename).firstOrNull;
        return attachment == null
            ? Text(
                filename.isEmpty ? 'File unavailable' : '$filename unavailable',
                style: TextStyle(color: context.kaede.muted),
              )
            : _AttachmentCard(attachment: attachment, pollStatus: false);
      case 14:
        final large = raw['spacing'] == 2;
        return Padding(
          padding: EdgeInsets.symmetric(vertical: large ? 8 : 4),
          child:
              raw['divider'] == false ? SizedBox.shrink() : Divider(height: 1),
        );
      case 17:
        final children = layout.children;
        final accent = switch (raw['accent_color']) {
          final num value => Color(0xff000000 | value.toInt()),
          _ => context.kaede.border,
        };
        return Container(
          decoration: BoxDecoration(
            color: context.kaede.raised,
            borderRadius: BorderRadius.circular(8),
            border: Border(left: BorderSide(color: accent, width: 4)),
          ),
          padding: EdgeInsets.all(10),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              for (final (index, child) in children.indexed) ...[
                _child(child),
                if (index != children.length - 1) SizedBox(height: 8),
              ],
            ],
          ),
        );
      default:
        return SizedBox.shrink();
    }
  }
}

final class _RichV2Media extends StatefulWidget {
  const _RichV2Media({
    required this.media,
    required this.attachments,
    this.description,
    this.spoiler = false,
    this.allowExternalMedia = true,
  });

  final Map<String, Object?> media;
  final List<KaedeAttachment> attachments;
  final String? description;
  final bool spoiler;
  final bool allowExternalMedia;

  @override
  State<_RichV2Media> createState() => _RichV2MediaState();
}

final class _RichV2MediaState extends State<_RichV2Media> {
  var _revealed = false;

  @override
  Widget build(BuildContext context) {
    final url = '${widget.media['url'] ?? ''}';
    final filename = url.startsWith('attachment://')
        ? url.substring('attachment://'.length)
        : null;
    final attachment = filename == null
        ? null
        : widget.attachments
            .where((item) => item.filename == filename)
            .firstOrNull;
    final remote = richEmbedExternalMediaUri(url);
    final Widget content;
    if (attachment != null) {
      content = _AttachmentCard(attachment: attachment, pollStatus: false);
    } else if (remote != null && widget.allowExternalMedia) {
      content = AspectRatio(
        aspectRatio: 16 / 9,
        child: _ProxiedEmbedImage(
          url: remote.toString(),
          fit: BoxFit.cover,
        ),
      );
    } else if (remote != null) {
      content = SizedBox(
        height: 72,
        child: Center(
          child: TextButton.icon(
            onPressed: () => launchUrl(
              remote,
              mode: LaunchMode.externalApplication,
            ),
            icon: Icon(Icons.open_in_new_rounded, size: 16),
            label: Text('Open external component media'),
          ),
        ),
      );
    } else {
      content = SizedBox(
        height: 72,
        child: Center(
          child: Text(
            'Media unavailable',
            style: TextStyle(color: context.kaede.muted),
          ),
        ),
      );
    }
    return GestureDetector(
      onTap: widget.spoiler && !_revealed
          ? () => setState(() => _revealed = true)
          : null,
      child: Stack(
        alignment: Alignment.center,
        children: [
          content,
          if (widget.spoiler && !_revealed)
            Positioned.fill(
              child: ColoredBox(
                color: context.kaede.hover,
                child: Center(
                  child: Text('SPOILER',
                      style: TextStyle(fontWeight: FontWeight.w800)),
                ),
              ),
            ),
        ],
      ),
    );
  }
}

final class _ForwardedMessageCard extends ConsumerStatefulWidget {
  const _ForwardedMessageCard({required this.message, required this.state});

  final KaedeMessage message;
  final MobileState state;

  @override
  ConsumerState<_ForwardedMessageCard> createState() =>
      _ForwardedMessageCardState();
}

final class _ForwardedMessageCardState
    extends ConsumerState<_ForwardedMessageCard> {
  late Future<KaedeMessageSnapshot> _source;

  KaedeMessageSnapshot _materialFromMessage(KaedeMessage message) =>
      KaedeMessageSnapshot(
        content: message.content,
        embeds: message.embeds,
        components: message.components,
        attachments: message.attachments,
        stickerItems: message.stickerItems,
        mentionUserRefs: message.mentionUserRefs,
        messageSnapshots: message.forwardSnapshot == null
            ? const <KaedeMessageSnapshot>[]
            : <KaedeMessageSnapshot>[message.forwardSnapshot!],
        messageType: message.messageType,
        flags: message.flags,
        createdAt: message.createdAt,
        editedAt: message.editedAt,
      );

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant _ForwardedMessageCard oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.message.ref != widget.message.ref ||
        oldWidget.message.forwardedMessageRef !=
            widget.message.forwardedMessageRef ||
        oldWidget.message.forwardSnapshot != widget.message.forwardSnapshot) {
      _load();
    }
  }

  void _load() {
    final embedded = widget.message.forwardSnapshot;
    final legacy = widget.message.forwardedMessage;
    if (embedded != null) {
      _source = Future.value(embedded);
    } else if (legacy != null) {
      _source = Future.value(_materialFromMessage(legacy));
    } else {
      _source = ref
          .read(mobileControllerProvider.notifier)
          .repository
          .forwardedMessage(
            destinationChannel: widget.message.channelRef,
            destinationMessage: widget.message.ref,
          )
          .then(_materialFromMessage);
    }
  }

  @override
  Widget build(BuildContext context) => Container(
        margin: EdgeInsets.only(top: 8),
        padding: EdgeInsets.all(11),
        decoration: BoxDecoration(
          color: context.kaede.panel,
          borderRadius: BorderRadius.circular(9),
          border: Border.all(color: context.kaede.border),
        ),
        child: FutureBuilder<KaedeMessageSnapshot>(
          future: _source,
          builder: (context, snapshot) {
            if (snapshot.connectionState != ConnectionState.done) {
              return Row(children: [
                SizedBox.square(
                  dimension: 14,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
                SizedBox(width: 8),
                Text('Loading the forwarded snapshot…',
                    style: TextStyle(color: context.kaede.muted, fontSize: 12)),
              ]);
            }
            if (snapshot.hasError || snapshot.data == null) {
              return Text(
                'The forwarded snapshot is unavailable.',
                style: TextStyle(color: context.kaede.muted, fontSize: 12),
              );
            }
            final source = snapshot.data!;
            return Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(children: [
                  Icon(Icons.forward_rounded,
                      size: 15, color: context.kaede.muted),
                  SizedBox(width: 5),
                  Text('FORWARDED',
                      style: TextStyle(
                          color: context.kaede.muted,
                          fontSize: 10,
                          fontWeight: FontWeight.w800)),
                ]),
                SizedBox(height: 6),
                Text(
                  'Snapshot · ${DateFormat.yMMMd().add_jm().format(source.createdAt.toLocal())}',
                  style: TextStyle(
                    color: context.kaede.muted,
                    fontSize: 11,
                  ),
                ),
                if (source.content case final content?
                    when content.isNotEmpty) ...[
                  SizedBox(height: 3),
                  KaedeMessageMarkdown(content: content, state: widget.state),
                ],
                if (source.stickerItems.isNotEmpty)
                  Padding(
                    padding: EdgeInsets.only(top: 6),
                    child: Wrap(
                      spacing: 6,
                      runSpacing: 6,
                      children: [
                        for (final item in source.stickerItems)
                          _StickerMessage(
                            sticker: (name: item.name, ref: item.ref),
                            size: source.stickerItems.length == 1 ? 180 : 120,
                          ),
                      ],
                    ),
                  ),
                for (final embed in source.embeds)
                  _RichEmbedCard(
                    embed: embed,
                    attachments: source.attachments,
                    state: widget.state,
                    allowExternalMedia: widget.message.e2ee == null,
                  ),
                if (source.components.isNotEmpty)
                  _RichMessageComponents(
                    rows: source.components,
                    state: widget.state,
                    attachments: source.attachments,
                    allowExternalMedia: widget.message.e2ee == null,
                  ),
                for (final attachment in source.attachments)
                  _AttachmentCard(
                    attachment: attachment,
                    encryptedManifest: attachment.encryptedManifest,
                    pollStatus: false,
                  ),
                for (final nested in source.messageSnapshots)
                  _NestedForwardSnapshot(
                    snapshot: nested,
                    state: widget.state,
                    encrypted: widget.message.e2ee != null,
                  ),
              ],
            );
          },
        ),
      );
}

final class _NestedForwardSnapshot extends StatelessWidget {
  const _NestedForwardSnapshot({
    required this.snapshot,
    required this.state,
    required this.encrypted,
  });

  final KaedeMessageSnapshot snapshot;
  final MobileState state;
  final bool encrypted;

  @override
  Widget build(BuildContext context) => Container(
        margin: EdgeInsets.only(top: 8),
        padding: EdgeInsets.all(9),
        decoration: BoxDecoration(
          color: context.kaede.raised,
          borderRadius: BorderRadius.circular(8),
          border: Border.all(color: context.kaede.border),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Earlier forwarded snapshot',
              style: TextStyle(
                color: context.kaede.muted,
                fontSize: 10,
                fontWeight: FontWeight.w700,
              ),
            ),
            if (snapshot.content case final content? when content.isNotEmpty)
              KaedeMessageMarkdown(content: content, state: state),
            for (final embed in snapshot.embeds)
              _RichEmbedCard(
                embed: embed,
                attachments: snapshot.attachments,
                state: state,
                allowExternalMedia: !encrypted,
              ),
            if (snapshot.components.isNotEmpty)
              _RichMessageComponents(
                rows: snapshot.components,
                state: state,
                attachments: snapshot.attachments,
                allowExternalMedia: !encrypted,
              ),
            for (final attachment in snapshot.attachments)
              _AttachmentCard(
                attachment: attachment,
                encryptedManifest: attachment.encryptedManifest,
                pollStatus: false,
              ),
          ],
        ),
      );
}

final _linkPreviewCache = <String, Future<Map<String, Object?>>>{};

Future<Map<String, Object?>> _cachedLinkPreview(
  WidgetRef ref,
  String url, {
  bool retry = false,
}) {
  final instance =
      ref.read(mobileControllerProvider.notifier).api.tokens?.instance.value ??
          '';
  final key = '$instance\n$url';
  if (retry) _linkPreviewCache.remove(key);
  return _linkPreviewCache.putIfAbsent(
    key,
    () =>
        ref.read(mobileControllerProvider.notifier).repository.linkPreview(url),
  );
}

/// Renders authored remote embed media only through Kaede's authenticated
/// same-origin link-preview capability. A bot-controlled URL is never handed
/// directly to an image provider and never receives the user's bearer token.
final class _ProxiedEmbedImage extends ConsumerStatefulWidget {
  const _ProxiedEmbedImage({required this.url, required this.fit});

  final String url;
  final BoxFit fit;

  @override
  ConsumerState<_ProxiedEmbedImage> createState() => _ProxiedEmbedImageState();
}

final class _ProxiedEmbedImageState extends ConsumerState<_ProxiedEmbedImage> {
  late Future<Map<String, Object?>> _preview;

  @override
  void initState() {
    super.initState();
    _preview = _cachedLinkPreview(ref, widget.url);
  }

  @override
  void didUpdateWidget(covariant _ProxiedEmbedImage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.url != widget.url) {
      _preview = _cachedLinkPreview(ref, widget.url);
    }
  }

  @override
  Widget build(BuildContext context) => FutureBuilder<Map<String, Object?>>(
        future: _preview,
        builder: (context, snapshot) {
          final tokens = ref.read(mobileControllerProvider.notifier).api.tokens;
          final media =
              tokens == null || snapshot.data?['media_type'] != 'image'
                  ? null
                  : linkPreviewMediaUri(
                      tokens.instance,
                      snapshot.data?['media_url'],
                    );
          if (media == null) {
            return ColoredBox(
              color: context.kaede.hover,
              child: Center(
                child: snapshot.connectionState == ConnectionState.done
                    ? Icon(
                        Icons.broken_image_outlined,
                        size: 16,
                        color: context.kaede.muted,
                      )
                    : SizedBox.square(
                        dimension: 14,
                        child: CircularProgressIndicator(strokeWidth: 1.5),
                      ),
              ),
            );
          }
          return CachedNetworkImage(
            imageUrl: media.toString(),
            httpHeaders: <String, String>{
              'Authorization': 'Bearer ${tokens!.accessToken}',
            },
            fit: widget.fit,
            fadeInDuration: Duration.zero,
            placeholder: (_, __) => ColoredBox(
              color: context.kaede.hover,
              child: Center(
                child: SizedBox.square(
                  dimension: 14,
                  child: CircularProgressIndicator(strokeWidth: 1.5),
                ),
              ),
            ),
            errorWidget: (_, __, ___) => ColoredBox(
              color: context.kaede.hover,
              child: Center(
                child: Icon(
                  Icons.broken_image_outlined,
                  size: 16,
                  color: context.kaede.muted,
                ),
              ),
            ),
          );
        },
      );
}

final class _LinkPreviewCard extends ConsumerStatefulWidget {
  const _LinkPreviewCard({required this.url});
  final String url;

  @override
  ConsumerState<_LinkPreviewCard> createState() => _LinkPreviewCardState();
}

final class _LinkPreviewCardState extends ConsumerState<_LinkPreviewCard> {
  late Future<Map<String, Object?>> _preview;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant _LinkPreviewCard oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.url != widget.url) _load();
  }

  void _load({bool retry = false}) {
    _preview = _cachedLinkPreview(ref, widget.url, retry: retry);
  }

  Future<void> _open(Object? raw) async {
    final uri = Uri.tryParse('${raw ?? widget.url}');
    if (uri != null && const {'https', 'http'}.contains(uri.scheme)) {
      await launchUrl(uri, mode: LaunchMode.externalApplication);
    }
  }

  @override
  Widget build(BuildContext context) => FutureBuilder<Map<String, Object?>>(
        future: _preview,
        builder: (context, snapshot) {
          if (snapshot.connectionState != ConnectionState.done) {
            return Padding(
              padding: EdgeInsets.only(top: 8),
              child: LinearProgressIndicator(minHeight: 2),
            );
          }
          if (snapshot.hasError) {
            return Container(
              margin: EdgeInsets.only(top: 8),
              padding: EdgeInsets.fromLTRB(12, 8, 6, 8),
              decoration: BoxDecoration(
                color: context.kaede.raised,
                borderRadius: BorderRadius.circular(KaedeRadius.medium),
                border: Border.all(color: context.kaede.border),
              ),
              child: Row(children: [
                Expanded(
                  child: Text('Link preview unavailable',
                      style:
                          TextStyle(color: context.kaede.muted, fontSize: 12)),
                ),
                TextButton(
                    onPressed: () => _open(widget.url), child: Text('Open')),
                TextButton(
                  onPressed: () => setState(() => _load(retry: true)),
                  child: Text('Retry'),
                ),
              ]),
            );
          }
          final preview = snapshot.data!;
          final session =
              ref.read(mobileControllerProvider.notifier).api.tokens;
          final media = session == null
              ? null
              : linkPreviewMediaUri(session.instance, preview['media_url']);
          final mediaHeaders = media == null || session == null
              ? const <String, String>{}
              : <String, String>{
                  'Authorization': 'Bearer ${session.accessToken}'
                };
          final mediaIsImage = preview['media_type'] == 'image' &&
              media != null &&
              media.scheme == 'https';
          final title = '${preview['title'] ?? ''}'.trim();
          final description = '${preview['description'] ?? ''}'.trim();
          final site = '${preview['site_name'] ?? ''}'.trim();
          if (!mediaIsImage &&
              title.isEmpty &&
              description.isEmpty &&
              site.isEmpty) {
            return SizedBox.shrink();
          }
          return Card(
            margin: EdgeInsets.only(top: 8),
            clipBehavior: Clip.antiAlias,
            child: InkWell(
              onTap: () => _open(preview['url']),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  if (mediaIsImage)
                    ConstrainedBox(
                      constraints: BoxConstraints(maxHeight: 240),
                      child: CachedNetworkImage(
                        imageUrl: media.toString(),
                        httpHeaders: mediaHeaders,
                        fit: BoxFit.cover,
                        errorWidget: (_, __, ___) => SizedBox.shrink(),
                      ),
                    ),
                  if (title.isNotEmpty ||
                      description.isNotEmpty ||
                      site.isNotEmpty)
                    Padding(
                      padding: EdgeInsets.all(12),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          if (site.isNotEmpty)
                            Text(site.toUpperCase(),
                                style: TextStyle(
                                    color: context.kaede.muted,
                                    fontSize: 10,
                                    fontWeight: FontWeight.w700)),
                          if (title.isNotEmpty) ...[
                            SizedBox(height: 4),
                            Text(title,
                                maxLines: 2,
                                overflow: TextOverflow.ellipsis,
                                style: TextStyle(fontWeight: FontWeight.w800)),
                          ],
                          if (description.isNotEmpty) ...[
                            SizedBox(height: 5),
                            Text(description,
                                maxLines: 3,
                                overflow: TextOverflow.ellipsis,
                                style: TextStyle(
                                    color: context.kaede.muted, fontSize: 12)),
                          ],
                        ],
                      ),
                    ),
                ],
              ),
            ),
          );
        },
      );
}

/// Join, leave and removal notices, kept visually quieter than real messages.
String? stageSystemMessageText(
  int messageType,
  String author,
  String? topic,
) {
  final resolvedTopic =
      topic?.trim().isNotEmpty == true ? topic!.trim() : 'Untitled Stage';
  return switch (messageType) {
    27 => '$author started a Stage: $resolvedTopic',
    28 => '$author ended the Stage: $resolvedTopic',
    29 => '$author became a speaker.',
    31 => '$author changed the Stage topic: $resolvedTopic',
    _ => null,
  };
}

final class _SystemMessageRow extends StatelessWidget {
  const _SystemMessageRow({
    required this.message,
    required this.knownChannels,
    this.onJump,
    this.onMenu,
  });

  final KaedeMessage message;
  final Iterable<KaedeChannel> knownChannels;
  final VoidCallback? onJump;
  final VoidCallback? onMenu;

  @override
  Widget build(BuildContext context) {
    final icon = switch (message.messageType) {
      3 => Icons.person_add_alt_1_rounded,
      4 => Icons.logout_rounded,
      6 => Icons.push_pin_rounded,
      12 => Icons.campaign_outlined,
      27 => Icons.mic_external_on_rounded,
      28 => Icons.stop_circle_outlined,
      29 => Icons.record_voice_over_outlined,
      31 => Icons.edit_note_rounded,
      _ => Icons.person_remove_alt_1_rounded,
    };
    final stageText = stageSystemMessageText(
      message.messageType,
      message.author?.name ?? 'A member',
      message.content,
    );
    final text = switch (message.messageType) {
      6 =>
        '${message.author?.name ?? 'A member'} pinned a message to this channel.',
      12 => channelFollowSystemMessageText(message, knownChannels),
      _ => stageText ?? message.content ?? 'Group membership changed.',
    };
    return GestureDetector(
      behavior: HitTestBehavior.opaque,
      onLongPress: onMenu,
      child: Padding(
        padding: EdgeInsets.fromLTRB(12, 6, 14, 6),
        child: Row(
          children: [
            SizedBox(
              width: 40,
              child: Center(
                child: Container(
                  width: 26,
                  height: 26,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: context.kaede.coralSoft,
                  ),
                  child: Icon(icon, size: 14, color: context.kaede.coralText),
                ),
              ),
            ),
            SizedBox(width: 8),
            Expanded(
              child: InkWell(
                onTap: message.messageType == 6 ? onJump : null,
                borderRadius: BorderRadius.circular(KaedeRadius.small),
                child: Padding(
                  padding: EdgeInsets.symmetric(vertical: 4),
                  child: Text(
                    text,
                    style: TextStyle(color: context.kaede.muted, fontSize: 13),
                  ),
                ),
              ),
            ),
            SizedBox(width: 8),
            Text(
              DateFormat.jm().format(message.createdAt.toLocal()),
              style: TextStyle(color: context.kaede.muted, fontSize: 11),
            ),
          ],
        ),
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
        padding: EdgeInsets.only(top: 2, bottom: 3, right: 4),
        child: Row(
          children: [
            Padding(
              padding: EdgeInsets.only(right: 6),
              child: Icon(Icons.reply_rounded,
                  size: 13, color: context.kaede.muted),
            ),
            if (author != null) ...[
              UserAvatar(user: author, radius: 8),
              SizedBox(width: 5),
            ],
            Text(
              author?.name ?? 'Original message',
              style: TextStyle(
                fontWeight: FontWeight.w600,
                fontSize: 12.5,
                color: context.kaede.textSoft,
              ),
            ),
            SizedBox(width: 6),
            Expanded(
              child: Text(
                replyReferencePreview(referenced),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  color: context.kaede.muted,
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
        margin: EdgeInsets.only(top: 2, bottom: 2),
        padding: EdgeInsets.fromLTRB(10, 9, 12, 9),
        decoration: BoxDecoration(
          color: context.kaede.raised,
          borderRadius: BorderRadius.circular(KaedeRadius.medium),
          border: Border.all(color: context.kaede.border),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(Icons.lock_outline_rounded,
                size: 16, color: context.kaede.muted),
            SizedBox(width: 8),
            Expanded(
              child: Text(
                'Can\u2019t decrypt this message on this device. Verify, recover, or '
                'update this device\u2019s encryption support.',
                style: TextStyle(
                  color: context.kaede.muted,
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
    privateMediaUrl: attachment.privateMediaUrl,
    durationSecs: manifest['duration_millis'] is int
        ? (manifest['duration_millis']! as int) / 1000
        : null,
    waveform: manifest['waveform'] as String?,
  );
}

final class _AttachmentCard extends ConsumerStatefulWidget {
  const _AttachmentCard({
    required this.attachment,
    this.encryptedManifest,
    this.onActions,
    this.pollStatus = true,
    this.compact = false,
    this.compactFit = BoxFit.cover,
  });
  final KaedeAttachment attachment;
  final Map<String, Object?>? encryptedManifest;
  final bool pollStatus;
  final bool compact;
  final BoxFit compactFit;
  final _OpenAttachmentActions? onActions;

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
      margin: EdgeInsets.only(top: 7),
      padding: EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: context.kaede.raised,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(
          color: error ? context.kaede.danger : context.kaede.border,
        ),
      ),
      child: Row(
        children: [
          Icon(icon, color: error ? context.kaede.danger : context.kaede.muted),
          SizedBox(width: 10),
          Expanded(
            child: Text(
              message,
              maxLines: error ? 3 : 1,
              overflow: TextOverflow.ellipsis,
            ),
          ),
          if (onRetry != null)
            TextButton(onPressed: onRetry, child: Text('Retry')),
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
        padding: EdgeInsets.only(top: 7),
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
                        return AspectRatio(
                          aspectRatio: 16 / 9,
                          child: ColoredBox(
                            color: context.kaede.raised,
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
                    placeholder: (_, __) => AspectRatio(
                      aspectRatio: 16 / 9,
                      child: ColoredBox(
                        color: context.kaede.raised,
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
    final client = HttpClient()..connectionTimeout = Duration(seconds: 12);
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
            throw HttpException('Invalid media redirect');
          }
          final next = uri.resolve(location);
          if (next.scheme != 'https') {
            throw HttpException('Unsafe media redirect');
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
          throw HttpException('Media is too large');
        }
        final temporary = File('${destination.path}.part');
        final sink = temporary.openWrite();
        var received = 0;
        try {
          await for (final chunk in response) {
            received += chunk.length;
            if (received > maximum) {
              throw HttpException('Media is too large');
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
      throw HttpException('Too many media redirects');
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
          leading: Icon(Icons.link_rounded),
          title: Text('Copy media link'),
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
          color: context.kaede.raised,
          child: Padding(
            padding: EdgeInsets.all(12),
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Icon(
                  Icons.broken_image_outlined,
                  color: context.kaede.danger,
                ),
                SizedBox(height: 8),
                Text(
                  userFacingError(
                    error,
                    summary: 'Could not load the media preview',
                  ),
                  maxLines: 3,
                  overflow: TextOverflow.ellipsis,
                  textAlign: TextAlign.center,
                  style: TextStyle(fontSize: 12),
                ),
                TextButton(onPressed: onRetry, child: Text('Retry')),
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
        style: parentStyle ?? preferredStyle ?? TextStyle(),
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
              duration: Duration(milliseconds: 120),
              padding: EdgeInsets.symmetric(horizontal: 4, vertical: 1),
              decoration: BoxDecoration(
                // Covered spoilers are a dark block, not a bright slab: a
                // light fill reads as a broken image in a dark transcript.
                color: _revealed ? context.kaede.raised : context.kaede.hover,
                borderRadius: BorderRadius.circular(4),
                border: Border.all(
                  color: _revealed
                      ? context.kaede.border
                      : context.kaede.borderStrong,
                ),
              ),
              child: Text(
                widget.text,
                style: widget.style.copyWith(
                  color: _revealed ? context.kaede.text : Colors.transparent,
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
    final style = parentStyle ?? preferredStyle ?? TextStyle();
    if (kind == _MessageTokenKind.emoji) {
      final emoji = tryParseReactionEmoji(token);
      final ref = emoji?.customRef;
      if (emoji == null || ref == null) return Text(token, style: style);
      return CustomEmojiImage(ref: ref, label: emoji.label, size: 22);
    }

    var label = token;
    var foreground = context.kaede.coral;
    var background = context.kaede.coral.withValues(alpha: .14);
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
      foreground = context.kaede.text;
      background = context.kaede.selected;
    }
    return Container(
      padding: EdgeInsets.symmetric(horizontal: 3, vertical: 1),
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
        oldWidget.pollStatus != widget.pollStatus ||
        oldWidget.attachment.historyMediaUrl !=
            widget.attachment.historyMediaUrl ||
        oldWidget.attachment.privateMediaUrl !=
            widget.attachment.privateMediaUrl) {
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
    if (!widget.pollStatus) return;
    if (_displayAttachment.scanStatus == 'clean' ||
        _displayAttachment.scanStatus == 'encrypted' ||
        _displayAttachment.scanStatus == 'rejected' ||
        _displayAttachment.scanStatus == 'infected' ||
        _displayAttachment.scanStatus == 'failed') {
      return;
    }
    _statusTimer = Timer(Duration(seconds: 1), _pollStatus);
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
            privateMediaUrl: attachment.privateMediaUrl,
          );
    if (_disposed || generation != _loadGeneration) {
      throw FileSystemException('Attachment load was cancelled.');
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
          throw FileSystemException('Attachment load was cancelled.');
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
        await Future<void>.delayed(Duration(seconds: 1));
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

  void _openActions() => unawaited(widget.onActions?.call(
        widget.attachment,
        widget.encryptedManifest,
        _file,
      ));

  @override
  Widget build(BuildContext context) {
    final attachment = _displayAttachment;
    if (attachment.scanStatus == 'pending' ||
        attachment.scanStatus == 'processing') {
      if (widget.compact) {
        return ColoredBox(
          color: context.kaede.hover,
          child: Center(
            child: SizedBox.square(
              dimension: 14,
              child: CircularProgressIndicator(strokeWidth: 1.5),
            ),
          ),
        );
      }
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
      if (widget.compact) {
        return ColoredBox(
          color: context.kaede.hover,
          child: Center(
            child: Icon(
              Icons.broken_image_outlined,
              size: 16,
              color: context.kaede.muted,
            ),
          ),
        );
      }
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
          if (widget.compact) {
            return ColoredBox(
              color: context.kaede.hover,
              child: Center(
                child: Icon(
                  Icons.broken_image_outlined,
                  size: 16,
                  color: context.kaede.muted,
                ),
              ),
            );
          }
          return Container(
            margin: EdgeInsets.only(top: 7),
            padding: EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: context.kaede.raised,
              borderRadius: BorderRadius.circular(14),
              border: Border.all(color: context.kaede.danger),
            ),
            child: Row(
              children: [
                Icon(Icons.broken_image_outlined, color: context.kaede.danger),
                SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        attachment.filename,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                      ),
                      SizedBox(height: 2),
                      Text(
                        userFacingError(
                          snapshot.error!,
                          summary: 'Could not load the attachment',
                        ),
                        maxLines: 3,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(fontSize: 12),
                      ),
                    ],
                  ),
                ),
                TextButton(
                  onPressed: _retry,
                  child: Text('Retry'),
                ),
              ],
            ),
          );
        }
        if (!snapshot.hasData) {
          if (widget.compact) {
            return ColoredBox(
              color: context.kaede.hover,
              child: Center(
                child: SizedBox.square(
                  dimension: 14,
                  child: CircularProgressIndicator(strokeWidth: 1.5),
                ),
              ),
            );
          }
          final loading = Container(
            height: attachment.contentType.startsWith('image/') ? null : 62,
            margin: EdgeInsets.only(top: 7),
            decoration: BoxDecoration(
                color: context.kaede.raised,
                borderRadius: BorderRadius.circular(14)),
            child: Center(child: CircularProgressIndicator()),
          );
          return attachment.contentType.startsWith('image/')
              ? AspectRatio(aspectRatio: imageRatio, child: loading)
              : loading;
        }
        final file = snapshot.data!;
        if (attachment.contentType.startsWith('image/')) {
          if (widget.compact) {
            return Image.file(
              file,
              fit: widget.compactFit,
              width: double.infinity,
              height: double.infinity,
              errorBuilder: (_, __, ___) => ColoredBox(
                color: context.kaede.hover,
                child: Center(
                  child: Icon(
                    Icons.broken_image_outlined,
                    size: 16,
                    color: context.kaede.muted,
                  ),
                ),
              ),
            );
          }
          return AspectRatio(
            aspectRatio: imageRatio,
            child: Padding(
              padding: EdgeInsets.only(top: 7),
              child: Semantics(
                button: true,
                label: 'Open image ${attachment.filename}',
                child: GestureDetector(
                  onLongPress: widget.onActions == null ? null : _openActions,
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
                                  icon: Icon(Icons.close_rounded))),
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
            onLongPress: widget.onActions == null ? null : _openActions,
            child: _FileVideo(file: file),
          );
        }
        if (attachment.contentType.startsWith('audio/')) {
          return GestureDetector(
            onLongPress: widget.onActions == null ? null : _openActions,
            child: _FileAudio(
              file: file,
              contentType: attachment.contentType,
              durationSecs: attachment.durationSecs,
              waveform: attachment.waveform,
            ),
          );
        }
        return ListTile(
          onLongPress: widget.onActions == null ? null : _openActions,
          contentPadding: EdgeInsets.zero,
          leading: Icon(Icons.insert_drive_file_outlined),
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
        padding: EdgeInsets.fromLTRB(_messageGutter, 3, 16, 3),
        child: Row(
          children: [
            const _TypingDots(),
            SizedBox(width: 8),
            Expanded(
              child: Text(
                label,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  color: context.kaede.muted,
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

final class _FileAudio extends StatefulWidget {
  const _FileAudio({
    required this.file,
    required this.contentType,
    required this.durationSecs,
    required this.waveform,
  });

  final File file;
  final String contentType;
  final double? durationSecs;
  final String? waveform;

  @override
  State<_FileAudio> createState() => _FileAudioState();
}

final class _FileAudioState extends State<_FileAudio> {
  final AudioPlayer _player = AudioPlayer();
  final List<StreamSubscription<Object?>> _subscriptions = [];
  PlayerState _state = PlayerState.stopped;
  Duration _position = Duration.zero;
  Duration? _duration;
  Object? _error;

  @override
  void initState() {
    super.initState();
    _subscriptions
      ..add(_player.onPlayerStateChanged.listen((state) {
        if (mounted) setState(() => _state = state);
      }))
      ..add(_player.onPositionChanged.listen((position) {
        if (mounted) setState(() => _position = position);
      }))
      ..add(_player.onDurationChanged.listen((duration) {
        if (mounted) setState(() => _duration = duration);
      }));
    unawaited(_prepare());
  }

  Future<void> _prepare() async {
    try {
      await _player.setSource(
        DeviceFileSource(widget.file.path, mimeType: widget.contentType),
      );
      final duration = await _player.getDuration();
      if (mounted) setState(() => _duration = duration);
    } on Object catch (error) {
      if (mounted) setState(() => _error = error);
    }
  }

  Future<void> _toggle() async {
    try {
      if (_state == PlayerState.playing) {
        await _player.pause();
      } else {
        if (_state == PlayerState.completed) await _player.seek(Duration.zero);
        await _player.resume();
      }
    } on Object catch (error) {
      if (mounted) setState(() => _error = error);
    }
  }

  Future<void> _seek(double ratio) async {
    final duration = _effectiveDuration;
    if (duration <= Duration.zero) return;
    await _player.seek(duration * ratio.clamp(0, 1));
  }

  Duration get _effectiveDuration =>
      _duration ??
      Duration(
        milliseconds: ((widget.durationSecs ?? 0) * 1000).round(),
      );

  @override
  void dispose() {
    for (final subscription in _subscriptions) {
      unawaited(subscription.cancel());
    }
    unawaited(_player.dispose());
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (_error != null) {
      return Padding(
        padding: EdgeInsets.only(top: 7),
        child: _MediaDecodeError(
          kind: 'audio',
          onRetry: () {
            setState(() => _error = null);
            unawaited(_prepare());
          },
        ),
      );
    }
    final duration = _effectiveDuration;
    final progress = duration.inMilliseconds <= 0
        ? 0.0
        : (_position.inMilliseconds / duration.inMilliseconds)
            .clamp(0, 1)
            .toDouble();
    final samples = decodeVoiceWaveform(widget.waveform);
    return Container(
      constraints: BoxConstraints(maxWidth: 430),
      margin: EdgeInsets.only(top: 7),
      padding: EdgeInsets.fromLTRB(9, 8, 12, 8),
      decoration: BoxDecoration(
        color: context.kaede.raised,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: context.kaede.border),
      ),
      child: Row(
        children: [
          IconButton.filled(
            tooltip: _state == PlayerState.playing
                ? 'Pause voice message'
                : 'Play voice message',
            onPressed: _toggle,
            icon: Icon(
              _state == PlayerState.playing
                  ? Icons.pause_rounded
                  : Icons.play_arrow_rounded,
            ),
          ),
          SizedBox(width: 8),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Row(
                  children: [
                    Icon(Icons.mic_rounded,
                        size: 14, color: context.kaede.muted),
                    SizedBox(width: 4),
                    Expanded(
                      child: Text('Voice message',
                          style: TextStyle(fontWeight: FontWeight.w700)),
                    ),
                    Text(
                      voiceDurationLabel(
                        duration.inMilliseconds > 0
                            ? duration.inMilliseconds / 1000
                            : widget.durationSecs,
                      ),
                      style: TextStyle(
                        color: context.kaede.muted,
                        fontSize: 11,
                        fontFeatures: <FontFeature>[
                          FontFeature.tabularFigures()
                        ],
                      ),
                    ),
                  ],
                ),
                SizedBox(height: 5),
                LayoutBuilder(
                  builder: (context, constraints) => GestureDetector(
                    behavior: HitTestBehavior.opaque,
                    onTapDown: (event) => unawaited(
                      _seek(event.localPosition.dx / constraints.maxWidth),
                    ),
                    child: SizedBox(
                      height: 28,
                      child: CustomPaint(
                        painter: _VoiceWaveformPainter(
                          samples: samples,
                          progress: progress,
                          playedColor: context.kaede.coral,
                          remainingColor:
                              context.kaede.muted.withValues(alpha: .55),
                        ),
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

final class _VoiceWaveformPainter extends CustomPainter {
  const _VoiceWaveformPainter({
    required this.samples,
    required this.progress,
    required this.playedColor,
    required this.remainingColor,
  });

  final List<double> samples;
  final double progress;
  final Color playedColor;
  final Color remainingColor;

  @override
  void paint(Canvas canvas, Size size) {
    final values = samples.isEmpty
        ? List<double>.generate(48, (index) => .25 + (index % 7) / 12)
        : samples;
    final spacing = size.width / values.length;
    final past = Paint()
      ..color = playedColor
      ..strokeWidth = max(1, spacing * .58)
      ..strokeCap = StrokeCap.round;
    final future = Paint()
      ..color = remainingColor
      ..strokeWidth = past.strokeWidth
      ..strokeCap = StrokeCap.round;
    for (var index = 0; index < values.length; index += 1) {
      final x = spacing * (index + .5);
      final height = max(3, size.height * values[index].clamp(.12, 1));
      canvas.drawLine(
        Offset(x, (size.height - height) / 2),
        Offset(x, (size.height + height) / 2),
        x / size.width <= progress ? past : future,
      );
    }
  }

  @override
  bool shouldRepaint(covariant _VoiceWaveformPainter oldDelegate) =>
      oldDelegate.progress != progress ||
      oldDelegate.samples != samples ||
      oldDelegate.playedColor != playedColor ||
      oldDelegate.remainingColor != remainingColor;
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
        padding: EdgeInsets.only(top: 7),
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
      return Padding(
          padding: EdgeInsets.all(18), child: LinearProgressIndicator());
    }
    return Padding(
      padding: EdgeInsets.only(top: 7),
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
        color: context.kaede.raised,
        child: Center(
          child: Padding(
            padding: EdgeInsets.all(16),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(
                  Icons.broken_image_outlined,
                  color: context.kaede.danger,
                ),
                SizedBox(height: 8),
                Text(
                  'Could not display this $kind. The file may be damaged or use an unsupported format.',
                  textAlign: TextAlign.center,
                  style: TextStyle(fontSize: 12),
                ),
                if (onRetry != null)
                  TextButton(onPressed: onRetry, child: Text('Retry')),
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
    if (users.isEmpty) return SizedBox.shrink();
    return Container(
      constraints: BoxConstraints(maxHeight: 210),
      margin: EdgeInsets.fromLTRB(8, 2, 8, 4),
      decoration: BoxDecoration(
        color: context.kaede.panel,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: context.kaede.border),
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

final class _ApplicationCommandSheet extends StatefulWidget {
  const _ApplicationCommandSheet({
    required this.command,
    required this.users,
    required this.channels,
    required this.roles,
    required this.attachments,
    required this.onPickAttachments,
    required this.onAutocomplete,
    required this.onSubmit,
  });

  final MobileApplicationCommand command;
  final List<_CommandEntityChoice> users;
  final List<_CommandChannelChoice> channels;
  final List<_CommandEntityChoice> roles;
  final List<_CommandAttachmentChoice> attachments;
  final Future<List<_CommandAttachmentChoice>> Function(
    MobileApplicationCommandOption option,
  ) onPickAttachments;
  final Future<List<MobileApplicationCommandChoice>> Function({
    required CommandComposerValues values,
    required String path,
    required int generation,
  }) onAutocomplete;
  final Future<void> Function(CommandComposerValues values) onSubmit;

  @override
  State<_ApplicationCommandSheet> createState() =>
      _ApplicationCommandSheetState();
}

final class _ApplicationCommandSheetState
    extends State<_ApplicationCommandSheet> {
  CommandComposerValues _values = <String, Object?>{};
  late List<_CommandAttachmentChoice> _attachments = widget.attachments;
  final _controllers = <String, TextEditingController>{};
  final _suggestions = <String, List<MobileApplicationCommandChoice>>{};
  Timer? _autocompleteTimer;
  var _autocompleteGeneration = 0;
  String? _autocompletePath;
  String? _autocompleteError;
  var _showErrors = false;
  var _submitting = false;
  var _pickingFiles = false;
  String? _submitError;

  @override
  void dispose() {
    _autocompleteTimer?.cancel();
    _autocompleteGeneration += 1;
    for (final controller in _controllers.values) {
      controller.dispose();
    }
    super.dispose();
  }

  TextEditingController _controller(String path) =>
      _controllers.putIfAbsent(path, () {
        return TextEditingController(text: '${_values[path] ?? ''}');
      });

  void _invalidateAutocomplete() {
    _autocompleteTimer?.cancel();
    _autocompleteTimer = null;
    _autocompleteGeneration += 1;
    _autocompletePath = null;
    _autocompleteError = null;
  }

  void _setValue(
    String path,
    Object? value, {
    MobileApplicationCommandOption? autocompleteOption,
  }) {
    setState(() {
      _values = mobileCommandValueChanged(_values, path, value);
      _submitError = null;
      if (autocompleteOption == null || !autocompleteOption.autocomplete) {
        _invalidateAutocomplete();
      }
    });
    if (autocompleteOption?.autocomplete == true) {
      _scheduleAutocomplete(autocompleteOption!, path, '$value');
    }
  }

  void _scheduleAutocomplete(
    MobileApplicationCommandOption option,
    String path,
    String value,
  ) {
    _autocompleteTimer?.cancel();
    final generation = ++_autocompleteGeneration;
    setState(() {
      _autocompletePath = path;
      _autocompleteError = null;
    });
    _autocompleteTimer = Timer(Duration(milliseconds: 225), () async {
      try {
        final choices = await widget.onAutocomplete(
          values: CommandComposerValues.of(_values),
          path: path,
          generation: generation,
        );
        if (!mounted ||
            generation != _autocompleteGeneration ||
            _autocompletePath != path ||
            '${_values[path] ?? ''}' != value) {
          return;
        }
        setState(() {
          _suggestions[path] = choices;
          _autocompletePath = null;
        });
      } on Object catch (error) {
        if (!mounted || generation != _autocompleteGeneration) return;
        setState(() {
          _autocompletePath = null;
          _autocompleteError = userFacingError(
            error,
            summary: 'Could not load suggestions',
          );
        });
      }
    });
  }

  Future<void> _pickAttachments(MobileApplicationCommandOption option) async {
    if (_pickingFiles || _attachments.length >= 10) return;
    setState(() => _pickingFiles = true);
    try {
      final attachments = await widget.onPickAttachments(option);
      if (mounted) setState(() => _attachments = attachments);
    } on Object catch (error) {
      if (mounted) {
        setState(() => _submitError = userFacingError(
              error,
              summary: 'Could not add that file',
            ));
      }
    } finally {
      if (mounted) setState(() => _pickingFiles = false);
    }
  }

  Future<void> _submit() async {
    final errors = mobileCommandOptionErrors(widget.command, _values);
    if (errors.isNotEmpty) {
      setState(() {
        _showErrors = true;
        _submitError = 'Review the highlighted command options.';
      });
      return;
    }
    _invalidateAutocomplete();
    setState(() {
      _submitting = true;
      _submitError = null;
    });
    try {
      await widget.onSubmit(CommandComposerValues.of(_values));
      if (mounted) Navigator.pop(context);
    } on Object catch (error) {
      if (mounted) {
        setState(() => _submitError = userFacingError(
              error,
              summary: 'The bot command could not be delivered',
            ));
      }
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final model = mobileCommandComposerModel(widget.command.options, _values);
    final errors = _showErrors
        ? mobileCommandOptionErrors(widget.command, _values)
        : const <String, String>{};
    final locale = Localizations.localeOf(context).toLanguageTag();
    final bottomInset = MediaQuery.viewInsetsOf(context).bottom;
    return FractionallySizedBox(
      heightFactor: .92,
      child: Padding(
        padding: EdgeInsets.fromLTRB(18, 10, 18, 16 + bottomInset),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Center(
              child: Container(
                width: 42,
                height: 4,
                decoration: BoxDecoration(
                  color: context.kaede.border,
                  borderRadius: BorderRadius.circular(4),
                ),
              ),
            ),
            SizedBox(height: 14),
            Row(
              children: [
                Icon(Icons.terminal_rounded),
                SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        '/${widget.command.displayName(locale)}',
                        style: Theme.of(context).textTheme.titleLarge,
                      ),
                      Text(
                        widget.command.applicationName,
                        style: TextStyle(color: context.kaede.muted),
                      ),
                    ],
                  ),
                ),
                IconButton(
                  tooltip: 'Cancel command',
                  onPressed: _submitting ? null : () => Navigator.pop(context),
                  icon: Icon(Icons.close_rounded),
                ),
              ],
            ),
            if (widget.command.displayDescription(locale).isNotEmpty) ...[
              SizedBox(height: 8),
              Text(widget.command.displayDescription(locale)),
            ],
            SizedBox(height: 14),
            Expanded(
              child: ListView(
                keyboardDismissBehavior:
                    ScrollViewKeyboardDismissBehavior.onDrag,
                children: [
                  for (final selector in model.selectors) ...[
                    DropdownButtonFormField<String>(
                      key: ValueKey(
                        'command-selector-${selector.path}-${selector.selected}',
                      ),
                      initialValue:
                          selector.selected.isEmpty ? null : selector.selected,
                      isExpanded: true,
                      decoration: InputDecoration(
                        labelText: selector.label,
                        errorText: errors[selector.path],
                        prefixIcon: Icon(Icons.account_tree_outlined),
                      ),
                      items: [
                        for (final option in selector.options)
                          DropdownMenuItem(
                            value: option.name,
                            child: Text(
                              option.displayName(locale),
                              overflow: TextOverflow.ellipsis,
                            ),
                          ),
                      ],
                      onChanged: _submitting
                          ? null
                          : (value) => _setValue(
                                selector.path,
                                value ?? '',
                              ),
                    ),
                    SizedBox(height: 14),
                  ],
                  for (final field in model.fields) ...[
                    _field(field, errors[field.path]),
                    SizedBox(height: 14),
                  ],
                  if (model.selectors.isNotEmpty && model.fields.isEmpty)
                    Padding(
                      padding: EdgeInsets.symmetric(vertical: 18),
                      child: Text(
                        'Choose the command path to see its options.',
                        style: TextStyle(color: context.kaede.muted),
                      ),
                    ),
                  if (model.selectors.isEmpty && model.fields.isEmpty)
                    Padding(
                      padding: EdgeInsets.symmetric(vertical: 18),
                      child: Text('This command has no options.'),
                    ),
                  if (_autocompleteError case final error?)
                    Padding(
                      padding: EdgeInsets.only(bottom: 10),
                      child: Text(
                        error,
                        style: TextStyle(color: context.kaede.danger),
                      ),
                    ),
                  if (_submitError case final error?)
                    Container(
                      padding: EdgeInsets.all(12),
                      decoration: BoxDecoration(
                        color: context.kaede.danger.withValues(alpha: .12),
                        borderRadius: BorderRadius.circular(10),
                      ),
                      child: Text(
                        error,
                        style: TextStyle(color: context.kaede.danger),
                      ),
                    ),
                ],
              ),
            ),
            SizedBox(height: 12),
            FilledButton.icon(
              onPressed: _submitting ? null : _submit,
              icon: _submitting
                  ? SizedBox.square(
                      dimension: 18,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : Icon(Icons.send_rounded),
              label: Text(_submitting ? 'Sending…' : 'Run command'),
            ),
          ],
        ),
      ),
    );
  }

  Widget _field(MobileCommandOptionField field, String? error) {
    final option = field.option;
    final path = field.path;
    final locale = Localizations.localeOf(context).toLanguageTag();
    final displayName = option.displayName(locale);
    final label = option.required ? displayName : '$displayName (optional)';
    final displayDescription = option.displayDescription(locale);
    final description = displayDescription.isEmpty ? null : displayDescription;
    if (option.choices.isNotEmpty && !option.autocomplete) {
      final selectedIndex = option.choices.indexWhere(
        (choice) => '${choice.value}' == '${_values[path]}',
      );
      return DropdownButtonFormField<int>(
        key: ValueKey('command-choice-$path-$selectedIndex'),
        initialValue: selectedIndex < 0 ? null : selectedIndex,
        isExpanded: true,
        decoration: InputDecoration(
          labelText: label,
          helperText: description,
          errorText: error,
        ),
        items: [
          for (final entry in option.choices.indexed)
            DropdownMenuItem(
              value: entry.$1,
              child: Text(entry.$2.displayName(locale),
                  overflow: TextOverflow.ellipsis),
            ),
        ],
        onChanged: _submitting
            ? null
            : (index) => _setValue(
                  path,
                  index == null ? null : option.choices[index].value,
                ),
      );
    }
    if (option.type == 'boolean') {
      final current = _values[path];
      final selected = current is bool ? '$current' : '';
      return DropdownButtonFormField<String>(
        key: ValueKey('command-boolean-$path-$selected'),
        initialValue: selected,
        decoration: InputDecoration(
          labelText: label,
          helperText: description,
          errorText: error,
        ),
        items: const [
          DropdownMenuItem(value: '', child: Text('Not set')),
          DropdownMenuItem(value: 'true', child: Text('True')),
          DropdownMenuItem(value: 'false', child: Text('False')),
        ],
        onChanged: _submitting
            ? null
            : (value) => _setValue(
                  path,
                  value == null || value.isEmpty ? null : value == 'true',
                ),
      );
    }
    if (option.type == 'user') {
      return _entityField(
        path: path,
        label: label,
        description: description,
        error: error,
        emptyLabel: 'Select a user',
        choices: widget.users,
      );
    }
    if (option.type == 'channel') {
      final choices = widget.channels
          .where((channel) =>
              mobileCommandOptionAllowsChannelType(option, channel.type))
          .map((channel) => (value: channel.value, label: channel.label))
          .toList(growable: false);
      return _entityField(
        path: path,
        label: label,
        description: description,
        error: error,
        emptyLabel: 'Select a channel',
        choices: choices,
      );
    }
    if (option.type == 'role') {
      return _entityField(
        path: path,
        label: label,
        description: description,
        error: error,
        emptyLabel: 'Select a role',
        choices: widget.roles,
      );
    }
    if (option.type == 'mentionable') {
      return _entityField(
        path: path,
        label: label,
        description: description,
        error: error,
        emptyLabel: 'Select a user or role',
        choices: <_CommandEntityChoice>[
          ...widget.users,
          ...widget.roles,
        ],
      );
    }
    if (option.type == 'attachment') {
      final selected = '${_values[path] ?? ''}';
      final valid = selected.isEmpty ||
          _attachments.any((attachment) => attachment.key == selected);
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          DropdownButtonFormField<String>(
            key: ValueKey('command-attachment-$path-$selected'),
            initialValue: valid ? selected : '',
            isExpanded: true,
            decoration: InputDecoration(
              labelText: label,
              helperText: description,
              errorText: error,
              prefixIcon: Icon(Icons.attach_file_rounded),
            ),
            items: [
              DropdownMenuItem(value: '', child: Text('No file')),
              for (final attachment in _attachments)
                DropdownMenuItem(
                  value: attachment.key,
                  child: Text(
                    attachment.label,
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
            ],
            onChanged:
                _submitting ? null : (value) => _setValue(path, value ?? ''),
          ),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton.icon(
              onPressed:
                  _submitting || _pickingFiles || _attachments.length >= 10
                      ? null
                      : () => _pickAttachments(option),
              icon: _pickingFiles
                  ? SizedBox.square(
                      dimension: 16,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : Icon(Icons.add_rounded),
              label: Text(_attachments.length >= 10
                  ? 'Ten files already selected'
                  : 'Add a file'),
            ),
          ),
        ],
      );
    }

    final controller = _controller(path);
    final suggestions = _suggestions[path] ?? const [];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        TextFormField(
          controller: controller,
          enabled: !_submitting,
          keyboardType: option.type == 'integer'
              ? TextInputType.numberWithOptions(signed: true)
              : option.type == 'number'
                  ? TextInputType.numberWithOptions(
                      decimal: true,
                      signed: true,
                    )
                  : TextInputType.text,
          maxLength: option.maxLength,
          decoration: InputDecoration(
            labelText: label,
            helperText: description,
            errorText: error,
            suffixIcon: _autocompletePath == path
                ? Padding(
                    padding: EdgeInsets.all(13),
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : option.autocomplete
                    ? Icon(Icons.auto_awesome_rounded)
                    : null,
          ),
          onChanged: (value) => _setValue(
            path,
            value,
            autocompleteOption: option,
          ),
        ),
        if (option.autocomplete && suggestions.isNotEmpty) ...[
          SizedBox(height: 7),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              for (final choice in suggestions.take(25))
                ActionChip(
                  avatar: Icon(Icons.auto_awesome_rounded, size: 15),
                  label: Text(choice.name),
                  onPressed: _submitting
                      ? null
                      : () {
                          controller
                            ..text = '${choice.value}'
                            ..selection = TextSelection.collapsed(
                              offset: '${choice.value}'.length,
                            );
                          _setValue(path, choice.value);
                        },
                ),
            ],
          ),
        ],
      ],
    );
  }

  Widget _entityField({
    required String path,
    required String label,
    required String? description,
    required String? error,
    required String emptyLabel,
    required List<_CommandEntityChoice> choices,
  }) {
    final selected = '${_values[path] ?? ''}';
    final valid =
        selected.isEmpty || choices.any((choice) => choice.value == selected);
    return DropdownButtonFormField<String>(
      key: ValueKey('command-entity-$path-$selected-${choices.length}'),
      initialValue: valid ? selected : '',
      isExpanded: true,
      decoration: InputDecoration(
        labelText: label,
        helperText: description,
        errorText: error,
      ),
      items: [
        DropdownMenuItem(value: '', child: Text(emptyLabel)),
        for (final choice in choices)
          DropdownMenuItem(
            value: choice.value,
            child: Text(choice.label, overflow: TextOverflow.ellipsis),
          ),
      ],
      onChanged: _submitting ? null : (value) => _setValue(path, value ?? ''),
    );
  }
}

final class _ApplicationCommandPickerSheet extends StatefulWidget {
  const _ApplicationCommandPickerSheet({
    required this.commands,
    this.contextCommands = false,
    this.contextHistory = const <String>[],
  });

  final List<MobileApplicationCommand> commands;
  final bool contextCommands;
  final List<String> contextHistory;

  @override
  State<_ApplicationCommandPickerSheet> createState() =>
      _ApplicationCommandPickerSheetState();
}

final class _ApplicationCommandPickerSheetState
    extends State<_ApplicationCommandPickerSheet> {
  final _search = TextEditingController();

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final locale = Localizations.localeOf(context).toLanguageTag();
    final contextModel = widget.contextCommands
        ? mobileContextCommandMenuModel(
            widget.commands,
            _search.text,
            locale,
            widget.contextHistory,
          )
        : null;
    final groups = contextModel?.groups ??
        mobileApplicationCommandLauncherGroups(
          widget.commands,
          _search.text,
          locale,
        );
    final frequent =
        contextModel?.frequent ?? const <MobileApplicationCommand>[];
    return FractionallySizedBox(
      heightFactor: .86,
      child: Padding(
        padding: EdgeInsets.fromLTRB(16, 12, 16, 16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text('Apps',
                      style: Theme.of(context).textTheme.headlineSmall),
                ),
                IconButton(
                  tooltip: 'Close Apps',
                  onPressed: () => Navigator.pop(context),
                  icon: Icon(Icons.close_rounded),
                ),
              ],
            ),
            SizedBox(height: 8),
            TextField(
              controller: _search,
              autofocus: true,
              decoration: InputDecoration(
                labelText: 'Search apps and commands',
                prefixIcon: Icon(Icons.search_rounded),
              ),
              onChanged: (_) => setState(() {}),
            ),
            SizedBox(height: 10),
            Expanded(
              child: groups.isEmpty && frequent.isEmpty
                  ? Center(
                      child: Text('No apps or commands match your search.'))
                  : ListView(
                      keyboardDismissBehavior:
                          ScrollViewKeyboardDismissBehavior.onDrag,
                      children: [
                        if (frequent.isNotEmpty) ...[
                          Semantics(
                            header: true,
                            child: ListTile(
                              dense: true,
                              title: Text('Frequently Used'),
                            ),
                          ),
                          for (final command in frequent)
                            _commandTile(context, command, locale),
                          Divider(),
                        ],
                        for (final group in groups) ...[
                          Semantics(
                            header: true,
                            child: ListTile(
                              dense: true,
                              title: Text(group.applicationName),
                              subtitle: Text(group.application.wire),
                            ),
                          ),
                          for (final command in group.commands)
                            _commandTile(context, command, locale),
                          Divider(),
                        ],
                      ],
                    ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _commandTile(
    BuildContext context,
    MobileApplicationCommand command,
    String locale,
  ) =>
      ListTile(
        leading: Icon(switch (command.type) {
          'message' => Icons.chat_bubble_outline_rounded,
          'user' => Icons.person_outline_rounded,
          _ => Icons.terminal_rounded,
        }),
        title: Text(
          '${widget.contextCommands ? '' : '/'}${command.displayName(locale)}',
        ),
        subtitle: Text(
          command.displayDescription(locale).isEmpty
              ? switch (command.type) {
                  'message' => 'Message command',
                  'user' => 'User command',
                  _ => 'Run command',
                }
              : command.displayDescription(locale),
        ),
        onTap: () => Navigator.pop(context, command),
      );
}

final class _CommandSuggestions extends StatelessWidget {
  const _CommandSuggestions({
    required this.commands,
    required this.showNativeThread,
    required this.onNativeThread,
    required this.onSelected,
  });

  final List<MobileApplicationCommand> commands;
  final bool showNativeThread;
  final VoidCallback onNativeThread;
  final ValueChanged<MobileApplicationCommand> onSelected;

  @override
  Widget build(BuildContext context) {
    if (commands.isEmpty && !showNativeThread) return SizedBox.shrink();
    final locale = Localizations.localeOf(context).toLanguageTag();
    return Container(
      constraints: BoxConstraints(maxHeight: 230),
      margin: EdgeInsets.fromLTRB(8, 2, 8, 4),
      decoration: BoxDecoration(
        color: context.kaede.panel,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: context.kaede.border),
      ),
      child: ListView.builder(
        shrinkWrap: true,
        itemCount: commands.length + (showNativeThread ? 1 : 0),
        itemBuilder: (context, index) {
          if (showNativeThread && index == 0) {
            return ListTile(
              dense: true,
              leading: Icon(Icons.forum_outlined),
              title: Text('/thread'),
              subtitle: Text('Create a thread with a first message'),
              onTap: onNativeThread,
            );
          }
          final command = commands[index - (showNativeThread ? 1 : 0)];
          return ListTile(
            dense: true,
            leading: Icon(Icons.terminal_rounded),
            title: Text('/${command.displayName(locale)}'),
            subtitle: Text([
              if (command.displayDescription(locale).isNotEmpty)
                command.displayDescription(locale),
              command.applicationName,
            ].join(' · ')),
            onTap: () => onSelected(command),
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
      required this.appsEnabled,
      required this.onNotifyChanged,
      required this.onCancelReply,
      required this.onRemoveUpload,
      required this.onMore,
      required this.onApps,
      required this.onMedia,
      this.idleAction,
      required this.onSend});
  final TextEditingController controller;
  final FocusNode focusNode;
  final String hint;
  final KaedeMessage? reply;
  final bool notifyReply;
  final List<_PendingUpload> uploads;
  final bool sending;
  final Duration slowModeRemaining;

  /// Narrow phones collapse attachment and media entry points into a single
  /// sheet so the text field keeps a usable width.
  final bool compact;
  final bool appsEnabled;
  final ValueChanged<bool> onNotifyChanged;
  final VoidCallback onCancelReply;
  final ValueChanged<_PendingUpload> onRemoveUpload;
  final VoidCallback onMore;
  final VoidCallback onApps;
  final VoidCallback onMedia;
  final Widget? idleAction;
  final VoidCallback onSend;

  @override
  Widget build(BuildContext context) {
    final coolingDown = slowModeRemaining > Duration.zero;
    final seconds = (slowModeRemaining.inMilliseconds / 1000).ceil();
    return SafeArea(
      top: false,
      child: Padding(
        padding: EdgeInsets.fromLTRB(8, 4, 8, 8),
        child: DecoratedBox(
          decoration: BoxDecoration(
            color: context.kaede.raised,
            borderRadius: BorderRadius.circular(24),
            border: Border.all(color: context.kaede.border),
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
                    padding: EdgeInsets.fromLTRB(10, 10, 10, 4),
                    separatorBuilder: (_, __) => SizedBox(width: 8),
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
                  padding: EdgeInsets.fromLTRB(16, 8, 16, 0),
                  child: Row(
                    children: [
                      Icon(Icons.timer_outlined,
                          size: 15, color: context.kaede.muted),
                      SizedBox(width: 6),
                      Text(
                        'Slow mode · ${seconds}s remaining',
                        style: TextStyle(
                          color: context.kaede.muted,
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
                        ? 'Add files, emoji, stickers, or GIF'
                        : 'Add files, media, or a poll',
                    size: 22,
                    onPressed: sending ? null : onMore,
                  ),
                  _ComposerButton(
                    icon: Icons.apps_rounded,
                    tooltip: 'Apps',
                    size: 20,
                    onPressed: sending || !appsEnabled ? null : onApps,
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
                      style: TextStyle(fontSize: 15.5, height: 1.35),
                      decoration: InputDecoration(
                        hintText: hint,
                        isCollapsed: true,
                        filled: false,
                        contentPadding: EdgeInsets.symmetric(
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
                      tooltip: 'GIFs, stickers, and emoji',
                      onPressed: sending ? null : onMedia,
                    ),
                  _ComposerSend(
                    controller: controller,
                    hasAttachments: uploads.isNotEmpty,
                    sending: sending,
                    enabled: !coolingDown,
                    idleAction: idleAction,
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
  });

  final IconData icon;
  final String tooltip;
  final VoidCallback? onPressed;
  final double size;

  @override
  Widget build(BuildContext context) => Padding(
        padding: EdgeInsets.fromLTRB(3, 0, 3, 4),
        child: IconButton(
          tooltip: tooltip,
          onPressed: onPressed,
          visualDensity: VisualDensity.compact,
          constraints: BoxConstraints.tightFor(width: 40, height: 40),
          padding: EdgeInsets.zero,
          style: IconButton.styleFrom(
            foregroundColor: context.kaede.textSoft,
            shape: CircleBorder(),
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
    this.idleAction,
    required this.onSend,
  });

  final TextEditingController controller;
  final bool hasAttachments;
  final bool sending;
  final bool enabled;
  final Widget? idleAction;
  final VoidCallback onSend;

  @override
  Widget build(BuildContext context) =>
      ValueListenableBuilder<TextEditingValue>(
        valueListenable: controller,
        builder: (context, value, _) {
          final ready =
              value.text.trim().isNotEmpty || hasAttachments || sending;
          return AnimatedSize(
            duration: Duration(milliseconds: 160),
            curve: Curves.easeOutCubic,
            alignment: Alignment.centerLeft,
            child: ready
                ? Padding(
                    padding: EdgeInsets.fromLTRB(2, 0, 5, 5),
                    child: IconButton.filled(
                      tooltip: 'Send message',
                      constraints: BoxConstraints.tightFor(
                        width: 38,
                        height: 38,
                      ),
                      padding: EdgeInsets.zero,
                      style: IconButton.styleFrom(
                        backgroundColor: context.kaede.coral,
                        foregroundColor: context.kaede.onCoral,
                        disabledBackgroundColor: context.kaede.hover,
                        disabledForegroundColor: context.kaede.muted,
                      ),
                      onPressed: sending || !enabled ? null : onSend,
                      icon: sending
                          ? SizedBox.square(
                              dimension: 16,
                              child: CircularProgressIndicator(
                                strokeWidth: 2,
                                color: context.kaede.muted,
                              ),
                            )
                          : Icon(Icons.arrow_upward_rounded, size: 20),
                    ),
                  )
                : idleAction ?? SizedBox(width: 6, height: 46),
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
  const _PermissionNotice({required this.message, this.onApps});

  final String message;
  final VoidCallback? onApps;

  @override
  Widget build(BuildContext context) => SafeArea(
        top: false,
        child: Padding(
          padding: EdgeInsets.fromLTRB(8, 4, 8, 8),
          child: DecoratedBox(
            decoration: BoxDecoration(
              color: context.kaede.panel,
              borderRadius: BorderRadius.circular(KaedeRadius.large),
              border: Border.all(color: context.kaede.border),
            ),
            child: Padding(
              padding: EdgeInsets.symmetric(horizontal: 16, vertical: 15),
              child: Row(
                children: [
                  Icon(Icons.lock_outline_rounded,
                      size: 17, color: context.kaede.muted),
                  SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      message,
                      style: TextStyle(
                        color: context.kaede.muted,
                        fontSize: 13,
                        height: 1.35,
                      ),
                    ),
                  ),
                  if (onApps != null)
                    IconButton(
                      tooltip: 'Apps',
                      onPressed: onApps,
                      icon: Icon(Icons.apps_rounded, size: 20),
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
        padding: EdgeInsets.fromLTRB(18, 30, 18, 20),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              width: 62,
              height: 62,
              decoration: BoxDecoration(
                color: context.kaede.raised,
                shape: BoxShape.circle,
                border: Border.all(color: context.kaede.border),
              ),
              child: Icon(
                guildChannel
                    ? Icons.tag_rounded
                    : channel.conversationType == 'group'
                        ? Icons.group_rounded
                        : Icons.alternate_email_rounded,
                size: 28,
                color: context.kaede.muted,
              ),
            ),
            SizedBox(height: 16),
            Text(
              guildChannel ? 'Welcome to $title' : title,
              style: Theme.of(context).textTheme.headlineMedium,
            ),
            SizedBox(height: 6),
            Text(
              topic?.isNotEmpty == true
                  ? topic!
                  : guildChannel
                      ? 'This is the start of #${name ?? 'channel'}. Say hello.'
                      : 'This is the beginning of your conversation.',
              style: TextStyle(color: context.kaede.muted, height: 1.4),
            ),
          ],
        ),
      ),
    );
  }
}

final class _PendingUpload {
  _PendingUpload({
    required this.commandKey,
    required this.name,
    required this.file,
    required this.size,
    required this.contentType,
    required this.temporary,
  });
  final String commandKey;
  final String name;
  final File file;
  final int size;
  final String contentType;
  final bool temporary;
  EntityRef? _commandUpload;
  EntityRef? _commandUploadChannel;
  EncryptedMobileUpload? _encryptedCommandUpload;
  EntityRef? _encryptedCommandUploadChannel;

  EntityRef? commandUploadFor(EntityRef channel) =>
      _commandUploadChannel == channel ? _commandUpload : null;

  void rememberCommandUpload(EntityRef channel, EntityRef attachment) {
    _commandUploadChannel = channel;
    _commandUpload = attachment;
  }

  EncryptedMobileUpload? encryptedCommandUploadFor(EntityRef channel) =>
      _encryptedCommandUploadChannel == channel
          ? _encryptedCommandUpload
          : null;

  void rememberEncryptedCommandUpload(
    EntityRef channel,
    EncryptedMobileUpload upload,
  ) {
    _encryptedCommandUploadChannel = channel;
    _encryptedCommandUpload = upload;
  }

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
