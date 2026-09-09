import { expect, it } from 'vitest';
import { applicationSettingsMatch } from './application-settings';

it('accepts server normalization without treating ordered content as a set', () => {
  expect(
    applicationSettingsMatch(
      {
        support_url: 'https://apps.example/',
        default_scopes: ['channels.read', 'messages.send'],
        directory_description_localizations: { fr: 'Bonjour', de: 'Hallo' },
        manifest_generation: '2'
      },
      {
        support_url: 'https://apps.example',
        default_scopes: ['messages.send', 'channels.read'],
        directory_description_localizations: { de: 'Hallo', fr: 'Bonjour' }
      }
    )
  ).toBe(true);
  expect(
    applicationSettingsMatch(
      { directory_media: ['second', 'first'] },
      { directory_media: ['first', 'second'] }
    )
  ).toBe(false);
});

it.each([
  [{ support_url: null }, { support_url: 'https://apps.example/support' }],
  [{ directory_supported_locales: [] }, { directory_supported_locales: ['fr'] }],
  [{ default_permissions: '0' }, { default_permissions: '9007199254740992' }],
  [{}, { directory_category: 'utilities' }]
])('rejects missing or discarded settings', (saved, submitted) => {
  expect(applicationSettingsMatch(saved, submitted)).toBe(false);
});
