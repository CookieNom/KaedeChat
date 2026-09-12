import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:kaede_mobile/src/features/auth/session_lock.dart';

void main() {
  testWidgets('lock preserves pages and dialogs while blocking access and back',
      (tester) async {
    final locked = ValueNotifier(false);
    final observer = SessionLockObserver();
    final router = GoRouter(
      observers: [observer],
      routes: [
        GoRoute(path: '/', builder: (_, __) => const _DraftEditor()),
      ],
    );
    addTearDown(router.dispose);
    addTearDown(locked.dispose);
    addTearDown(observer.canPopNotifier.dispose);
    final semantics = tester.ensureSemantics();

    await tester.pumpWidget(MaterialApp.router(
      routerConfig: router,
      theme: ThemeData(
        pageTransitionsTheme: const PageTransitionsTheme(builders: {
          TargetPlatform.android: PredictiveBackPageTransitionsBuilder(),
        }),
      ),
      builder: (context, child) => ValueListenableBuilder<bool>(
        valueListenable: locked,
        builder: (context, value, _) => SessionLock(
          observer: observer,
          locked: value,
          lockScreen: const Scaffold(body: Text('Locked')),
          child: child!,
        ),
      ),
    ));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'Message draft');
    final navigator = router.routerDelegate.navigatorKey.currentState!;
    navigator.push(MaterialPageRoute<void>(
      builder: (_) => const _DraftEditor(),
    ));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'Page draft');
    showModalBottomSheet<void>(
      context: tester.element(find.byType(TextField)),
      isScrollControlled: true,
      builder: (_) => const _DraftEditor(),
    );
    await tester.pumpAndSettle();
    await tester.enterText(
        find.byType(TextField).last, 'Private unsaved draft');
    await tester.pump();
    final editorState = tester.state(find.byType(_DraftEditor).last);
    final textPosition = tester.getCenter(find.byType(TextField).last);
    final route = ModalRoute.of(tester.element(find.byType(TextField).last))!;
    expect(tester.testTextInput.isVisible, isTrue);
    expect(find.bySemanticsLabel('Private editor'), findsWidgets);

    // A failed/cancelled authentication leaves this boundary locked. Repeat
    // the cycle to catch accidental disposal or navigation changes on unlock.
    for (var cycle = 0; cycle < 2; cycle++) {
      locked.value = true;
      await tester.pumpAndSettle();
      expect(find.text('Locked'), findsOneWidget);
      expect(find.byType(TextField), findsNothing);
      expect(find.bySemanticsLabel('Private editor'), findsNothing);
      expect(tester.testTextInput.isVisible, isFalse);
      expect(route.popDisposition, RoutePopDisposition.doNotPop);
      await tester.tapAt(textPosition);
      await tester.sendKeyEvent(LogicalKeyboardKey.keyX);
      await tester.binding.handlePopRoute();
      await navigator.maybePop();
      await tester.pumpAndSettle();
      expect(route.isActive, isTrue);
      expect(find.text('Locked'), findsOneWidget);

      locked.value = false;
      await tester.pumpAndSettle();
      expect(tester.state(find.byType(_DraftEditor).last), same(editorState));
      expect(find.text('Private unsaved draft'), findsOneWidget);
      expect(route.popDisposition, RoutePopDisposition.pop);
    }

    await tester.binding.handlePopRoute();
    await tester.pumpAndSettle();
    expect(find.text('Page draft'), findsOneWidget);
    await tester.binding.handlePopRoute();
    await tester.pumpAndSettle();
    expect(find.text('Message draft'), findsOneWidget);

    // Sign-out discards preserved routes immediately, even while locked.
    navigator.push(MaterialPageRoute<void>(
      builder: (_) => const _DraftEditor(),
    ));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'Discard on sign-out');
    final signOutRoute = ModalRoute.of(tester.element(find.byType(TextField)))!;
    locked.value = true;
    await tester.pumpAndSettle();
    for (final method in const [
      MethodCall('startBackGesture', {
        'touchOffset': [5.0, 300.0],
        'progress': 0.0,
        'swipeEdge': 0,
      }),
      MethodCall('commitBackGesture'),
    ]) {
      await tester.binding.defaultBinaryMessenger.handlePlatformMessage(
        'flutter/backgesture',
        const StandardMethodCodec().encodeMethodCall(method),
        (_) {},
      );
    }
    await tester.pump();
    expect(
        find.text('Discard on sign-out', skipOffstage: false), findsOneWidget);
    expect(signOutRoute.isCurrent, isTrue);
    observer.discardTransientRoutes();
    locked.value = false;
    await tester.pump();
    expect(find.text('Discard on sign-out', skipOffstage: false), findsNothing);
    expect(find.text('Message draft'), findsOneWidget);
    await tester.pumpWidget(const SizedBox.shrink());
    semantics.dispose();
  });
}

class _DraftEditor extends StatefulWidget {
  const _DraftEditor();

  @override
  State<_DraftEditor> createState() => _DraftEditorState();
}

class _DraftEditorState extends State<_DraftEditor> {
  final controller = TextEditingController();

  @override
  void dispose() {
    controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        body: TextField(
          controller: controller,
          decoration: const InputDecoration(labelText: 'Private editor'),
        ),
      );
}
