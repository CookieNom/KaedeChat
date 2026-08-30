import 'package:kaede_mobile/src/core/refs.dart';

typedef RichJson = Map<String, Object?>;

String? _text(Object? value) => value is String ? value : null;
int _int(Object? value, [int fallback = 0]) =>
    value is num ? value.toInt() : int.tryParse('$value') ?? fallback;
bool _bool(Object? value, [bool fallback = false]) =>
    value is bool ? value : fallback;
List<RichJson> _objects(Object? value) {
  if (value == null) return const <RichJson>[];
  if (value is! List) throw const FormatException('Expected an object array.');
  final result = <RichJson>[];
  for (final item in value) {
    if (item is! Map || item.keys.any((key) => key is! String)) {
      throw const FormatException('Object array contains an invalid child.');
    }
    result.add(Map<String, Object?>.from(item));
  }
  return List<RichJson>.unmodifiable(result);
}

final class RichEmoji {
  const RichEmoji({this.ref, this.name, this.animated = false});

  factory RichEmoji.fromJson(RichJson json) {
    EntityRef? ref;
    final rawRef = json['id'];
    if (rawRef != null) {
      try {
        ref = EntityRef.fromJson(rawRef);
      } on FormatException {
        // A missing custom emoji should not make the whole message unreadable.
      }
    }
    return RichEmoji(
      ref: ref,
      name: _text(json['name']),
      animated: _bool(json['animated']),
    );
  }

  final EntityRef? ref;
  final String? name;
  final bool animated;

  String get label => ref == null
      ? name ?? ''
      : name == null
          ? 'Custom emoji'
          : ':$name:';

  RichJson toJson() => <String, Object?>{
        'id': ref?.wire,
        'name': name,
        'animated': animated,
      };
}

final class RichEmbedField {
  const RichEmbedField(
      {required this.name, required this.value, this.inline = false});

  factory RichEmbedField.fromJson(RichJson json) => RichEmbedField(
        name: _text(json['name']) ?? '',
        value: _text(json['value']) ?? '',
        inline: _bool(json['inline']),
      );

  final String name;
  final String value;
  final bool inline;

  RichJson toJson() => <String, Object?>{
        'name': name,
        'value': value,
        'inline': inline,
      };
}

final class RichEmbed {
  const RichEmbed({
    this.type,
    this.title,
    this.description,
    this.url,
    this.timestamp,
    this.color,
    this.footerText,
    this.footerIconUrl,
    this.imageUrl,
    this.thumbnailUrl,
    this.authorName,
    this.authorUrl,
    this.authorIconUrl,
    this.fields = const <RichEmbedField>[],
  });

  factory RichEmbed.fromJson(RichJson json) {
    final footer = json['footer'] is Map
        ? Map<String, Object?>.from(json['footer']! as Map)
        : const <String, Object?>{};
    final image = json['image'] is Map
        ? Map<String, Object?>.from(json['image']! as Map)
        : const <String, Object?>{};
    final thumbnail = json['thumbnail'] is Map
        ? Map<String, Object?>.from(json['thumbnail']! as Map)
        : const <String, Object?>{};
    final author = json['author'] is Map
        ? Map<String, Object?>.from(json['author']! as Map)
        : const <String, Object?>{};
    return RichEmbed(
      type: _text(json['type']),
      title: _text(json['title']),
      description: _text(json['description']),
      url: _text(json['url']),
      timestamp: DateTime.tryParse(_text(json['timestamp']) ?? '')?.toUtc(),
      color: json['color'] == null ? null : _int(json['color']),
      footerText: _text(footer['text']),
      footerIconUrl: _text(footer['icon_url']),
      imageUrl: _text(image['url']),
      thumbnailUrl: _text(thumbnail['url']),
      authorName: _text(author['name']),
      authorUrl: _text(author['url']),
      authorIconUrl: _text(author['icon_url']),
      fields: _objects(json['fields'])
          .map(RichEmbedField.fromJson)
          .toList(growable: false),
    );
  }

  final String? type;
  final String? title;
  final String? description;
  final String? url;
  final DateTime? timestamp;
  final int? color;
  final String? footerText;
  final String? footerIconUrl;
  final String? imageUrl;
  final String? thumbnailUrl;
  final String? authorName;
  final String? authorUrl;
  final String? authorIconUrl;
  final List<RichEmbedField> fields;

  RichJson toJson() => <String, Object?>{
        if (type != null) 'type': type,
        if (title != null) 'title': title,
        if (description != null) 'description': description,
        if (url != null) 'url': url,
        if (timestamp != null) 'timestamp': timestamp!.toIso8601String(),
        if (color != null) 'color': color,
        if (footerText != null || footerIconUrl != null)
          'footer': <String, Object?>{
            if (footerText != null) 'text': footerText,
            if (footerIconUrl != null) 'icon_url': footerIconUrl,
          },
        if (imageUrl != null) 'image': <String, Object?>{'url': imageUrl},
        if (thumbnailUrl != null)
          'thumbnail': <String, Object?>{'url': thumbnailUrl},
        if (authorName != null || authorUrl != null || authorIconUrl != null)
          'author': <String, Object?>{
            if (authorName != null) 'name': authorName,
            if (authorUrl != null) 'url': authorUrl,
            if (authorIconUrl != null) 'icon_url': authorIconUrl,
          },
        'fields': fields.map((item) => item.toJson()).toList(),
      };
}

/// Returns an authored embed media URL only when it is an absolute web URL
/// that can be handed to Kaede's server-side preview/media proxy. The client
/// must never load this URL directly: embed payloads may come from federated
/// bots and therefore remain untrusted even after schema validation.
Uri? richEmbedExternalMediaUri(String? value) {
  if (value == null || value.startsWith('attachment://')) return null;
  final uri = Uri.tryParse(value);
  if (uri == null ||
      !const {'http', 'https'}.contains(uri.scheme) ||
      uri.host.isEmpty ||
      uri.userInfo.isNotEmpty) {
    return null;
  }
  return uri;
}

final class RichSelectOption {
  const RichSelectOption({
    required this.label,
    required this.value,
    this.description,
    this.emoji,
    this.isDefault = false,
  });

  factory RichSelectOption.fromJson(RichJson json) => RichSelectOption(
        label: _text(json['label']) ?? '',
        value: _text(json['value']) ?? '',
        description: _text(json['description']),
        emoji: json['emoji'] is Map
            ? RichEmoji.fromJson(
                Map<String, Object?>.from(json['emoji']! as Map))
            : null,
        isDefault: _bool(json['default']),
      );

  final String label;
  final String value;
  final String? description;
  final RichEmoji? emoji;
  final bool isDefault;

  RichJson toJson() => <String, Object?>{
        'label': label,
        'value': value,
        if (description != null) 'description': description,
        if (emoji != null) 'emoji': emoji!.toJson(),
        'default': isDefault,
      };
}

final class RichComponent {
  const RichComponent({
    required this.type,
    this.id,
    this.customId,
    this.style = 1,
    this.label,
    this.emoji,
    this.url,
    this.disabled = false,
    this.placeholder,
    this.minValues = 1,
    this.maxValues = 1,
    this.options = const <RichSelectOption>[],
    this.defaultValues = const <({EntityRef ref, String type})>[],
    this.channelTypes = const <int>[],
    this.checked = false,
    this.inputStyle = 1,
    this.minLength,
    this.maxLength,
    this.required = true,
    this.value,
    this.description,
    this.content,
    this.skuRef,
    this.fileTypes = const <String>[],
  });

  factory RichComponent.fromJson(RichJson json) {
    final rawType = json['type'];
    final type = rawType is String ? rawType : _int(rawType);
    final defaults = <({EntityRef ref, String type})>[];
    for (final item in _objects(json['default_values'])) {
      try {
        defaults.add(
            (ref: EntityRef.fromJson(item['id']), type: '${item['type']}'));
      } on FormatException {
        // Ignore only the malformed default, not the entire usable control.
      }
    }
    final options = _objects(json['options'])
        .map(RichSelectOption.fromJson)
        .toList(growable: false);
    return RichComponent(
      type: type,
      id: json['id'] == null ? null : _int(json['id']),
      customId: _text(json['custom_id']),
      style: _int(json['style'], 1),
      label: _text(json['label']),
      emoji: json['emoji'] is Map
          ? RichEmoji.fromJson(Map<String, Object?>.from(json['emoji']! as Map))
          : null,
      url: _text(json['url']),
      disabled: _bool(json['disabled']),
      placeholder: _text(json['placeholder']),
      minValues: _int(json['min_values'], 1),
      maxValues: json['max_values'] == null && type == 22
          ? options.length
          : _int(json['max_values'], 1),
      options: options,
      defaultValues: List.unmodifiable(defaults),
      channelTypes: (json['channel_types'] as List? ?? const <Object>[])
          .map(_int)
          .toList(growable: false),
      checked: _bool(json['default']),
      inputStyle: _int(json['style'], 1),
      minLength: json['min_length'] == null ? null : _int(json['min_length']),
      maxLength: json['max_length'] == null ? null : _int(json['max_length']),
      required: _bool(json['required'], true),
      value: _text(json['value']),
      description: _text(json['description']),
      content: _text(json['content']),
      skuRef: json['sku_id'] == null
          ? null
          : () {
              try {
                return EntityRef.fromJson(json['sku_id']);
              } on FormatException {
                return null;
              }
            }(),
      fileTypes: (json['file_types'] as List? ?? const <Object>[])
          .whereType<String>()
          .map((value) => value.toLowerCase())
          .toList(growable: false),
    );
  }

  final Object type;
  final int? id;
  final String? customId;
  final int style;
  final String? label;
  final RichEmoji? emoji;
  final String? url;
  final bool disabled;
  final String? placeholder;
  final int minValues;
  final int maxValues;
  final List<RichSelectOption> options;
  final List<({EntityRef ref, String type})> defaultValues;
  final List<int> channelTypes;
  final bool checked;
  final int inputStyle;
  final int? minLength;
  final int? maxLength;
  final bool required;
  final String? value;
  final String? description;
  final String? content;
  final EntityRef? skuRef;
  final List<String> fileTypes;

  bool get isButton => type == 2;
  bool get isStringSelect => type == 3;
  bool get isEntitySelect => const <int>{5, 6, 7, 8}.contains(type);
  bool get isCheckboxV2 => type == 23;
  bool get isRadioGroup => type == 21;
  bool get isCheckboxGroup => type == 22;
  bool get isFileUpload => type == 19;
  bool get isTextInput => type == 4;

  RichJson toJson() => <String, Object?>{
        'type': type,
        if (id != null) 'id': id,
        if (customId != null) 'custom_id': customId,
        if (isButton || isTextInput) 'style': style,
        if (label != null) 'label': label,
        if (emoji != null) 'emoji': emoji!.toJson(),
        if (url != null) 'url': url,
        if (skuRef != null) 'sku_id': skuRef!.wire,
        if (fileTypes.isNotEmpty) 'file_types': fileTypes,
        'disabled': disabled,
        if (placeholder != null) 'placeholder': placeholder,
        if (isStringSelect || isEntitySelect) ...<String, Object?>{
          'min_values': minValues,
          'max_values': maxValues,
        },
        if (isStringSelect || isRadioGroup || isCheckboxGroup)
          'options': options.map((item) => item.toJson()).toList(),
        if (isEntitySelect)
          'default_values': defaultValues
              .map((item) => <String, Object?>{
                    'id': item.ref.wire,
                    'type': item.type,
                  })
              .toList(),
        if (channelTypes.isNotEmpty) 'channel_types': channelTypes,
        if (isCheckboxV2) 'default': checked,
        if (description != null) 'description': description,
        if (isFileUpload || isCheckboxGroup) ...<String, Object?>{
          'min_values': minValues,
          'max_values': maxValues,
          'required': required,
        },
        if (isRadioGroup) 'required': required,
        if (isTextInput) ...<String, Object?>{
          'required': required,
          if (minLength != null) 'min_length': minLength,
          if (maxLength != null) 'max_length': maxLength,
          if (value != null) 'value': value,
        },
      };
}

final class RichActionRow {
  const RichActionRow(
    this.components, {
    this.type = 1,
    this.id,
    this.label,
    this.description,
    this.content,
  });

  factory RichActionRow.fromJson(RichJson json) {
    final type = _int(json['type'], 1);
    final components = type == 18
        ? <RichJson>[
            if (json['component'] is Map)
              Map<String, Object?>.from(json['component']! as Map),
          ]
        : _objects(json['components']);
    return RichActionRow(
      components.map(RichComponent.fromJson).toList(growable: false),
      type: type,
      id: json['id'] == null ? null : _int(json['id']),
      label: _text(json['label']),
      description: _text(json['description']),
      content: _text(json['content']),
    );
  }

  final List<RichComponent> components;
  final int type;
  final int? id;
  final String? label;
  final String? description;
  final String? content;

  RichJson toJson() => type == 10
      ? <String, Object?>{
          'type': 10,
          if (id != null) 'id': id,
          'content': content ?? '',
        }
      : type == 18
          ? <String, Object?>{
              'type': 18,
              if (id != null) 'id': id,
              if (label != null) 'label': label,
              if (description != null) 'description': description,
              if (components.isNotEmpty) 'component': components.first.toJson(),
            }
          : <String, Object?>{
              'type': 1,
              if (id != null) 'id': id,
              'components': components.map((item) => item.toJson()).toList(),
            };
}

/// A forward-compatible top-level message layout component. Interactive rows
/// keep their strongly typed [RichActionRow] projection while Components V2
/// retain their complete nested wire object for platform-native rendering.
final class RichMessageLayout {
  const RichMessageLayout._(this.raw, this.type, this.id);

  factory RichMessageLayout.fromJson(RichJson json) => RichMessageLayout._(
        Map<String, Object?>.unmodifiable(json),
        _int(json['type']),
        json['id'] == null ? null : _int(json['id']),
      );

  factory RichMessageLayout.actionRow(RichActionRow row) =>
      RichMessageLayout.fromJson(row.toJson());

  final RichJson raw;
  final int type;
  final int? id;

  RichActionRow? get actionRow =>
      type == 1 ? RichActionRow.fromJson(raw) : null;
  List<RichComponent> get components =>
      actionRow?.components ?? const <RichComponent>[];
  List<RichMessageLayout> get children => _objects(raw['components'])
      .map(RichMessageLayout.fromJson)
      .toList(growable: false);

  RichJson toJson() => Map<String, Object?>.of(raw);
}

final class RichPollMedia {
  const RichPollMedia({this.text, this.emoji});

  factory RichPollMedia.fromJson(RichJson json) => RichPollMedia(
        text: _text(json['text']),
        emoji: json['emoji'] is Map
            ? RichEmoji.fromJson(
                Map<String, Object?>.from(json['emoji']! as Map))
            : null,
      );

  final String? text;
  final RichEmoji? emoji;

  RichJson toJson() => <String, Object?>{
        if (text != null) 'text': text,
        if (emoji != null) 'emoji': emoji!.toJson(),
      };
}

final class RichPollAnswer {
  const RichPollAnswer({required this.id, required this.media});

  factory RichPollAnswer.fromJson(RichJson json) => RichPollAnswer(
        id: _int(json['answer_id']),
        media: RichPollMedia.fromJson(
          json['poll_media'] is Map
              ? Map<String, Object?>.from(json['poll_media']! as Map)
              : const <String, Object?>{},
        ),
      );

  final int id;
  final RichPollMedia media;

  RichJson toJson() => <String, Object?>{
        'answer_id': id,
        'poll_media': media.toJson(),
      };
}

final class RichPollResult {
  const RichPollResult(
      {required this.id, required this.count, required this.meVoted});

  factory RichPollResult.fromJson(RichJson json) => RichPollResult(
        id: _int(json['id']),
        count: _int(json['count']),
        meVoted: _bool(json['me_voted']),
      );

  final int id;
  final int count;
  final bool meVoted;

  RichJson toJson() => <String, Object?>{
        'id': id,
        'count': count,
        'me_voted': meVoted,
      };
}

final class RichPoll {
  const RichPoll({
    required this.question,
    required this.answers,
    required this.expiry,
    required this.allowMultiselect,
    required this.finalized,
    required this.results,
  });

  factory RichPoll.fromJson(RichJson json) {
    final result = json['results'] is Map
        ? Map<String, Object?>.from(json['results']! as Map)
        : const <String, Object?>{};
    return RichPoll(
      question: RichPollMedia.fromJson(
        json['question'] is Map
            ? Map<String, Object?>.from(json['question']! as Map)
            : const <String, Object?>{},
      ),
      answers: _objects(json['answers'])
          .map(RichPollAnswer.fromJson)
          .toList(growable: false),
      expiry: DateTime.tryParse(_text(json['expiry']) ?? '')?.toUtc(),
      allowMultiselect: _bool(json['allow_multiselect']),
      finalized: _bool(result['is_finalized']),
      results: _objects(result['answer_counts'])
          .map(RichPollResult.fromJson)
          .toList(growable: false),
    );
  }

  final RichPollMedia question;
  final List<RichPollAnswer> answers;
  final DateTime? expiry;
  final bool allowMultiselect;
  final bool finalized;
  final List<RichPollResult> results;

  RichPollResult resultFor(int answerId) => results.firstWhere(
        (item) => item.id == answerId,
        orElse: () => RichPollResult(id: answerId, count: 0, meVoted: false),
      );

  int get totalVotes => results.fold(0, (total, item) => total + item.count);
  bool isClosed([DateTime? now]) =>
      finalized ||
      expiry == null ||
      !expiry!.isAfter((now ?? DateTime.now()).toUtc());
  int percentFor(int answerId) {
    final total = totalVotes;
    return total == 0 ? 0 : (resultFor(answerId).count * 100 / total).round();
  }

  RichPoll withVote({
    required int answerId,
    required bool added,
    required bool isCurrentUser,
  }) {
    if (!answers.any((answer) => answer.id == answerId)) return this;
    final current = resultFor(answerId);
    final nextCount = current.count + (added ? 1 : -1);
    final updated = RichPollResult(
      id: answerId,
      count: nextCount < 0 ? 0 : nextCount,
      meVoted: isCurrentUser ? added : current.meVoted,
    );
    return RichPoll(
      question: question,
      answers: answers,
      expiry: expiry,
      allowMultiselect: allowMultiselect,
      finalized: finalized,
      results: <RichPollResult>[
        for (final result in results)
          if (result.id != answerId) result,
        updated,
      ]..sort((left, right) => left.id.compareTo(right.id)),
    );
  }

  RichJson toJson() => <String, Object?>{
        'question': question.toJson(),
        'answers': answers.map((item) => item.toJson()).toList(),
        'expiry': expiry?.toIso8601String(),
        'allow_multiselect': allowMultiselect,
        'layout_type': 1,
        'results': <String, Object?>{
          'is_finalized': finalized,
          'answer_counts': results.map((item) => item.toJson()).toList(),
        },
      };
}

final class RichPollResultCount {
  const RichPollResultCount({required this.id, required this.count});

  final int id;
  final int count;

  RichJson toJson() => <String, Object?>{'id': id, 'count': count};
}

/// Strict, authority-authenticated projection for Discord message type 46.
///
/// E2EE source labels never enter this projection. They are merged only by
/// [withVerifiedPoll] after the caller proves the referenced poll was locally
/// decrypted and authenticated.
final class RichPollResultMessage {
  const RichPollResultMessage({
    required this.pollMessageRef,
    required this.sourceEncryptionMode,
    required this.answerCounts,
    required this.totalVotes,
    required this.victorAnswerId,
    required this.victorAnswerVotes,
    this.questionText,
    this.victorAnswerText,
    this.victorAnswerEmoji,
  });

  factory RichPollResultMessage.fromMessageJson(RichJson json) {
    Never invalid(String reason) =>
        throw FormatException('Invalid poll result message: $reason');
    final projection =
        _strictRichObject(json['poll_result']) ?? invalid('missing projection');
    if (!_hasExactRichKeys(projection, const <String>{
          'version',
          'poll_message_ref',
          'source_encryption_mode',
          'answer_counts',
          'total_votes',
          'victor_answer_id',
          'victor_answer_votes',
        }) ||
        projection['version'] != 1) {
      invalid('projection shape');
    }
    final rawRef = projection['poll_message_ref'];
    if (rawRef is! String) invalid('source reference');
    late final EntityRef source;
    try {
      source = EntityRef.parse(rawRef);
    } on FormatException {
      invalid('source reference');
    }
    if (source.wire != rawRef) invalid('noncanonical source reference');
    final referenceId = json['referenced_message_id'];
    final referenceDomain = json['referenced_message_domain'];
    if (referenceId is! String || referenceDomain is! String) {
      invalid('missing source reference');
    }
    late final EntityRef directSource;
    try {
      directSource = EntityRef(Snowflake(referenceId), Domain(referenceDomain));
    } on FormatException {
      invalid('source reference');
    }
    if (directSource != source ||
        directSource.wire != '$referenceId@$referenceDomain') {
      invalid('inconsistent source reference');
    }
    final sourceMode = projection['source_encryption_mode'];
    if (sourceMode != 'plaintext' && sourceMode != 'e2ee') {
      invalid('source encryption mode');
    }
    final rawCounts = projection['answer_counts'];
    if (rawCounts is! List || rawCounts.isEmpty || rawCounts.length > 10) {
      invalid('answer counts');
    }
    final counts = <RichPollResultCount>[];
    for (final raw in rawCounts) {
      final item = _strictRichObject(raw);
      if (item == null ||
          !_hasExactRichKeys(item, const <String>{'id', 'count'})) {
        invalid('answer count');
      }
      final id = _strictNonnegativeInt(item['id']);
      final count = _strictNonnegativeInt(item['count']);
      if (id == null || id < 1 || id > 10 || count == null) {
        invalid('answer count range');
      }
      counts.add(RichPollResultCount(id: id, count: count));
    }
    final identifiers = counts.map((item) => item.id).toList(growable: false);
    for (var index = 1; index < identifiers.length; index += 1) {
      if (identifiers[index] <= identifiers[index - 1]) {
        invalid('answer order');
      }
    }
    final total = _strictNonnegativeInt(projection['total_votes']);
    final victorVotes =
        _strictNonnegativeInt(projection['victor_answer_votes']);
    final rawVictor = projection['victor_answer_id'];
    final victorId =
        rawVictor == null ? null : _strictNonnegativeInt(rawVictor);
    if (total == null ||
        victorVotes == null ||
        (rawVictor != null &&
            (victorId == null || victorId < 1 || victorId > 10))) {
      invalid('vote totals');
    }
    final highest = counts.map((item) => item.count).reduce(
          (left, right) => left > right ? left : right,
        );
    final winners = counts
        .where((item) => item.count == highest)
        .map((item) => item.id)
        .toList(growable: false);
    final expectedVictor =
        highest > 0 && winners.length == 1 ? winners.single : null;
    if (counts.fold<int>(0, (sum, item) => sum + item.count) != total ||
        victorVotes != highest ||
        victorId != expectedVictor) {
      invalid('inconsistent vote totals');
    }
    bool absentOrEmptyList(Object? value) =>
        value == null || value is List && value.isEmpty;
    if (json['message_type'] != 46 ||
        json['content'] != null ||
        json['e2ee'] != null ||
        !absentOrEmptyList(json['attachments']) ||
        !absentOrEmptyList(json['components']) ||
        !absentOrEmptyList(json['sticker_items']) ||
        json['poll'] != null ||
        (json['flags'] ?? 0) != 0 ||
        (json['tts'] ?? false) != false) {
      invalid('noncanonical body');
    }
    final rawEmbeds = json['embeds'];
    if (rawEmbeds is! List || rawEmbeds.length != 1) {
      invalid('missing result embed');
    }
    final embed = _strictRichObject(rawEmbeds.single);
    if (embed == null ||
        !_hasExactRichKeys(embed, const <String>{'type', 'fields'}) ||
        embed['type'] != 'poll_result') {
      invalid('result embed shape');
    }
    final rawFields = embed['fields'];
    if (rawFields is! List || rawFields.length < 2 || rawFields.length > 8) {
      invalid('result embed fields');
    }
    const allowed = <String>{
      'poll_question_text',
      'victor_answer_votes',
      'total_votes',
      'victor_answer_id',
      'victor_answer_text',
      'victor_answer_emoji_id',
      'victor_answer_emoji_name',
      'victor_answer_emoji_animated',
    };
    const private = <String>{
      'poll_question_text',
      'victor_answer_text',
      'victor_answer_emoji_id',
      'victor_answer_emoji_name',
      'victor_answer_emoji_animated',
    };
    final fields = <String, String>{};
    for (final raw in rawFields) {
      final field = _strictRichObject(raw);
      if (field == null ||
          !_hasExactRichKeys(
              field, const <String>{'name', 'value', 'inline'})) {
        invalid('result embed field');
      }
      final name = field['name'];
      final value = field['value'];
      if (name is! String ||
          !allowed.contains(name) ||
          fields.containsKey(name) ||
          value is! String ||
          value.isEmpty ||
          value.length > 1024 ||
          field['inline'] != false) {
        invalid('result embed field');
      }
      fields[name] = value;
    }
    if (fields['victor_answer_votes'] != '$victorVotes' ||
        fields['total_votes'] != '$total' ||
        fields['victor_answer_id'] != (victorId == null ? null : '$victorId') ||
        (victorId == null &&
            private
                .where((name) => name != 'poll_question_text')
                .any(fields.containsKey)) ||
        (fields.containsKey('victor_answer_emoji_animated') &&
            fields['victor_answer_emoji_animated'] != 'true' &&
            fields['victor_answer_emoji_animated'] != 'false') ||
        (sourceMode == 'e2ee' && private.any(fields.containsKey))) {
      invalid('result embed does not match projection');
    }
    RichEmoji? emoji;
    if (fields.containsKey('victor_answer_emoji_id') ||
        fields.containsKey('victor_answer_emoji_name')) {
      emoji = RichEmoji.fromJson(<String, Object?>{
        'id': fields['victor_answer_emoji_id'],
        'name': fields['victor_answer_emoji_name'],
        'animated': fields['victor_answer_emoji_animated'] == 'true',
      });
    }
    return RichPollResultMessage(
      pollMessageRef: source,
      sourceEncryptionMode: sourceMode as String,
      answerCounts: List<RichPollResultCount>.unmodifiable(counts),
      totalVotes: total,
      victorAnswerId: victorId,
      victorAnswerVotes: victorVotes,
      questionText: fields['poll_question_text'],
      victorAnswerText: fields['victor_answer_text'],
      victorAnswerEmoji: emoji,
    );
  }

  final EntityRef pollMessageRef;
  final String sourceEncryptionMode;
  final List<RichPollResultCount> answerCounts;
  final int totalVotes;
  final int? victorAnswerId;
  final int victorAnswerVotes;
  final String? questionText;
  final String? victorAnswerText;
  final RichEmoji? victorAnswerEmoji;

  RichPollResultMessage withVerifiedPoll(RichPoll poll) {
    if (sourceEncryptionMode != 'e2ee') return this;
    final pollIds = poll.answers.map((answer) => answer.id).toList()..sort();
    final resultIds = answerCounts.map((answer) => answer.id).toList();
    if (pollIds.length != resultIds.length) return this;
    for (var index = 0; index < pollIds.length; index += 1) {
      if (pollIds[index] != resultIds[index]) return this;
    }
    RichPollAnswer? winner;
    for (final answer in poll.answers) {
      if (answer.id == victorAnswerId) {
        winner = answer;
        break;
      }
    }
    return RichPollResultMessage(
      pollMessageRef: pollMessageRef,
      sourceEncryptionMode: sourceEncryptionMode,
      answerCounts: answerCounts,
      totalVotes: totalVotes,
      victorAnswerId: victorAnswerId,
      victorAnswerVotes: victorAnswerVotes,
      questionText: poll.question.text,
      victorAnswerText: winner?.media.text,
      victorAnswerEmoji: winner?.media.emoji,
    );
  }

  RichJson toJson() => <String, Object?>{
        'version': 1,
        'poll_message_ref': pollMessageRef.wire,
        'source_encryption_mode': sourceEncryptionMode,
        'answer_counts': answerCounts.map((item) => item.toJson()).toList(),
        'total_votes': totalVotes,
        'victor_answer_id': victorAnswerId,
        'victor_answer_votes': victorAnswerVotes,
      };
}

RichJson? _strictRichObject(Object? value) {
  if (value is! Map || value.keys.any((key) => key is! String)) return null;
  return Map<String, Object?>.from(value);
}

bool _hasExactRichKeys(RichJson value, Set<String> expected) =>
    value.length == expected.length && value.keys.toSet().containsAll(expected);

int? _strictNonnegativeInt(Object? value) =>
    value is int && value >= 0 ? value : null;

/// A poll answer before it has been assigned a server-side answer ID.
///
/// Keeping the Discord-compatible wire limits here means every Mobile entry
/// point submits the same valid shape, rather than relying on a form widget to
/// be the only validation boundary.
final class RichPollDraftAnswer {
  factory RichPollDraftAnswer({String? text, RichEmoji? emoji}) {
    final normalizedText = text?.trim();
    if ((normalizedText == null || normalizedText.isEmpty) && emoji == null) {
      throw ArgumentError('A poll answer needs text or an emoji.');
    }
    if (normalizedText != null && normalizedText.length > 55) {
      throw ArgumentError('Poll answers can contain at most 55 characters.');
    }
    return RichPollDraftAnswer._(
      text: normalizedText?.isEmpty == true ? null : normalizedText,
      emoji: emoji,
    );
  }

  const RichPollDraftAnswer._({this.text, this.emoji});

  final String? text;
  final RichEmoji? emoji;

  RichJson toJson() => <String, Object?>{
        'poll_media': <String, Object?>{
          if (text != null) 'text': text,
          if (emoji != null) 'emoji': emoji!.toJson(),
        },
      };
}

/// User-authored poll data using Discord's default poll layout.
final class RichPollDraft {
  factory RichPollDraft({
    required String question,
    required List<RichPollDraftAnswer> answers,
    required int durationHours,
    bool allowMultiselect = false,
  }) {
    final normalizedQuestion = question.trim();
    if (normalizedQuestion.isEmpty) {
      throw ArgumentError('Enter a poll question.');
    }
    if (normalizedQuestion.length > 300) {
      throw ArgumentError('Poll questions can contain at most 300 characters.');
    }
    if (answers.length < 2 || answers.length > 10) {
      throw ArgumentError('A poll needs between 2 and 10 answers.');
    }
    if (durationHours < 1 || durationHours > 768) {
      throw ArgumentError('Poll duration must be between 1 and 768 hours.');
    }
    return RichPollDraft._(
      question: normalizedQuestion,
      answers: List.unmodifiable(answers),
      durationHours: durationHours,
      allowMultiselect: allowMultiselect,
    );
  }

  const RichPollDraft._({
    required this.question,
    required this.answers,
    required this.durationHours,
    required this.allowMultiselect,
  });

  final String question;
  final List<RichPollDraftAnswer> answers;
  final int durationHours;
  final bool allowMultiselect;

  RichJson toJson() => <String, Object?>{
        'question': <String, Object?>{'text': question},
        'answers': answers.map((answer) => answer.toJson()).toList(),
        'duration': durationHours,
        'allow_multiselect': allowMultiselect,
        'layout_type': 1,
      };
}

/// Converts the value returned by Mobile's shared emoji picker into poll
/// media. Custom emoji retain their federated reference; Unicode stays in the
/// name field exactly as Discord-compatible poll payloads expect.
RichEmoji richPollEmojiFromComposerValue(String value) {
  final normalized = value.trim();
  final custom =
      RegExp(r'^<(a?):([A-Za-z0-9_]{2,32}):([^>]+)>$').firstMatch(normalized);
  if (custom != null) {
    return RichEmoji(
      ref: EntityRef.parse(custom.group(3)!),
      name: custom.group(2),
      animated: custom.group(1) == 'a',
    );
  }
  if (normalized.isEmpty || normalized.length > 64) {
    throw ArgumentError('Choose one valid emoji.');
  }
  return RichEmoji(name: normalized);
}

final class InteractionModal {
  const InteractionModal(
      {required this.title, required this.customId, required this.rows});

  factory InteractionModal.fromJson(RichJson json) => InteractionModal(
        title: _text(json['title']) ?? 'Bot form',
        customId: _text(json['custom_id']) ?? '',
        rows: _objects(json['components'])
            .map(RichActionRow.fromJson)
            .toList(growable: false),
      );

  final String title;
  final String customId;
  final List<RichActionRow> rows;

  RichJson toJson() => <String, Object?>{
        'title': title,
        'custom_id': customId,
        'components': rows.map((item) => item.toJson()).toList(),
      };
}

const _interactionResponseWireFields = <String>{
  'application_ref',
  'authority_domain',
  'autocomplete_generation',
  'callback_type',
  'channel_ref',
  'data',
  'deleted_at',
  'ephemeral',
  'expires_at',
  'interaction_id',
  'interaction_ref',
  'invoker_ref',
  'message_ref',
  'operation',
  'response_grant_id',
  'response_id',
  'response_ref',
  'revision',
  'sequence',
  'user_ref',
};

bool _interactionResponseTimestamp(String value) =>
    RegExp(r'(?:Z|[+-][0-9]{2}:[0-9]{2})$').hasMatch(value) &&
    DateTime.tryParse(value) != null;

final class MobileInteractionResponse {
  const MobileInteractionResponse({
    required this.interactionRef,
    required this.responseRef,
    required this.invokerRef,
    required this.channelRef,
    required this.applicationRef,
    required this.responseGrantId,
    required this.revision,
    required this.operation,
    required this.expiresAt,
    this.sequence = 0,
    required this.callbackType,
    this.ephemeral = false,
    this.data = const <String, Object?>{},
    this.messageRef,
    this.autocompleteGeneration,
    this.deletedAt,
    this.decryptionUnavailable = false,
  });

  factory MobileInteractionResponse.fromJson(
    RichJson json, {
    bool allowClientState = false,
  }) {
    final clientUnavailable =
        allowClientState && json['decryption_unavailable'] == true;
    if (json.length !=
            _interactionResponseWireFields.length +
                (clientUnavailable ? 1 : 0) ||
        !json.keys.toSet().containsAll(_interactionResponseWireFields) ||
        (clientUnavailable
            ? !json.containsKey('decryption_unavailable')
            : json.containsKey('decryption_unavailable'))) {
      throw FormatException('Invalid interaction response fields', json);
    }
    final rawAuthority = json['authority_domain'];
    final rawInteractionRef = json['interaction_ref'];
    final rawResponseRef = json['response_ref'];
    final rawInvokerRef = json['invoker_ref'];
    final rawUserRef = json['user_ref'];
    final rawChannelRef = json['channel_ref'];
    final rawApplicationRef = json['application_ref'];
    final rawResponseGrantId = json['response_grant_id'];
    if (rawAuthority is! String ||
        rawInteractionRef is! String ||
        rawResponseRef is! String ||
        rawInvokerRef is! String ||
        rawUserRef is! String ||
        rawChannelRef is! String ||
        rawApplicationRef is! String ||
        rawResponseGrantId is! String) {
      throw FormatException('Invalid interaction response identity', json);
    }
    final authority = Domain(rawAuthority);
    final interactionRef = EntityRef.parse(rawInteractionRef);
    final responseRef = EntityRef.parse(rawResponseRef);
    final invokerRef = EntityRef.parse(rawInvokerRef);
    final userRef = EntityRef.parse(rawUserRef);
    final channelRef = EntityRef.parse(rawChannelRef);
    final applicationRef = EntityRef.parse(rawApplicationRef);
    final rawMessageRef = json['message_ref'];
    EntityRef? messageRef;
    if (rawMessageRef != null) {
      if (rawMessageRef is! String) {
        throw FormatException('Invalid interaction response message', json);
      }
      messageRef = EntityRef.parse(rawMessageRef);
    }
    final interactionId = _text(json['interaction_id']);
    final responseId = _text(json['response_id']);
    final revisionText = _text(json['revision']);
    final operation = _text(json['operation']);
    final rawExpiresAt = _text(json['expires_at']);
    final rawDeletedAt = _text(json['deleted_at']);
    final expiresAt = DateTime.tryParse(rawExpiresAt ?? '')?.toUtc();
    final deletedAt = DateTime.tryParse(rawDeletedAt ?? '')?.toUtc();
    final rawAutocompleteGeneration = json['autocomplete_generation'];
    final autocompleteText =
        rawAutocompleteGeneration is String ? rawAutocompleteGeneration : null;
    final validAutocomplete = autocompleteText != null &&
        RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(autocompleteText) &&
        BigInt.parse(autocompleteText) <= BigInt.parse('9223372036854775807');
    final rawData = json['data'];
    final create = operation == 'CREATE';
    final remove = operation == 'DELETE';
    if (rawAuthority != authority.value ||
        rawInteractionRef != interactionRef.wire ||
        rawResponseRef != responseRef.wire ||
        rawInvokerRef != invokerRef.wire ||
        rawUserRef != userRef.wire ||
        rawChannelRef != channelRef.wire ||
        rawApplicationRef != applicationRef.wire ||
        interactionRef.domain != authority ||
        responseRef.domain != authority ||
        channelRef.domain != authority ||
        invokerRef != userRef ||
        interactionId != interactionRef.id.value ||
        responseId != responseRef.id.value ||
        revisionText == null ||
        !RegExp(r'^[1-9][0-9]{0,18}$').hasMatch(revisionText) ||
        BigInt.parse(revisionText) > BigInt.parse('9223372036854775807') ||
        !const <String>{'CREATE', 'UPDATE', 'DELETE'}.contains(operation) ||
        (create
            ? revisionText != '1'
            : BigInt.parse(revisionText) <= BigInt.one) ||
        expiresAt == null ||
        rawExpiresAt == null ||
        !_interactionResponseTimestamp(rawExpiresAt) ||
        json['sequence'] is! int ||
        (json['sequence']! as int) < 0 ||
        (json['sequence']! as int) > 9223372036854775807 ||
        json['callback_type'] is! int ||
        !const <int>{4, 7, 8, 9}.contains(json['callback_type']) ||
        json['ephemeral'] is! bool ||
        rawData is! Map ||
        rawData.keys.any((key) => key is! String) ||
        !isCanonicalBase64url32(rawResponseGrantId) ||
        (messageRef != null &&
            (messageRef.wire != rawMessageRef ||
                messageRef.domain != authority)) ||
        (json['ephemeral'] == true && messageRef != null) ||
        (const <int>{8, 9}.contains(json['callback_type']) &&
            messageRef != null) ||
        (json['callback_type'] == 8
            ? !validAutocomplete
            : rawAutocompleteGeneration != null) ||
        (remove
            ? rawDeletedAt == null ||
                !_interactionResponseTimestamp(rawDeletedAt) ||
                deletedAt == null ||
                rawData.isNotEmpty
            : rawDeletedAt != null)) {
      throw FormatException('Invalid interaction response identity', json);
    }
    return MobileInteractionResponse(
      interactionRef: interactionRef,
      responseRef: responseRef,
      invokerRef: invokerRef,
      channelRef: channelRef,
      applicationRef: applicationRef,
      responseGrantId: rawResponseGrantId,
      revision: BigInt.parse(revisionText),
      operation: operation!,
      expiresAt: expiresAt,
      sequence: json['sequence']! as int,
      callbackType: json['callback_type']! as int,
      ephemeral: json['ephemeral']! as bool,
      data:
          Map<String, Object?>.unmodifiable(Map<String, Object?>.from(rawData)),
      messageRef: messageRef,
      autocompleteGeneration:
          validAutocomplete ? BigInt.parse(autocompleteText).toInt() : null,
      deletedAt: deletedAt,
      decryptionUnavailable: clientUnavailable,
    );
  }

  final EntityRef interactionRef;
  final EntityRef responseRef;
  final EntityRef invokerRef;
  final EntityRef channelRef;
  final EntityRef applicationRef;
  final String responseGrantId;
  final BigInt revision;
  final String operation;
  final DateTime expiresAt;
  String get interactionId => interactionRef.id.value;
  String get responseId => responseRef.id.value;
  final int sequence;
  final int callbackType;
  final bool ephemeral;
  final RichJson data;
  final EntityRef? messageRef;
  final int? autocompleteGeneration;
  final DateTime? deletedAt;
  final bool decryptionUnavailable;

  String get storageKey => responseRef.wire;

  bool get hasMessageContent =>
      data['content'] is String && (data['content']! as String).isNotEmpty ||
      data['embeds'] is List && (data['embeds']! as List).isNotEmpty ||
      data['components'] is List && (data['components']! as List).isNotEmpty ||
      data['attachments'] is List &&
          (data['attachments']! as List).isNotEmpty ||
      data['poll'] is Map;

  InteractionModal? get modal => callbackType == 9 && deletedAt == null
      ? InteractionModal.fromJson(data)
      : null;
}

Map<String, MobileInteractionResponse> applyMobileInteractionResponseEvent(
  Map<String, MobileInteractionResponse> current,
  String eventName,
  RichJson data, {
  bool allowClientState = false,
}) {
  final responses = Map<String, MobileInteractionResponse>.of(current);
  if (eventName == 'INTERACTION_RESPONSE_CREATE' ||
      eventName == 'INTERACTION_RESPONSE_UPDATE' ||
      eventName == 'INTERACTION_RESPONSE_DELETE') {
    MobileInteractionResponse response;
    try {
      response = MobileInteractionResponse.fromJson(
        data,
        allowClientState: allowClientState,
      );
    } on FormatException {
      return Map.unmodifiable(responses);
    }
    final expectedOperation =
        eventName.substring('INTERACTION_RESPONSE_'.length);
    if (response.operation != expectedOperation || response.sequence < 0) {
      return Map.unmodifiable(responses);
    }
    final previous = responses[response.storageKey];
    if (previous != null &&
        (response.revision <= previous.revision ||
            previous.deletedAt != null ||
            response.interactionRef != previous.interactionRef ||
            response.expiresAt != previous.expiresAt)) {
      return Map.unmodifiable(responses);
    }
    if (eventName == 'INTERACTION_RESPONSE_DELETE') {
      if (response.deletedAt == null || response.data.isNotEmpty) {
        return Map.unmodifiable(responses);
      }
      responses[response.storageKey] = response;
      return Map.unmodifiable(responses);
    }
    if (response.deletedAt != null) {
      return Map.unmodifiable(responses);
    } else {
      responses[response.storageKey] = response;
    }
    return Map.unmodifiable(responses);
  }
  return Map.unmodifiable(responses);
}
