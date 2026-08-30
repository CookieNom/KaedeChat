const _clientOnlyNetworkFields = <String>{
  'e2ee_verified',
  'decrypted_content',
  'decrypted_attachments',
  'decrypted_allowed_mentions',
  'decrypted_forward_snapshot',
  'encrypted_manifest',
};

/// Decode a nested network object array without silently dropping hostile
/// scalar children. Optional absent arrays remain empty for rolling upgrades.
List<Map<String, Object?>> strictNetworkObjectList(
  Object? value, {
  String label = 'network object array',
}) {
  if (value == null) return const <Map<String, Object?>>[];
  if (value is! List) throw FormatException('$label must be an array.');
  final result = <Map<String, Object?>>[];
  for (final item in value) {
    if (item is! Map || item.keys.any((key) => key is! String)) {
      throw FormatException('$label contains an invalid child.');
    }
    result.add(Map<String, Object?>.from(item));
  }
  return List<Map<String, Object?>>.unmodifiable(result);
}

/// Removes fields that can only be produced by authenticated local decryption.
/// Neither the signed-in authority nor a federated peer may project this state.
Object? stripNetworkClientState(Object? value) {
  if (value is List) {
    return value.map(stripNetworkClientState).toList(growable: false);
  }
  if (value is! Map) return value;
  final result = <String, Object?>{};
  for (final entry in value.entries) {
    final key = entry.key;
    if (key is! String) {
      throw const FormatException('JSON object key is invalid');
    }
    if (_clientOnlyNetworkFields.contains(key)) continue;
    result[key] = stripNetworkClientState(entry.value);
  }
  return result;
}
