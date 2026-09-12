import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

/// Prevents system gestures from popping any preserved route while locked.
final class SessionLockObserver extends NavigatorObserver
    implements PopEntry<Object?> {
  final _routes = <Route<dynamic>>{};

  // Sign-out must discard editors immediately, without an exit animation
  // exposing their contents over the authentication screen.
  void discardTransientRoutes() {
    for (final route in _routes.toList().reversed) {
      if (route.settings is! Page && route.isActive) {
        navigator?.removeRoute(route);
      }
    }
  }

  @override
  final ValueNotifier<bool> canPopNotifier = ValueNotifier(true);

  @override
  void onPopInvoked(bool didPop) {}

  @override
  void onPopInvokedWithResult(bool didPop, Object? result) {}

  @override
  void didPush(Route<dynamic> route, Route<dynamic>? previousRoute) {
    _routes.add(route);
    if (route is ModalRoute) route.registerPopEntry(this);
  }

  @override
  void didPop(Route<dynamic> route, Route<dynamic>? previousRoute) {
    _routes.remove(route);
    if (route is ModalRoute) route.unregisterPopEntry(this);
  }

  @override
  void didRemove(Route<dynamic> route, Route<dynamic>? previousRoute) {
    _routes.remove(route);
    if (route is ModalRoute) route.unregisterPopEntry(this);
  }

  @override
  void didReplace({Route<dynamic>? newRoute, Route<dynamic>? oldRoute}) {
    _routes.remove(oldRoute);
    if (newRoute != null) _routes.add(newRoute);
    if (oldRoute is ModalRoute) oldRoute.unregisterPopEntry(this);
    if (newRoute is ModalRoute) newRoute.registerPopEntry(this);
  }
}

/// Lives above the app navigator so dialogs are hidden along with their pages.
final class SessionLock extends StatefulWidget {
  const SessionLock({
    super.key,
    required this.locked,
    required this.lockScreen,
    required this.observer,
    required this.child,
  });

  final bool locked;
  final Widget lockScreen;
  final SessionLockObserver observer;
  final Widget child;

  @override
  State<SessionLock> createState() => _SessionLockState();
}

final class _SessionLockState extends State<SessionLock>
    with WidgetsBindingObserver {
  @override
  void initState() {
    super.initState();
    // Register before the child Router's back dispatcher.
    WidgetsBinding.instance.addObserver(this);
    widget.observer.canPopNotifier.value = !widget.locked;
  }

  @override
  void didUpdateWidget(covariant SessionLock oldWidget) {
    super.didUpdateWidget(oldWidget);
    widget.observer.canPopNotifier.value = !widget.locked;
    if (widget.locked && !oldWidget.locked) {
      FocusManager.instance.primaryFocus?.unfocus();
    }
  }

  @override
  Future<bool> didPopRoute() async => widget.locked;

  @override
  bool handleStartBackGesture(PredictiveBackEvent backEvent) => widget.locked;

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Stack(
        fit: StackFit.expand,
        children: [
          Offstage(
            offstage: widget.locked,
            child: ExcludeFocus(
              excluding: widget.locked,
              child: TickerMode(enabled: !widget.locked, child: widget.child),
            ),
          ),
          if (widget.locked) widget.lockScreen,
        ],
      );
}
