import { describe, expect, it } from 'vitest';
import {
  commandCompletions,
  commandInvocation,
  commandOptionsComplete,
  commandStringOptions,
  type ApplicationCommand
} from './application-commands';

const commands: ApplicationCommand[] = [
  {
    id: '1',
    application_ref: '2@apps.test',
    application_name: 'Poll Bot',
    name: 'poll',
    type: 'chat_input',
    description: 'Create a poll'
  },
  {
    id: '2',
    application_ref: '2@apps.test',
    application_name: 'Poll Bot',
    name: 'about',
    type: 'chat_input'
  }
];

describe('application commands', () => {
  it('matches command and application names for composer completion', () => {
    expect(commandCompletions(commands, 'pol').map((item) => item.value)).toEqual([
      '/poll',
      '/about'
    ]);
  });

  it('parses only unambiguous published commands and keeps arguments opaque', () => {
    expect(commandInvocation('/poll lunch tomorrow', commands)).toEqual({
      command: commands[0],
      options: { raw: 'lunch tomorrow' }
    });
    expect(commandInvocation('/missing hello', commands)).toBeNull();
  });

  it('exposes the bounded string options needed by Discord-style command fields', () => {
    const thread = {
      ...commands[0],
      name: 'thread',
      options: [
        { type: 'string' as const, name: 'name', required: true },
        { type: 'string' as const, name: 'message', required: true },
        { type: 'boolean' as const, name: 'private' }
      ]
    };
    expect(commandStringOptions(thread).map((option) => option.name)).toEqual(['name', 'message']);
    expect(commandOptionsComplete(thread, { name: 'test', message: '' })).toBe(false);
    expect(commandOptionsComplete(thread, { name: 'test', message: 'hello' })).toBe(true);
  });
});
