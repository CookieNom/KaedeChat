import type { CompletionOption } from './completion';

export interface ApplicationCommand {
  id: string;
  application_ref: string;
  application_name: string;
  name: string;
  type: 'chat_input' | 'user' | 'message';
  description?: string;
  options?: Array<Record<string, unknown>>;
}

export function commandCompletions(
  commands: ApplicationCommand[],
  query: string
): CompletionOption[] {
  const needle = query.toLocaleLowerCase();
  return commands
    .filter(
      (command) =>
        command.type === 'chat_input' &&
        (command.name.toLocaleLowerCase().includes(needle) ||
          command.application_name.toLocaleLowerCase().includes(needle))
    )
    .sort((left, right) => {
      const score = (command: ApplicationCommand) => {
        const name = command.name.toLocaleLowerCase();
        if (name.startsWith(needle)) return 0;
        if (name.includes(needle)) return 1;
        return 2;
      };
      return score(left) - score(right) || left.name.localeCompare(right.name);
    })
    .map((command) => ({
      value: `/${command.name}`,
      label: `/${command.name}`,
      detail: [command.description, command.application_name].filter(Boolean).join(' · '),
      kind: 'application-command' as const
    }));
}

export function commandInvocation(
  content: string,
  commands: ApplicationCommand[]
): { command: ApplicationCommand; options: Record<string, unknown> } | null {
  const match = /^\/([a-z0-9_-]{1,32})(?:\s+([\s\S]*))?$/.exec(content.trim());
  if (!match) return null;
  const matching = commands.filter(
    (command) => command.type === 'chat_input' && command.name === match[1]
  );
  if (matching.length !== 1) return null;
  const raw = (match[2] ?? '').trim();
  return {
    command: matching[0],
    options: raw ? { raw } : {}
  };
}
