import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/painting.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/storage/local_database.dart';
import 'package:path_provider/path_provider.dart';

/// Account-scoped, durable revocations also fence late downloads and old vaults.
final contentSafety = ContentSafety();

final class ContentSafety extends ChangeNotifier {
  final Map<String, String> messages = {};
  final Map<String, String> attachments = {};
  final Map<String, Set<String>> _files = {};
  final Set<String> _orphanedFiles = {};
  final Set<String> _freshMessages = {};
  final Set<String> _freshAttachments = {};
  final Map<String, int> _attachmentGenerations = {};
  String? accountKey;
  String cursor = '0';
  LocalDatabase? _database;
  Future<void> _saveTail = Future<void>.value();

  Future<void> load(LocalDatabase database, String account) async {
    if (accountKey == account) return;
    accountKey = account;
    _database = database;
    messages.clear();
    attachments.clear();
    _files.clear();
    _orphanedFiles.clear();
    resetFreshAuthorizations();
    _attachmentGenerations.clear();
    cursor = '0';
    final directory = await getTemporaryDirectory();
    await for (final entry in directory.list()) {
      final name = entry.uri.pathSegments.last;
      if (entry is File &&
          (name.startsWith('kaede-report-') ||
              name.startsWith('kaede-forward-'))) {
        // Report dialogs and legacy forwarding never resume across startups.
        _orphanedFiles.add(entry.path);
      }
    }
    final rows = await database.snapshots(account, 'content_safety');
    if (accountKey != account || rows.isEmpty) return;
    final row = rows.single;
    cursor = '${row['cursor'] ?? '0'}';
    for (final (key, destination) in [
      ('messages', messages),
      ('attachments', attachments),
    ]) {
      final raw = row[key];
      if (raw is Map) {
        for (final entry in raw.entries) {
          destination[EntityRef.parse('${entry.key}').wire] = '${entry.value}';
        }
      }
    }
    notifyListeners();
  }

  Future<void> save() {
    final account = accountKey;
    final database = _database;
    if (account == null || database == null) return Future<void>.value();
    final payload = {
      'cursor': cursor,
      'messages': Map<String, String>.of(messages),
      'attachments': Map<String, String>.of(attachments),
    };
    final write = _saveTail.then((_) =>
        database.putSnapshot(account, 'content_safety', 'state', payload));
    _saveTail = write.catchError((Object _) {});
    return write;
  }

  void apply(Map<String, Object?> update) {
    final state = update['state'];
    if (!const {
      'pending_review',
      'removed',
      'hidden',
      'released',
      'unavailable'
    }.contains(state)) {
      throw const FormatException('Invalid content safety state.');
    }
    final parsed = <String, List<String>>{};
    for (final key in ['message_refs', 'attachment_refs']) {
      final refs = update[key];
      if (refs is! List || refs.length > 10000) {
        throw const FormatException('Invalid content safety references.');
      }
      parsed[key] = [
        for (final raw in refs)
          if (raw is String)
            EntityRef.parse(raw).wire
          else
            throw const FormatException('Invalid safety reference.'),
      ];
    }
    for (final (key, destination) in [
      ('message_refs', messages),
      ('attachment_refs', attachments),
    ]) {
      for (final ref in parsed[key]!) {
        if (state == 'unavailable' &&
            (key == 'message_refs' ? _freshMessages : _freshAttachments)
                .contains(ref)) {
          continue;
        }
        if (state == 'released') {
          if (destination[ref] == 'pending_review' ||
              destination[ref] == 'unavailable') {
            destination.remove(ref);
          }
        } else if (destination[ref] != 'removed' &&
            (destination[ref] != 'hidden' || state == 'removed') &&
            !(state == 'unavailable' && destination[ref] == 'pending_review')) {
          destination[ref] = state! as String;
          if (key == 'attachment_refs') {
            _attachmentGenerations[ref] =
                (_attachmentGenerations[ref] ?? 0) + 1;
          }
        }
      }
    }
    notifyListeners();
  }

  /// A complete authenticated response can restore access after a membership
  /// change or remote-history lookup. It never clears an actual safety verdict.
  bool acceptFreshMessage(KaedeMessage message) {
    if (message.safetyState != null ||
        message.safetyLocked ||
        message.deletedAt != null) {
      return false;
    }
    var changed = false;
    void authorize(String ref, bool attachment) {
      final destination = attachment ? attachments : messages;
      (attachment ? _freshAttachments : _freshMessages).add(ref);
      if (destination[ref] == 'unavailable') {
        destination.remove(ref);
        changed = true;
      }
    }

    void visit(Object? value) {
      if (value is List) {
        value.forEach(visit);
        return;
      }
      if (value is! Map) return;
      for (final pair in [
        ('attachment_id', 'attachment_domain'),
        ('id', 'origin_domain'),
        ('message_id', 'message_domain'),
      ]) {
        final id = value[pair.$1];
        final domain = value[pair.$2];
        if (id is String && domain is String) {
          final ref = '$id@$domain';
          if (pair.$1 == 'attachment_id' ||
              (pair.$1 == 'id' &&
                  value.containsKey('filename') &&
                  value.containsKey('content_type'))) {
            authorize(ref, true);
          } else if (pair.$1 == 'message_id' ||
              (pair.$1 == 'id' &&
                  value.containsKey('channel_id') &&
                  value.containsKey('author_id'))) {
            authorize(ref, false);
          }
        }
      }
      value.values.forEach(visit);
    }

    visit(message.toJson());
    if (changed) notifyListeners();
    return changed;
  }

  void resetFreshAuthorizations() {
    _freshMessages.clear();
    _freshAttachments.clear();
  }

  bool blocksAttachment(EntityRef ref) => attachments.containsKey(ref.wire);

  int attachmentGeneration(EntityRef ref) =>
      _attachmentGenerations[ref.wire] ?? 0;

  void requireAttachment(EntityRef ref, {int? generation}) {
    if (blocksAttachment(ref) ||
        (generation != null && generation != attachmentGeneration(ref))) {
      throw StateError('This attachment is unavailable for safety reasons.');
    }
  }

  void trackFile(EntityRef ref, File file) {
    requireAttachment(ref);
    _files.putIfAbsent(ref.wire, () => <String>{}).add(file.path);
  }

  bool get hasDestructiveRestrictions => [
        ...messages.values,
        ...attachments.values
      ].any((state) => state == 'removed' || state == 'hidden');

  bool blocksValue(Object? value) =>
      _blocksValue(value, destructiveOnly: false);

  bool _restricted(String? state, {required bool destructiveOnly}) =>
      state != null &&
      (!destructiveOnly || state == 'removed' || state == 'hidden');

  bool _blocksValue(Object? value, {required bool destructiveOnly}) {
    bool visit(Object? value) =>
        _blocksValue(value, destructiveOnly: destructiveOnly);
    if (value is List) return value.any(visit);
    if (value is! Map) return false;
    for (final pair in [
      ('attachment_id', 'attachment_domain'),
      ('id', 'origin_domain'),
      ('message_id', 'message_domain'),
    ]) {
      final id = value[pair.$1];
      final domain = value[pair.$2];
      if (id is String &&
          domain is String &&
          (_restricted(attachments['$id@$domain'],
                  destructiveOnly: destructiveOnly) ||
              _restricted(messages['$id@$domain'],
                  destructiveOnly: destructiveOnly))) {
        return true;
      }
    }
    return value.values.any(visit);
  }

  bool blocksPlaintext(String? messageRef, String plaintext) {
    return _blocksPlaintext(messageRef, plaintext, destructiveOnly: false);
  }

  // Temporary holds retain sealed MLS history so consumed ciphertext can recover
  // after dismissal or fresh authorization. Confirmed/reporter-hidden copies purge.
  bool purgesPlaintext(String? messageRef, String plaintext) {
    return _blocksPlaintext(messageRef, plaintext, destructiveOnly: true);
  }

  bool _blocksPlaintext(String? messageRef, String plaintext,
      {required bool destructiveOnly}) {
    if (_restricted(messages[messageRef], destructiveOnly: destructiveOnly)) {
      return true;
    }
    try {
      return _blocksValue(jsonDecode(plaintext),
          destructiveOnly: destructiveOnly);
    } on FormatException {
      return false;
    }
  }

  KaedeMessage protect(KaedeMessage message) {
    final status = messages[message.ref.wire] ?? message.safetyState;
    if (status == null &&
        !message.safetyLocked &&
        !blocksValue(message.toJson())) {
      return message;
    }
    return message.copyWith(
      clearContent: true,
      stickerItems: const [],
      embeds: const [],
      components: const [],
      clearForwardedMessageRef: true,
      clearForwardedMessage: true,
      clearForwardSnapshot: true,
      clearDecryptedForwardSnapshot: true,
      clearReferencedMessage: true,
      clearPoll: true,
      clearPollResult: true,
      clearEncryptedPollProjection: true,
      clearE2ee: true,
      e2eeVerified: false,
      attachments: const [],
      decryptedAttachments: const [],
      clearDecryptedAllowedMentions: true,
      mentionUserRefs: const [],
      mentionRoleRefs: const [],
      mentionEveryone: false,
      reactionCounts: const {},
      reactedEmoji: const {},
      pinned: false,
      contentUnavailable: true,
      safetyState: status ??
          (_blocksValue(message.toJson(), destructiveOnly: true)
              ? 'removed'
              : 'unavailable'),
      safetyLocked: true,
    );
  }

  Future<void> purgeFiles() async {
    if (attachments.isEmpty && _orphanedFiles.isEmpty) return;
    final directory = await getTemporaryDirectory();
    final paths = <String>{
      ..._orphanedFiles,
      for (final ref in attachments.keys) ...?_files[ref],
    };
    final prefixes = attachments.keys.map((wire) {
      final ref = EntityRef.parse(wire);
      return 'kaede-media-${ref.id.value}-${ref.domain.value}-';
    }).toList();
    await for (final entry in directory.list()) {
      if (entry is File &&
          prefixes.any(entry.uri.pathSegments.last.startsWith)) {
        paths.add(entry.path);
      }
    }
    for (final path in paths) {
      final file = File(path);
      await FileImage(file).evict();
      if (await file.exists()) {
        try {
          await file.delete();
        } on FileSystemException {
          // Let a just-closed native decoder release its handle, then retry.
          await Future<void>.delayed(const Duration(milliseconds: 250));
          if (await file.exists()) await file.delete();
        }
      }
      final partial = File('$path.part');
      if (await partial.exists()) await partial.delete();
      _orphanedFiles.remove(path);
    }
  }
}

String contentSafetyLabel(String? state) => switch (state) {
      'pending_review' =>
        'Content hidden while a safety review is in progress.',
      'hidden' => 'You reported this content. It is hidden on this device.',
      'unavailable' =>
        'This content is unavailable. Reopen the conversation to check access.',
      _ => 'Content removed for safety reasons.',
    };
