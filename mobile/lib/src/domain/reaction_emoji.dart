import 'package:kaede_mobile/src/core/refs.dart';

final _customReactionPattern = RegExp(
  r'^<(a?):([A-Za-z0-9_]{2,32}):([1-9][0-9]{0,18})@([A-Za-z0-9.-]{1,253})>$',
);

const _textVariationSelector = 0xfe0e;
const _emojiVariationSelector = 0xfe0f;
const _zeroWidthJoiner = 0x200d;
const _keycap = 0x20e3;

// Emoji 17 code points whose default Unicode presentation is text. The wire
// identity intentionally omits variation selectors; presentation restores
// U+FE0F only for these bases so reactions such as the canonical `❤` render as
// the expected color emoji without changing API, cache, or gateway keys.
const _textPresentationEmojiBases = <int>{
  0x00a9,
  0x00ae,
  0x203c,
  0x2049,
  0x2122,
  0x2139,
  0x2194,
  0x2195,
  0x2196,
  0x2197,
  0x2198,
  0x2199,
  0x21a9,
  0x21aa,
  0x2328,
  0x23cf,
  0x23ed,
  0x23ee,
  0x23ef,
  0x23f1,
  0x23f2,
  0x23f8,
  0x23f9,
  0x23fa,
  0x24c2,
  0x25aa,
  0x25ab,
  0x25b6,
  0x25c0,
  0x25fb,
  0x25fc,
  0x2600,
  0x2601,
  0x2602,
  0x2603,
  0x2604,
  0x260e,
  0x2611,
  0x2618,
  0x261d,
  0x2620,
  0x2622,
  0x2623,
  0x2626,
  0x262a,
  0x262e,
  0x262f,
  0x2638,
  0x2639,
  0x263a,
  0x2640,
  0x2642,
  0x265f,
  0x2660,
  0x2663,
  0x2665,
  0x2666,
  0x2668,
  0x267b,
  0x267e,
  0x2692,
  0x2694,
  0x2695,
  0x2696,
  0x2697,
  0x2699,
  0x269b,
  0x269c,
  0x26a0,
  0x26a7,
  0x26b0,
  0x26b1,
  0x26c8,
  0x26cf,
  0x26d1,
  0x26d3,
  0x26e9,
  0x26f0,
  0x26f1,
  0x26f4,
  0x26f7,
  0x26f8,
  0x26f9,
  0x2702,
  0x2708,
  0x2709,
  0x270c,
  0x270d,
  0x270f,
  0x2712,
  0x2714,
  0x2716,
  0x271d,
  0x2721,
  0x2733,
  0x2734,
  0x2744,
  0x2747,
  0x2763,
  0x2764,
  0x27a1,
  0x2934,
  0x2935,
  0x2b05,
  0x2b06,
  0x2b07,
  0x3030,
  0x303d,
  0x3297,
  0x3299,
  0x1f170,
  0x1f171,
  0x1f17e,
  0x1f17f,
  0x1f202,
  0x1f237,
  0x1f321,
  0x1f324,
  0x1f325,
  0x1f326,
  0x1f327,
  0x1f328,
  0x1f329,
  0x1f32a,
  0x1f32b,
  0x1f32c,
  0x1f336,
  0x1f37d,
  0x1f396,
  0x1f397,
  0x1f399,
  0x1f39a,
  0x1f39b,
  0x1f39e,
  0x1f39f,
  0x1f3cb,
  0x1f3cc,
  0x1f3cd,
  0x1f3ce,
  0x1f3d4,
  0x1f3d5,
  0x1f3d6,
  0x1f3d7,
  0x1f3d8,
  0x1f3d9,
  0x1f3da,
  0x1f3db,
  0x1f3dc,
  0x1f3dd,
  0x1f3de,
  0x1f3df,
  0x1f3f3,
  0x1f3f5,
  0x1f3f7,
  0x1f43f,
  0x1f441,
  0x1f4fd,
  0x1f549,
  0x1f54a,
  0x1f56f,
  0x1f570,
  0x1f573,
  0x1f574,
  0x1f575,
  0x1f576,
  0x1f577,
  0x1f578,
  0x1f579,
  0x1f587,
  0x1f58a,
  0x1f58b,
  0x1f58c,
  0x1f58d,
  0x1f590,
  0x1f5a5,
  0x1f5a8,
  0x1f5b1,
  0x1f5b2,
  0x1f5bc,
  0x1f5c2,
  0x1f5c3,
  0x1f5c4,
  0x1f5d1,
  0x1f5d2,
  0x1f5d3,
  0x1f5dc,
  0x1f5dd,
  0x1f5de,
  0x1f5e1,
  0x1f5e3,
  0x1f5e8,
  0x1f5ef,
  0x1f5f3,
  0x1f5fa,
  0x1f6cb,
  0x1f6cd,
  0x1f6ce,
  0x1f6cf,
  0x1f6e0,
  0x1f6e1,
  0x1f6e2,
  0x1f6e3,
  0x1f6e4,
  0x1f6e5,
  0x1f6e9,
  0x1f6f0,
  0x1f6f3,
};

/// One reaction key in the exact canonical form accepted by the backend.
final class ReactionEmoji {
  const ReactionEmoji._({
    required this.value,
    this.customRef,
    this.customName,
    this.animated = false,
  });

  final String value;
  final EntityRef? customRef;
  final String? customName;
  final bool animated;

  bool get isCustom => customRef != null;
  String get label => customName == null ? value : ':$customName:';
}

/// Canonicalizes a Unicode emoji or fully-qualified custom emoji token.
///
/// The backend performs NFC before removing U+FE0E/U+FE0F. Every code point
/// admitted by [_isUnicodeEmojiSequence] is NFC-stable, and the only admitted
/// combining sequence is keycap, which has no composed form. Valid results are
/// therefore NFC by construction without shipping a second Unicode table in
/// the client.
ReactionEmoji parseReactionEmoji(String input) {
  final custom = _customReactionPattern.firstMatch(input);
  if (custom != null) {
    try {
      final ref = EntityRef(
        Snowflake(custom.group(3)!),
        Domain(custom.group(4)!),
      );
      final animated = custom.group(1) == 'a';
      final name = custom.group(2)!;
      return ReactionEmoji._(
        value: '<${animated ? 'a' : ''}:$name:${ref.wire}>',
        customRef: ref,
        customName: name,
        animated: animated,
      );
    } on FormatException {
      throw FormatException('Invalid custom reaction emoji.', input);
    }
  }

  final codepoints = input.runes
      .where(
        (codepoint) =>
            codepoint != _textVariationSelector &&
            codepoint != _emojiVariationSelector,
      )
      .toList(growable: false);
  final value = String.fromCharCodes(codepoints);
  if (value.isEmpty || !_isUnicodeEmojiSequence(codepoints)) {
    throw FormatException(
        'Reaction must contain exactly one valid emoji.', input);
  }
  return ReactionEmoji._(value: value);
}

ReactionEmoji? tryParseReactionEmoji(Object? input) {
  if (input is! String) return null;
  try {
    return parseReactionEmoji(input);
  } on FormatException {
    return null;
  }
}

String canonicalReactionEmoji(String input) => parseReactionEmoji(input).value;

/// Returns a display-only, emoji-qualified sequence for one reaction key.
///
/// This must never be used for persistence or API calls: selectors are visual
/// hints and canonical reaction equality deliberately ignores them.
String reactionEmojiPresentation(String input) {
  final reaction = parseReactionEmoji(input);
  if (reaction.isCustom) return reaction.value;
  final codepoints = reaction.value.runes.toList(growable: false);
  if (codepoints.length == 2 && codepoints.last == _keycap) {
    return String.fromCharCodes(<int>[
      codepoints.first,
      _emojiVariationSelector,
      _keycap,
    ]);
  }

  final presented = <int>[];
  for (var index = 0; index < codepoints.length; index++) {
    final codepoint = codepoints[index];
    presented.add(codepoint);
    final next = index + 1 < codepoints.length ? codepoints[index + 1] : null;
    final followedBySkinTone =
        next != null && next >= 0x1f3fb && next <= 0x1f3ff;
    if (_textPresentationEmojiBases.contains(codepoint) &&
        !followedBySkinTone) {
      presented.add(_emojiVariationSelector);
    }
  }
  return String.fromCharCodes(presented);
}

Map<String, int> canonicalReactionCounts(Map<String, int> counts) {
  final canonical = <String, int>{};
  for (final entry in counts.entries) {
    final emoji = canonicalReactionEmoji(entry.key);
    canonical[emoji] = (canonical[emoji] ?? 0) + entry.value;
  }
  return Map<String, int>.unmodifiable(canonical);
}

Set<String> canonicalReactedEmoji(Iterable<String> values) =>
    Set<String>.unmodifiable(values.map(canonicalReactionEmoji));

/// Prefers Kaede's canonical sparse `reaction` key and falls back to either the
/// legacy string or Discord-shaped partial object carried in `emoji`.
String? gatewayReactionEmoji(Map<String, Object?> data) {
  final rawLegacy = data['reaction'];
  final legacy = tryParseReactionEmoji(rawLegacy);
  if (rawLegacy != null && legacy == null) {
    throw const FormatException('Legacy reaction gateway emoji is invalid.');
  }
  return legacy?.value ?? _structuredReactionEmoji(data['emoji'])?.value;
}

ReactionEmoji? _structuredReactionEmoji(Object? value) {
  if (value == null) return null;
  if (value is String) return parseReactionEmoji(value);
  if (value is! Map || value.keys.any((key) => key is! String)) {
    throw const FormatException('Reaction gateway emoji is invalid.');
  }
  final emoji = Map<String, Object?>.from(value);
  final rawId = emoji['id'];
  final rawName = emoji['name'];
  final rawAnimated = emoji['animated'];
  if (rawAnimated != null && rawAnimated is! bool) {
    throw const FormatException(
        'Reaction gateway emoji animation flag is invalid.');
  }
  if (rawId == null) {
    if (rawName is! String) {
      throw const FormatException('Unicode reaction gateway emoji is invalid.');
    }
    return parseReactionEmoji(rawName);
  }
  if (rawName is! String) {
    throw const FormatException('Custom reaction gateway emoji is invalid.');
  }
  late final EntityRef ref;
  try {
    if (rawId is String && rawId.contains('@')) {
      ref = EntityRef.parse(rawId);
    } else {
      final domain = emoji['origin_domain'] ?? emoji['domain'];
      if (domain == null) throw const FormatException();
      ref = EntityRef(Snowflake('$rawId'), Domain('$domain'));
    }
  } on FormatException {
    throw const FormatException(
        'Custom reaction gateway emoji reference is invalid.');
  }
  final prefix = rawAnimated == true ? 'a' : '';
  return parseReactionEmoji('<$prefix:$rawName:${ref.wire}>');
}

bool _isUnicodeEmojiSequence(List<int> codepoints) {
  if (codepoints.length == 2 &&
      codepoints.every((item) => item >= 0x1f1e6 && item <= 0x1f1ff)) {
    return true;
  }
  if (codepoints.length == 2 &&
      '#*0123456789'.runes.contains(codepoints.first) &&
      codepoints.last == _keycap) {
    return true;
  }
  if (codepoints.length >= 3 &&
      codepoints.first == 0x1f3f4 &&
      codepoints.last == 0xe007f &&
      codepoints
          .skip(1)
          .take(codepoints.length - 2)
          .every((item) => item >= 0xe0061 && item <= 0xe007a)) {
    return true;
  }

  final segments = <List<int>>[<int>[]];
  for (final codepoint in codepoints) {
    if (codepoint == _zeroWidthJoiner) {
      if (segments.last.isEmpty) return false;
      segments.add(<int>[]);
    } else {
      segments.last.add(codepoint);
    }
  }
  if (segments.last.isEmpty) return false;
  for (final segment in segments) {
    if ((segment.length != 1 && segment.length != 2) ||
        !_isEmojiBase(segment.first)) {
      return false;
    }
    if (segment.length == 2 &&
        (segment.last < 0x1f3fb || segment.last > 0x1f3ff)) {
      return false;
    }
  }
  return true;
}

bool _isEmojiBase(int codepoint) =>
    codepoint >= 0x1f000 && codepoint <= 0x1faff ||
    codepoint >= 0x2600 && codepoint <= 0x27bf ||
    const <int>{
      0x00a9,
      0x00ae,
      0x203c,
      0x2049,
      0x2122,
      0x2139,
      0x2194,
      0x2195,
      0x2196,
      0x2197,
      0x2198,
      0x2199,
      0x21a9,
      0x21aa,
      0x231a,
      0x231b,
      0x2328,
      0x23cf,
      0x24c2,
      0x25aa,
      0x25ab,
      0x25b6,
      0x25c0,
      0x25fb,
      0x25fc,
      0x25fd,
      0x25fe,
      0x3030,
      0x303d,
      0x3297,
      0x3299,
    }.contains(codepoint);
