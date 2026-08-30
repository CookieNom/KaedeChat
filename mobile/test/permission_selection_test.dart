import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/domain/permission_selection.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';

void main() {
  test('named permission selection preserves unknown high bits', () {
    final administrator = permissionMetadata.singleWhere(
      (item) => item.name == 'ADMINISTRATOR',
    );
    final futureBit = BigInt.one << 62;

    final selected = setApplicationPermission(
      futureBit,
      administrator,
      true,
    );
    expect(selected, futureBit | BigInt.from(administrator.bit));
    expect(applicationPermissionSelected(selected, administrator), isTrue);
    expect(
      setApplicationPermission(selected, administrator, false),
      futureBit,
    );
  });

  test('permission masks project readable install metadata', () {
    expect(
      selectedApplicationPermissions('3').map((item) => item.label),
      ['Create invites', 'Kick members'],
    );
    expect(
      () => applicationPermissionMask('-1'),
      throwsA(isA<FormatException>()),
    );
    expect(
      () => applicationPermissionMask('1.5'),
      throwsA(isA<FormatException>()),
    );
  });
}
