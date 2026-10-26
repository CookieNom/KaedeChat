import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/content_safety.dart';
import 'package:kaede_mobile/src/domain/models.dart';

void main() {
  Map<String, Object?> update(String state) => {
        'state': state,
        'message_refs': ['10@chat.example'],
        'attachment_refs': ['20@chat.example'],
      };

  test('pending review scrubs cached bodies and encrypted attachment keys', () {
    final safety = ContentSafety()..apply(update('pending_review'));
    final original = KaedeMessage(
      ref: EntityRef.parse('10@chat.example'),
      channelRef: EntityRef.parse('2@chat.example'),
      authorRef: EntityRef.parse('3@chat.example'),
      createdAt: DateTime.utc(2026),
      content: 'private',
      e2ee: const {'ciphertext': 'opaque'},
      e2eeVerified: true,
      decryptedAttachments: const [
        {
          'attachment_id': '20',
          'attachment_domain': 'chat.example',
          'key': 'secret'
        }
      ],
    );
    final protected = safety.protect(original);
    expect(protected.content, isNull);
    expect(protected.e2ee, isNull);
    expect(protected.decryptedAttachments, isEmpty);
    expect(protected.safetyLocked, isTrue);
    expect(protected.safetyState, 'pending_review');
    expect(
        safety.blocksPlaintext(
            '99@chat.example',
            jsonEncode({
              'forward_snapshot': {
                'attachments': original.decryptedAttachments
              },
            })),
        isTrue);
    expect(
        safety.blocksAttachment(EntityRef.parse('20@other.example')), isFalse);
    safety.apply(update('released'));
    expect(safety.protect(original), same(original));
  });

  test('release cannot restore confirmed or reporter-hidden content', () {
    for (final state in ['removed', 'hidden']) {
      final safety = ContentSafety()..apply(update(state));
      safety.apply(update('released'));
      expect(safety.messages['10@chat.example'], state);
      expect(() => safety.requireAttachment(EntityRef.parse('20@chat.example')),
          throwsStateError);
      expect(safety.blocksPlaintext('10@chat.example', '{}'), isTrue);
    }
    final safety = ContentSafety()..apply(update('hidden'));
    safety.apply(update('removed'));
    expect(safety.messages['10@chat.example'], 'removed');
  });

  test('malformed updates do not partially change persisted revocations', () {
    final safety = ContentSafety()..apply(update('pending_review'));
    expect(
        () => safety.apply({
              ...update('released'),
              'attachment_refs': ['invalid'],
            }),
        throwsFormatException);
    expect(safety.messages['10@chat.example'], 'pending_review');
    expect(safety.attachments['20@chat.example'], 'pending_review');
  });

  test('temporary access loss needs a fresh response and never clears verdicts',
      () {
    final message = KaedeMessage(
      ref: EntityRef.parse('10@chat.example'),
      channelRef: EntityRef.parse('2@chat.example'),
      authorRef: EntityRef.parse('3@chat.example'),
      createdAt: DateTime.utc(2026),
      content: 'authorized content',
      decryptedAttachments: const [
        {'attachment_id': '20', 'attachment_domain': 'chat.example'},
      ],
    );
    final safety = ContentSafety();
    final attachment = EntityRef.parse('20@chat.example');
    final generation = safety.attachmentGeneration(attachment);
    safety.apply(update('unavailable'));
    expect(safety.protect(message).content, isNull);
    expect(safety.acceptFreshMessage(message), isTrue);
    safety.apply(update('unavailable'));
    expect(safety.protect(message), same(message));
    expect(() => safety.requireAttachment(attachment, generation: generation),
        throwsStateError);
    safety.resetFreshAuthorizations();
    safety.apply(update('unavailable'));
    expect(safety.protect(message).content, isNull);
    for (final verdict in ['pending_review', 'hidden', 'removed']) {
      safety.apply(update(verdict));
      safety.acceptFreshMessage(message);
      safety.apply(update('unavailable'));
      expect(safety.messages[message.ref.wire], verdict);
    }
  });

  test('temporary holds preserve sealed MLS history until access is restored',
      () {
    for (final temporary in ['unavailable', 'pending_review']) {
      final safety = ContentSafety()..apply(update(temporary));
      final cache = <String, String>{
        '10@chat.example': '{"content":"history"}'
      };
      cache.removeWhere(safety.purgesPlaintext);
      expect(cache, hasLength(1));
      expect(safety.hasDestructiveRestrictions, isFalse);
      expect(safety.blocksPlaintext(cache.keys.single, cache.values.single),
          isTrue);
      if (temporary == 'pending_review') {
        safety.apply(update('released'));
      } else {
        safety.acceptFreshMessage(KaedeMessage(
          ref: EntityRef.parse('10@chat.example'),
          channelRef: EntityRef.parse('2@chat.example'),
          authorRef: EntityRef.parse('3@chat.example'),
          createdAt: DateTime.utc(2026),
          e2ee: const {'ciphertext': 'authenticated response'},
        ));
      }
      expect(safety.blocksPlaintext(cache.keys.single, cache.values.single),
          isFalse);
      safety.apply(update('removed'));
      cache.removeWhere(safety.purgesPlaintext);
      expect(cache, isEmpty);
      expect(safety.hasDestructiveRestrictions, isTrue);
    }
  });
}
