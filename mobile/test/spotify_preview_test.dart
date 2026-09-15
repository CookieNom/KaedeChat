import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/features/chat/spotify_preview.dart';
import 'package:webview_flutter/webview_flutter.dart';
// The plugin's public platform interface lets us test native-view lifetimes.
// ignore: depend_on_referenced_packages
import 'package:webview_flutter_platform_interface/webview_flutter_platform_interface.dart';

void main() {
  final uri =
      Uri.parse('https://open.spotify.com/embed/track/11dFghVXANMlKmJXsNCbNl');
  test('only canonical Spotify content becomes a player', () {
    expect(
        spotifyEmbedUri(
            'https://open.spotify.com/intl-de/track/11dFghVXANMlKmJXsNCbNl?si=x&theme=0'),
        uri);
    for (final value in [
      'https://open.spotify.com.evil.test/track/11dFghVXANMlKmJXsNCbNl',
      'https://user@open.spotify.com/track/11dFghVXANMlKmJXsNCbNl',
      'https://open.spotify.com:8443/track/11dFghVXANMlKmJXsNCbNl',
      'http://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl',
      'https://open.spotify.com/track/bad'
    ]) {
      expect(spotifyEmbedUri(value), isNull);
    }
  });

  testWidgets('unloads when swiping away from the conversation page',
      (tester) async {
    final platform = _Platform();
    WebViewPlatform.instance = platform;
    final pages = PageController();
    addTearDown(pages.dispose);
    await tester.pumpWidget(MaterialApp(
        home: Scaffold(
            body: PageView(
      controller: pages,
      children: [
        ListView(children: [SpotifyPreview(uri: uri)]),
        const SizedBox.expand(),
      ],
    ))));
    await tester.pump();
    await tester.pump();
    expect(platform.controllers.single.loads, [uri]);
    pages.jumpToPage(1);
    await tester.pump();
    await tester.pump();
    expect(find.byType(WebViewWidget), findsNothing);
    expect(platform.controllers.single.loads.last, Uri.parse('about:blank'));
    await tester.pumpWidget(const SizedBox());
  });

  for (final width in [320.0, 800.0]) {
    for (final reversed in [false, true]) {
      testWidgets(
          'loads only in viewport, unloads on scroll, keeps geometry at $width (reverse=$reversed)',
          (tester) async {
        tester.view.physicalSize = Size(width, 600);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        final platform = _Platform();
        WebViewPlatform.instance = platform;
        final scroll = ScrollController();
        addTearDown(scroll.dispose);
        await tester.pumpWidget(MaterialApp(
            home: Scaffold(
                body: ListView(
          controller: scroll,
          reverse: reversed,
          // Keep the offscreen widget built to prove cache retention does not load it.
          cacheExtent: 2000,
          children: [
            const SizedBox(height: 650),
            SpotifyPreview(uri: uri),
            const SizedBox(height: 1000)
          ],
        ))));
        await tester.pump();
        expect(platform.controllers, isEmpty);
        final size =
            tester.getSize(find.byType(SpotifyPreview, skipOffstage: false));
        scroll.jumpTo(300);
        await tester.pump();
        await tester.pump();
        await tester.pump();
        expect(platform.controllers, hasLength(1));
        expect(platform.controllers.single.loads, [uri]);
        expect(tester.getSize(find.byType(SpotifyPreview, skipOffstage: false)),
            size);
        expect(tester.getSize(find.byType(WebViewWidget)).height, 152);
        expect(tester.getSize(find.byType(WebViewWidget)).width,
            lessThanOrEqualTo(width));
        expect(
            tester
                .widget<WebViewWidget>(find.byType(WebViewWidget))
                .platform
                .params
                .gestureRecognizers,
            isEmpty);
        // A drag beginning on the player still scrolls the conversation.
        await tester.drag(
            find.byType(WebViewWidget), Offset(0, reversed ? 550 : -550));
        await tester.pump();
        await tester.pump();
        expect(scroll.offset, greaterThan(300));
        expect(find.byType(WebViewWidget), findsNothing);
        expect(
            platform.controllers.single.loads.last, Uri.parse('about:blank'));
        expect(tester.getSize(find.byType(SpotifyPreview, skipOffstage: false)),
            size);
        scroll.jumpTo(300);
        await tester.pump();
        await tester.pump();
        await tester.pump();
        expect(platform.controllers, hasLength(2));
        tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
        await tester.pump();
        await tester.pump();
        expect(platform.controllers.last.loads.last, Uri.parse('about:blank'));
        tester.binding
            .handleAppLifecycleStateChanged(AppLifecycleState.resumed);
        await tester.pump();
        await tester.pump();
        await tester.pump();
        expect(platform.controllers.last.loads.last, uri);
        final navigator = tester.state<NavigatorState>(find.byType(Navigator));
        navigator.push(MaterialPageRoute<void>(
            builder: (_) => const Scaffold(body: Text('Other screen'))));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 350));
        await tester.pump();
        expect(platform.controllers.last.loads.last, Uri.parse('about:blank'));
        await tester.pumpWidget(const SizedBox());
        expect(platform.controllers.last.loads.last, Uri.parse('about:blank'));
        expect(tester.takeException(), isNull);
      });
    }
  }
}

class _Platform extends WebViewPlatform {
  final controllers = <_Controller>[];
  @override
  PlatformWebViewController createPlatformWebViewController(
      PlatformWebViewControllerCreationParams params) {
    final controller = _Controller(params);
    controllers.add(controller);
    return controller;
  }

  @override
  PlatformNavigationDelegate createPlatformNavigationDelegate(
          PlatformNavigationDelegateCreationParams params) =>
      _Delegate(params);
  @override
  PlatformWebViewWidget createPlatformWebViewWidget(
          PlatformWebViewWidgetCreationParams params) =>
      _View(params);
}

class _Controller extends PlatformWebViewController {
  _Controller(super.params) : super.implementation();
  final loads = <Uri>[];
  @override
  Future<void> loadRequest(LoadRequestParams params) async {
    loads.add(params.uri);
  }

  @override
  Future<void> setJavaScriptMode(JavaScriptMode mode) async {}
  @override
  Future<void> setBackgroundColor(Color color) async {}
  @override
  Future<void> setPlatformNavigationDelegate(
      PlatformNavigationDelegate handler) async {}
}

class _Delegate extends PlatformNavigationDelegate {
  _Delegate(super.params) : super.implementation();
  @override
  Future<void> setOnNavigationRequest(
      NavigationRequestCallback callback) async {}
  @override
  Future<void> setOnPageFinished(PageEventCallback callback) async {}
  @override
  Future<void> setOnWebResourceError(WebResourceErrorCallback callback) async {}
}

class _View extends PlatformWebViewWidget {
  _View(super.params) : super.implementation();
  @override
  Widget build(BuildContext context) =>
      const ColoredBox(color: Color(0xffa00014));
}
