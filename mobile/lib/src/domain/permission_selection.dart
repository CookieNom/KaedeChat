import 'package:kaede_mobile/src/protocol/generated.dart';

BigInt applicationPermissionMask(String value) {
  final normalized = value.trim();
  if (!RegExp(r'^\d+$').hasMatch(normalized)) {
    throw const FormatException(
      'Permissions must be a non-negative whole number.',
    );
  }
  return BigInt.parse(normalized);
}

bool applicationPermissionSelected(BigInt value, PermissionMetadata item) =>
    value & BigInt.from(item.bit) != BigInt.zero;

BigInt setApplicationPermission(
  BigInt value,
  PermissionMetadata item,
  bool selected,
) {
  final bit = BigInt.from(item.bit);
  return selected ? value | bit : value & ~bit;
}

List<PermissionMetadata> get applicationInstallPermissions => permissionMetadata
    .where((item) =>
        item.resourceScopes.contains('guild') ||
        item.resourceScopes.contains('channel'))
    .toList(growable: false);

List<PermissionMetadata> selectedApplicationPermissions(String value) {
  final mask = applicationPermissionMask(value);
  return applicationInstallPermissions
      .where((item) => applicationPermissionSelected(mask, item))
      .toList(growable: false);
}
