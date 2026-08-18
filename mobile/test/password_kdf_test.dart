import 'dart:convert';

import 'package:cryptography/cryptography.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:kaede_mobile/src/auth/password_kdf.dart';
import 'package:kaede_mobile/src/auth/password_vault.dart';
import 'package:kaede_mobile/src/core/refs.dart';
import 'package:mocktail/mocktail.dart';

final class _MockSecureStorage extends Mock implements FlutterSecureStorage {}

String _base64url(List<int> value) =>
    base64Url.encode(value).replaceAll('=', '');

void main() {
  group('mobile password protocol', () {
    final instance = Domain('Kaede.Example.');
    const context = ModernMobilePasswordKdfContext(
      authSalt: 'AAECAwQFBgcICQoLDA0ODw',
      vaultSalt: 'EBESExQVFhcYGRobHB0eHw',
    );

    test('matches the cross-client PBKDF2-SHA256 vectors', () async {
      final prepared = await prepareMobilePassword(
        'correct horse battery staple',
        context,
        instance,
      );
      try {
        expect(
          prepared.authenticationSecret,
          '-Z__QIBecQeJPG4vVovIPtt-Oct4ZE8zUSWu3oyMG3s',
        );
        final extracted = await prepared.vaultKey.extract();
        try {
          expect(
            _base64url(extracted.bytes),
            'ldmLIyIp7qlfGzCzvaUPzi4jvB3R8aDgFJUcQHZ2v70',
          );
        } finally {
          if (!identical(extracted, prepared.vaultKey)) extracted.destroy();
        }
      } finally {
        prepared.destroy();
      }
    });

    test('parses only exact modern and legacy server contexts', () {
      expect(
        MobilePasswordKdfContext.fromJson(context.toJson()),
        isA<ModernMobilePasswordKdfContext>(),
      );
      expect(
        MobilePasswordKdfContext.fromJson(<String, Object?>{
          'version': 0,
          'algorithm': 'legacy',
          'iterations': 0,
          'auth_salt': null,
          'vault_salt': context.vaultSalt,
        }),
        isA<LegacyMobilePasswordKdfContext>(),
      );
      expect(
        () => MobilePasswordKdfContext.fromJson(<String, Object?>{
          ...context.toJson(),
          'version': 1,
        }),
        throwsFormatException,
      );
      expect(
        () => MobilePasswordKdfContext.fromJson(<String, Object?>{
          ...context.toJson(),
          'iterations': mobilePasswordKdfIterations - 1,
        }),
        throwsFormatException,
      );
    });

    test('preserves the raw legacy password while preparing an upgrade',
        () async {
      final prepared = await prepareMobilePassword(
        ' leading and trailing ',
        const LegacyMobilePasswordKdfContext(
          vaultSalt: 'EBESExQVFhcYGRobHB0eHw',
        ),
        instance,
      );
      try {
        expect(prepared.authenticationSecret, ' leading and trailing ');
        expect(prepared.context.version, 0);
        expect(prepared.passwordUpgrade?['password'],
            matches(RegExp(r'^[A-Za-z0-9_-]{43}$')));
        expect(
          prepared.passwordUpgrade?['password_kdf'],
          containsPair('version', mobilePasswordKdfVersion),
        );
      } finally {
        prepared.destroy();
      }
    });

    test('enforces the original password policy before deriving', () async {
      await expectLater(
        prepareMobileRegistrationPassword('too-short', instance),
        throwsArgumentError,
      );
      await expectLater(
        prepareMobileResetPassword('x' * 257, instance),
        throwsArgumentError,
      );
      final reset = await prepareMobileResetPassword(' ten chars ', instance);
      expect(
          reset.authenticationSecret, matches(RegExp(r'^[A-Za-z0-9_-]{43}$')));
      expect(reset.passwordKdf['vault_salt'], isNull);
    });

    test('binds both derived secrets to the locally selected instance',
        () async {
      final first = await prepareMobilePassword(
        'correct horse battery staple',
        context,
        Domain('kaede.example'),
      );
      final second = await prepareMobilePassword(
        'correct horse battery staple',
        context,
        Domain('relay.example'),
      );
      try {
        expect(first.authenticationSecret, isNot(second.authenticationSecret));
        final firstKey = await first.vaultKey.extract();
        final secondKey = await second.vaultKey.extract();
        try {
          expect(firstKey.bytes, isNot(secondKey.bytes));
        } finally {
          if (!identical(firstKey, first.vaultKey)) firstKey.destroy();
          if (!identical(secondKey, second.vaultKey)) secondKey.destroy();
        }
      } finally {
        first.destroy();
        second.destroy();
      }
    });
  });

  group('protected account-vault key', () {
    test('isolates account labels and clears only the selected account',
        () async {
      final storage = _MockSecureStorage();
      final values = <String, String>{};
      when(() => storage.write(
            key: any(named: 'key'),
            value: any(named: 'value'),
          )).thenAnswer((invocation) async {
        values[invocation.namedArguments[#key]! as String] =
            invocation.namedArguments[#value]! as String;
      });
      when(() => storage.read(key: any(named: 'key'))).thenAnswer(
        (invocation) async =>
            values[invocation.namedArguments[#key]! as String],
      );
      when(() => storage.delete(key: any(named: 'key'))).thenAnswer(
        (invocation) async {
          values.remove(invocation.namedArguments[#key]! as String);
        },
      );
      final vault = MobilePasswordVault(storage);
      final key = SecretKeyData(
        List<int>.generate(32, (index) => index),
        overwriteWhenDestroyed: true,
      );
      try {
        await vault.write('1@alpha.example', key);
        await vault.write('2@beta.example', key);
        expect(values, hasLength(2));
        expect(values.keys, everyElement(isNot(contains('alpha.example'))));
        expect(values.keys, everyElement(isNot(contains('beta.example'))));

        final restored = await vault.read('1@alpha.example');
        expect(restored, isNotNull);
        final extracted = await restored!.extract();
        try {
          expect(extracted.bytes, key.bytes);
        } finally {
          if (!identical(extracted, restored)) extracted.destroy();
          restored.destroy();
        }

        await vault.clear('1@alpha.example');
        expect(await vault.read('1@alpha.example'), isNull);
        final other = await vault.read('2@beta.example');
        expect(other, isNotNull);
        other?.destroy();
      } finally {
        key.destroy();
      }
    });
  });
}
