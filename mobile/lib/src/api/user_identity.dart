import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/core/refs.dart';

final _qualifiedUserRef = RegExp(r'^\d+@[^@\s]+$');

extension UserIdentityRepository on KaedeRepository {
  /// Resolves the two identity forms accepted by people pickers without
  /// duplicating the qualified-ref/handle branch in each settings surface.
  Future<EntityRef> resolveUserIdentity(String input) async {
    final identity = input.trim();
    if (_qualifiedUserRef.hasMatch(identity)) return EntityRef.parse(identity);
    return (await lookupUser(identity)).ref;
  }
}
