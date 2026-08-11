import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/gateway/gateway_client.dart';
import 'package:kaede_mobile/src/platform/push_service.dart';
import 'package:kaede_mobile/src/storage/local_database.dart';

final localDatabaseProvider = Provider<LocalDatabase>(
  (_) => throw StateError('LocalDatabase must be initialized before runApp'),
);
final pushServiceProvider = Provider<PushService>(
  (_) => throw StateError('PushService must be initialized before runApp'),
);
final sessionVaultProvider =
    Provider<SessionVault>((_) => const SessionVault());
final apiClientProvider = Provider<KaedeApiClient>(
  (ref) => KaedeApiClient(vault: ref.watch(sessionVaultProvider)),
);
final repositoryProvider = Provider<KaedeRepository>(
  (ref) => KaedeRepository(ref.watch(apiClientProvider)),
);
final gatewayProvider = Provider<GatewayClient>((ref) {
  final api = ref.watch(apiClientProvider);
  final gateway = GatewayClient(
    tokens: () async => api.tokens ?? await api.restore(),
  );
  ref.onDispose(gateway.close);
  return gateway;
});
