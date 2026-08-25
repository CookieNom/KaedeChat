import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:kaede_mobile/src/domain/models.dart';
import 'package:kaede_mobile/src/features/chat/channel_view.dart';
import 'package:kaede_mobile/src/features/chat/composer_pickers.dart';
import 'package:kaede_mobile/src/protocol/generated.dart';
import 'package:kaede_mobile/src/theme/kaede_theme.dart';

void main() {
  group('composer insertion', () {
    test('replaces the active selection and restores the cursor', () {
      const current = TextEditingValue(
        text: 'hello there',
        selection: TextSelection(baseOffset: 6, extentOffset: 11),
      );

      final next = insertComposerText(current, '😀');

      expect(next?.text, 'hello 😀');
      expect(next?.selection, const TextSelection.collapsed(offset: 8));
    });

    test('appends for an invalid selection and enforces 4,000 characters', () {
      final invalidSelection = TextEditingValue(
        text: 'hello',
        selection: const TextSelection.collapsed(offset: -1),
      );

      expect(insertComposerText(invalidSelection, '!')?.text, 'hello!');
      expect(
        insertComposerText(
          TextEditingValue(
            text: List<String>.filled(4000, 'a').join(),
            selection: const TextSelection.collapsed(offset: 4000),
          ),
          '😀',
        ),
        isNull,
      );
    });
  });

  group('custom composer emoji', () {
    final guild = EntityRef.parse('20@home.example');

    KaedeChannel channel({required bool external}) => KaedeChannel(
          ref: EntityRef.parse('30@home.example'),
          guildRef: guild,
          type: ChannelType.text,
          position: 0,
          permissions: external
              ? BigInt.from(Permission.useExternalEmojis)
              : BigInt.zero,
        );

    Map<String, Object?> emoji({
      required String id,
      required String origin,
      required String guildId,
      required String guildDomain,
      required String name,
      bool animated = false,
    }) =>
        <String, Object?>{
          'id': id,
          'origin_domain': origin,
          'guild_id': guildId,
          'guild_domain': guildDomain,
          'name': name,
          'animated': animated,
          'media_hash': 'sha256:abc',
        };

    test('builds federated tokens and filters external guild emoji', () {
      final local = emoji(
        id: '41',
        origin: 'home.example',
        guildId: '20',
        guildDomain: 'home.example',
        name: 'party_blob',
        animated: true,
      );
      final external = emoji(
        id: '42',
        origin: 'remote.example',
        guildId: '21',
        guildDomain: 'remote.example',
        name: 'remote_wave',
      );
      final malformed = <String, Object?>{
        ...local,
        'id': '../../etc/passwd',
      };

      final withoutPermission = composerCustomEmojis(
        <Map<String, Object?>>[local, external, malformed],
        channel(external: false),
      );
      expect(withoutPermission, hasLength(1));
      expect(
        withoutPermission.single.token,
        '<a:party_blob:41@home.example>',
      );
      expect(
        withoutPermission.single.previewUri.toString(),
        'https://home.example/media/emojis/41/thumbnail_128',
      );

      final withPermission = composerCustomEmojis(
        <Map<String, Object?>>[external, local],
        channel(external: true),
      );
      expect(withPermission.map((item) => item.name),
          <String>['party_blob', 'remote_wave']);
    });
  });

  group('guild stickers', () {
    final currentGuild = EntityRef.parse('20@home.example');

    KaedeChannel channel({required bool external}) => KaedeChannel(
          ref: EntityRef.parse('30@home.example'),
          guildRef: currentGuild,
          type: ChannelType.text,
          position: 0,
          permissions: external
              ? BigInt.from(Permission.useExternalEmojis)
              : BigInt.zero,
        );

    Map<String, Object?> sticker({
      required String id,
      required String origin,
      required String guildId,
      required String guildDomain,
      required String guildName,
      required String name,
    }) =>
        <String, Object?>{
          'id': id,
          'origin_domain': origin,
          'guild_id': guildId,
          'guild_domain': guildDomain,
          'guild_name': guildName,
          'name': name,
          'animated': false,
          'media_hash': 'abc',
        };

    test('filters external stickers and keeps the current guild first', () {
      final local = sticker(
        id: '41',
        origin: 'home.example',
        guildId: '20',
        guildDomain: 'home.example',
        guildName: 'Z Local',
        name: 'wave',
      );
      final external = sticker(
        id: '42',
        origin: 'remote.example',
        guildId: '21',
        guildDomain: 'remote.example',
        guildName: 'A Remote',
        name: 'party',
      );

      expect(composerStickers([local, external], channel(external: false)),
          hasLength(1));
      final visible = composerStickers(
        [external, local],
        channel(external: true),
      );
      expect(visible.map((item) => item.guildName), ['Z Local', 'A Remote']);
      expect(visible.first.token, '<sticker:wave:41@home.example>');
    });

    test('parses only exact canonical sticker messages', () {
      final parsed = messageSticker('<sticker:wave:41@home.example>');
      expect(parsed?.name, 'wave');
      expect(parsed?.ref, EntityRef.parse('41@home.example'));
      expect(messageSticker('hello <sticker:wave:41@home.example>'), isNull);
      expect(messageSticker('<sticker:bad name:41@home.example>'), isNull);
    });
  });

  group('GIF response safety', () {
    test('accepts only trusted HTTPS assets and preserves pagination', () {
      final page = ComposerGifPage.fromJson(<String, Object?>{
        'page': 1,
        'next_page': '2',
        'items': <Object?>[
          <String, Object?>{
            'id': 'safe',
            'title': 'Celebration',
            'url': 'https://media.klipy.com/final.gif',
            'preview_url': 'http://static.klipy.com/unsafe-preview.gif',
            'width': '320',
            'height': 180,
          },
          <String, Object?>{
            'id': 'subdomain',
            'title': 'Not trusted',
            'url': 'https://media.klipy.com.attacker.example/final.gif',
            'preview_url': 'https://static.klipy.com/preview.gif',
          },
          <String, Object?>{
            'id': 'credentials',
            'title': 'Not trusted',
            'url': 'https://user@media.klipy.com/final.gif',
            'preview_url': 'https://static.klipy.com/preview.gif',
          },
        ],
      });

      expect(page.page, 1);
      expect(page.nextPage, 2);
      expect(page.items, hasLength(1));
      expect(page.items.single.width, 320);
      expect(page.items.single.height, 180);
      expect(page.items.single.previewUrl, page.items.single.url);
    });

    test('disables server GIF search only for E2EE channels', () {
      KaedeChannel channel(String mode) => KaedeChannel(
            ref: EntityRef.parse('30@home.example'),
            type: ChannelType.dm,
            position: 0,
            permissions: BigInt.zero,
            encryptionMode: mode,
          );

      expect(composerAllowsGifs(channel('plaintext')), isTrue);
      expect(composerAllowsGifs(channel('e2ee')), isFalse);
    });
  });

  testWidgets('add sheet keeps all mobile media in one action', (tester) async {
    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: Scaffold(
        body: Builder(builder: (context) {
          return FilledButton(
            onPressed: () => showComposerActionPicker(
              context,
              canAttach: false,
              gifsAllowed: false,
            ),
            child: const Text('Open'),
          );
        }),
      ),
    ));

    await tester.tap(find.text('Open'));
    await tester.pumpAndSettle();

    final attach = tester.widget<ListTile>(
      find.byKey(const ValueKey('composer-action-attach')),
    );
    expect(attach.enabled, isFalse);
    expect(
      find.text('GIFs are unavailable here; stickers and emoji work'),
      findsOneWidget,
    );
    expect(find.text('GIFs, stickers, and emoji'), findsOneWidget);
    expect(
      find.byKey(const ValueKey('composer-action-media')),
      findsOneWidget,
    );
    expect(find.byKey(const ValueKey('composer-action-gif')), findsNothing);
    expect(find.byKey(const ValueKey('composer-action-sticker')), findsNothing);
  });

  testWidgets('mobile media pane switches between GIFs, stickers, and emoji',
      (tester) async {
    final channel = KaedeChannel(
      ref: EntityRef.parse('30@home.example'),
      guildRef: EntityRef.parse('20@home.example'),
      type: ChannelType.text,
      position: 0,
      permissions: BigInt.zero,
    );
    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: Scaffold(
        body: ComposerMediaPicker(
          gifLoader: ({String? query, int page = 1}) async => <String, Object?>{
            'page': page,
            'next_page': null,
            'items': const <Object?>[],
          },
          emojiLoader: () async => const <Map<String, Object?>>[],
          stickerLoader: () async => const <Map<String, Object?>>[],
          channel: channel,
          gifsAllowed: true,
          categories: const <String, List<String>>{
            'Smileys': <String>['😀'],
          },
        ),
      ),
    ));
    await tester.pump();
    await tester.pump();

    expect(
      find.byKey(const ValueKey('composer-media-tabs')),
      findsOneWidget,
    );
    expect(
      find.byKey(const ValueKey('composer-gif-search')),
      findsOneWidget,
    );
    expect(
      find.byKey(const ValueKey('composer-sticker-search')),
      findsNothing,
    );

    await tester.tap(find.text('Stickers'));
    await tester.pumpAndSettle();

    expect(
      find.byKey(const ValueKey('composer-emoji-search')),
      findsNothing,
    );
    expect(
      find.byKey(const ValueKey('composer-sticker-search')),
      findsOneWidget,
    );

    await tester.tap(find.text('Emoji'));
    await tester.pumpAndSettle();

    expect(
      find.byKey(const ValueKey('composer-sticker-search')),
      findsNothing,
    );
    expect(
      find.byKey(const ValueKey('composer-emoji-search')),
      findsOneWidget,
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('mobile media pane keeps a disabled GIF category in E2EE',
      (tester) async {
    var gifRequests = 0;
    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: Scaffold(
        body: ComposerMediaPicker(
          gifLoader: ({String? query, int page = 1}) async {
            gifRequests += 1;
            return <String, Object?>{
              'page': page,
              'next_page': null,
              'items': const <Object?>[],
            };
          },
          emojiLoader: () async => const <Map<String, Object?>>[],
          stickerLoader: () async => const <Map<String, Object?>>[],
          channel: KaedeChannel(
            ref: EntityRef.parse('30@home.example'),
            type: ChannelType.dm,
            position: 0,
            permissions: BigInt.zero,
            encryptionMode: 'e2ee',
          ),
          gifsAllowed: false,
          categories: const <String, List<String>>{
            'Smileys': <String>['😀'],
          },
        ),
      ),
    ));
    await tester.pump();
    await tester.pump();

    expect(find.byKey(const ValueKey('composer-emoji-search')), findsOneWidget);
    await tester.tap(find.text('GIFs'));
    await tester.pumpAndSettle();

    expect(
      find.text(
        'GIF search is unavailable in end-to-end encrypted conversations.',
      ),
      findsOneWidget,
    );
    expect(gifRequests, 0);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
      'GIF picker stays keyboard-safe, paginates, and debounces searches',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(360, 480));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final calls = <(String?, int)>[];
    Future<Map<String, Object?>> loader({String? query, int page = 1}) async {
      calls.add((query, page));
      return <String, Object?>{
        'page': page,
        'next_page': page == 1 ? 2 : null,
        'items': <Object?>[
          <String, Object?>{
            'id': '$page-${query ?? 'trending'}',
            'title': page == 1 ? 'Celebration' : 'Happy dance',
            'url': 'https://media.klipy.com/$page.gif',
            'preview_url': 'https://static.klipy.com/$page.webp',
          },
        ],
      };
    }

    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: MediaQuery(
        data: const MediaQueryData(
          size: Size(360, 480),
          viewInsets: EdgeInsets.only(bottom: 260),
        ),
        child: Scaffold(body: ComposerGifPicker(loader: loader)),
      ),
    ));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 50));

    expect(tester.takeException(), isNull);
    expect(calls, <(String?, int)>[(null, 1)]);
    expect(
      find.byWidgetPredicate(
        (widget) =>
            widget is Semantics &&
            widget.properties.label == 'Send GIF: Celebration',
      ),
      findsOneWidget,
    );

    await tester.tap(find.byKey(const ValueKey('composer-gif-load-more')));
    await tester.pump();
    expect(calls.last, (null, 2));

    await tester.enterText(
      find.byKey(const ValueKey('composer-gif-search')),
      'cats',
    );
    await tester.pump(const Duration(milliseconds: 299));
    expect(calls.last, (null, 2));
    await tester.pump(const Duration(milliseconds: 2));
    await tester.pump();
    expect(calls.last, ('cats', 1));
    expect(tester.takeException(), isNull);
  });

  testWidgets('emoji and GIF pickers survive a short keyboard viewport',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(360, 320));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    Widget shortKeyboardFrame(Widget child) => MaterialApp(
          theme: kaedeTheme(),
          home: MediaQuery(
            data: const MediaQueryData(
              size: Size(360, 320),
              viewInsets: EdgeInsets.only(bottom: 260),
            ),
            child: Scaffold(
              resizeToAvoidBottomInset: false,
              body: child,
            ),
          ),
        );

    await tester.pumpWidget(shortKeyboardFrame(ComposerEmojiPicker(
      loader: () async => const <Map<String, Object?>>[],
      channel: KaedeChannel(
        ref: EntityRef.parse('30@home.example'),
        type: ChannelType.dm,
        position: 0,
        permissions: BigInt.zero,
      ),
      categories: const <String, List<String>>{
        'Smileys': <String>['😀'],
      },
    )));
    await tester.pump();
    await tester.pump();

    expect(
      find.byKey(const ValueKey('composer-picker-compact-scroll')),
      findsOneWidget,
    );
    expect(tester.takeException(), isNull);

    await tester.pumpWidget(shortKeyboardFrame(ComposerGifPicker(
      loader: ({String? query, int page = 1}) async => <String, Object?>{
        'page': page,
        'next_page': null,
        'items': const <Object?>[],
      },
    )));
    await tester.pump();
    await tester.pump();

    expect(
      find.byKey(const ValueKey('composer-picker-compact-scroll')),
      findsOneWidget,
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('GIF picker exposes loading, empty, error, and retry states',
      (tester) async {
    final first = Completer<Map<String, Object?>>();
    var attempts = 0;
    Future<Map<String, Object?>> loader({String? query, int page = 1}) {
      attempts += 1;
      if (attempts == 1) return first.future;
      if (attempts == 2) return Future.error(StateError('offline'));
      return Future.value(<String, Object?>{
        'page': 1,
        'next_page': null,
        'items': <Object?>[],
      });
    }

    await tester.pumpWidget(MaterialApp(
      theme: kaedeTheme(),
      home: Scaffold(body: ComposerGifPicker(loader: loader)),
    ));
    expect(find.text('Loading GIFs…'), findsOneWidget);

    first.completeError(StateError('offline'));
    await tester.pump();
    await tester.pump();
    expect(find.text('Retry'), findsOneWidget);

    await tester.tap(find.text('Retry'));
    await tester.pump();
    await tester.pump();
    expect(find.text('Retry'), findsOneWidget);

    await tester.tap(find.text('Retry'));
    await tester.pump();
    await tester.pump();
    expect(find.text('No GIFs found. Try another search.'), findsOneWidget);
  });
}
