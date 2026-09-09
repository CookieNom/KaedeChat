import { expect, it } from 'vitest';
import { commandDraftError, readCommandDraft } from './command-editor';

it.each([
  ['[]', ''],
  ['{}', 'JSON array'],
  ['[', 'not valid'],
  [JSON.stringify([{ name: 'Hello', description: 'Say hello' }]), 'lowercase'],
  [JSON.stringify([{ name: 'help', description: '' }]), 'description'],
  [JSON.stringify([{ type: 'user', name: 'View profile' }]), ''],
  [JSON.stringify([{ type: 'message', name: 'Translate', description: 'Wrong' }]), 'cannot have'],
  [
    JSON.stringify([
      { name: 'help', description: 'Help' },
      { name: 'help', description: 'Help again' }
    ]),
    'already has'
  ],
  [JSON.stringify([{ name: 'help', description: 'Help', contexts: [] }]), 'at least one'],
  [JSON.stringify([{ name: 'खोज', description: 'Search' }]), ''],
  [
    JSON.stringify([
      {
        name: 'search',
        description: 'Search',
        options: [{ type: 'string', name: 'query', description: 7 }]
      }
    ]),
    'valid input'
  ]
])('validates a command draft with actionable feedback', (text, expected) => {
  const error = commandDraftError(text);
  if (expected) expect(error).toContain(expected);
  else expect(error).toBe('');
});

it('preserves nested commands, translations, permissions, and input constraints', () => {
  const definition = {
    name: 'manage',
    description: 'Manage',
    name_localizations: { fr: 'gerer' },
    default_member_permissions: ['MANAGE_GUILD'],
    nsfw: true,
    options: [
      {
        type: 'subcommand',
        name: 'search',
        description: 'Search',
        options: [
          { type: 'integer', name: 'count', description: 'Count', min_value: 0, max_value: 100 }
        ]
      }
    ]
  };
  expect(readCommandDraft(JSON.stringify([definition]))).toEqual([definition]);
});
