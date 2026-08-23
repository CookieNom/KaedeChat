final class Snowflake {
  Snowflake(String value) : value = _validate(value);

  final String value;

  static final _pattern = RegExp(r'^[1-9][0-9]{0,18}$');
  static final _maximum = BigInt.parse('9223372036854775807');

  static String _validate(String value) {
    if (!_pattern.hasMatch(value)) {
      throw FormatException('Invalid snowflake', value);
    }
    final parsed = BigInt.parse(value);
    if (parsed > _maximum) {
      throw FormatException('Snowflake exceeds signed BIGINT', value);
    }
    return value;
  }

  @override
  bool operator ==(Object other) => other is Snowflake && value == other.value;

  @override
  int get hashCode => value.hashCode;

  @override
  String toString() => value;
}

final class Domain {
  Domain(String value) : value = _normalize(value);

  final String value;

  static final _labelPattern =
      RegExp(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$');

  static String _normalize(String input) {
    var value = input.trim().toLowerCase();
    if (value.endsWith('.')) value = value.substring(0, value.length - 1);
    if (value.isEmpty || value.length > 253 || value.contains('..')) {
      throw FormatException('Invalid domain', input);
    }
    if (value.contains('://') ||
        value.contains('/') ||
        value.contains(':') ||
        value.contains('@') ||
        value.contains('?') ||
        value.contains('#')) {
      throw FormatException('Expected a hostname without a URL or port', input);
    }
    final labels = value.split('.');
    if (labels.any((part) => !_labelPattern.hasMatch(part))) {
      throw FormatException('Invalid domain', input);
    }
    return value;
  }

  @override
  bool operator ==(Object other) => other is Domain && value == other.value;

  @override
  int get hashCode => value.hashCode;

  @override
  String toString() => value;
}

final class EntityRef {
  EntityRef(this.id, this.domain) : key = '${id.value}@${domain.value}';

  /// Decodes the two reference shapes used by Kaede's HTTP and gateway APIs.
  ///
  /// Older endpoints serialize a reference as `id@domain`; newer typed
  /// payloads serialize it as `{id, origin_domain}`. Keeping this conversion at
  /// the wire boundary prevents maps from being accidentally stringified into
  /// invalid references by individual models.
  factory EntityRef.fromJson(Object? input, {Domain? localDomain}) {
    if (input is String) {
      return EntityRef.parse(input, localDomain: localDomain);
    }
    if (input is Map) {
      final id = input['id'];
      final domain = input['origin_domain'] ?? input['domain'];
      if (id == null || domain == null) {
        throw FormatException('Invalid entity reference', input);
      }
      return EntityRef(Snowflake('$id'), Domain('$domain'));
    }
    throw FormatException('Invalid entity reference', input);
  }

  factory EntityRef.parse(String input, {Domain? localDomain}) {
    final pieces = input.split('@');
    if (pieces.length == 1 && localDomain != null) {
      return EntityRef(Snowflake(pieces.single), localDomain);
    }
    if (pieces.length != 2) {
      throw FormatException('Invalid entity reference', input);
    }
    return EntityRef(Snowflake(pieces.first), Domain(pieces.last));
  }

  final Snowflake id;
  final Domain domain;

  /// Precomputed so map-key hashing, equality and wire encoding never pay a
  /// string-concatenation cost in hot paths.
  final String key;

  String get wire => key;

  @override
  bool operator ==(Object other) => other is EntityRef && key == other.key;

  @override
  int get hashCode => key.hashCode;

  @override
  String toString() => key;
}
