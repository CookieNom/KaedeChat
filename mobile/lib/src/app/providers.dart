import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/api/api_client.dart';
import 'package:kaede_mobile/src/api/kaede_repository.dart';
import 'package:kaede_mobile/src/auth/session_vault.dart';
import 'package:kaede_mobile/src/gateway/gateway_client.dart';
import 'package:kaede_mobile/src/l10n/language_controller.dart';
import 'package:kaede_mobile/src/platform/push_service.dart';
import 'package:kaede_mobile/src/storage/local_database.dart';

final localDatabaseProvider = Provider<LocalDatabase>(
  (_) => throw StateError(
      L10n.current.ui_localdatabase_must_be_initialized_before_runa_b6e5733c),
);
final pushServiceProvider = Provider<PushService>(
  (_) => throw StateError(
      L10n.current.ui_pushservice_must_be_initialized_before_runapp_6c5900e1),
);
final sessionVaultProvider =
    Provider<SessionVault>((_) => const SessionVault());
final apiClientProvider = Provider<KaedeApiClient>(
  (ref) => KaedeApiClient(vault: ref.watch(sessionVaultProvider)),
);
final repositoryProvider = Provider<KaedeRepository>((ref) {
  final repository = KaedeRepository(ref.watch(apiClientProvider));
  ref.onDispose(repository.dispose);
  return repository;
});
final gatewayProvider = Provider<GatewayClient>((ref) {
  final api = ref.watch(apiClientProvider);
  final gateway = GatewayClient(
    tokens: () async => api.tokens ?? await api.restore(),
  );
  ref.onDispose(gateway.close);
  return gateway;
});
