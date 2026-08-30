import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:kaede_mobile/src/app.dart';
import 'package:kaede_mobile/src/app/providers.dart';
import 'package:kaede_mobile/src/core/errors.dart';
import 'package:kaede_mobile/src/platform/push_service.dart';
import 'package:kaede_mobile/src/storage/local_database.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  LicenseRegistry.addLicense(() async* {
    yield LicenseEntryWithLineBreaks(
      const <String>['Inter'],
      await rootBundle.loadString('assets/fonts/LICENSE-Inter.txt'),
    );
  });
  try {
    final bootstrap = await Future.wait<Object?>([
      LocalDatabase.open(),
      PushService.create(),
    ]);
    final database = bootstrap[0]! as LocalDatabase;
    final pushService = bootstrap[1]! as PushService;
    runApp(
      ProviderScope(
        overrides: [
          localDatabaseProvider.overrideWithValue(database),
          pushServiceProvider.overrideWithValue(pushService),
        ],
        child: const KaedeApp(),
      ),
    );
  } on Object catch (error, stackTrace) {
    FlutterError.reportError(
      FlutterErrorDetails(
        exception: error,
        stack: stackTrace,
        library: 'Kaede bootstrap',
      ),
    );
    runApp(_BootstrapFailure(error: error));
  }
}

final class _BootstrapFailure extends StatelessWidget {
  const _BootstrapFailure({required this.error});

  final Object error;

  @override
  Widget build(BuildContext context) => MaterialApp(
        debugShowCheckedModeBanner: false,
        theme: kaedeTheme(),
        home: Scaffold(
          body: SafeArea(
            child: Center(
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 420),
                child: Padding(
                  padding: const EdgeInsets.all(24),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const Icon(Icons.error_outline, size: 48),
                      const SizedBox(height: 16),
                      const Text(
                        'Kaede could not start',
                        style: TextStyle(
                          fontSize: 24,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                      const SizedBox(height: 12),
                      const Text(
                        'Close and reopen the app. If this continues, share '
                        'the diagnostic below with your instance operator.',
                        textAlign: TextAlign.center,
                      ),
                      const SizedBox(height: 16),
                      SelectableText(
                        userFacingError(error),
                        textAlign: TextAlign.center,
                        style: TextStyle(
                          color: Theme.of(context).colorScheme.onSurfaceVariant,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      );
}
