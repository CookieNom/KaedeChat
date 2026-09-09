import type { ApplicationCommandOption } from './application-commands';

export interface CommandDraft {
  name: string;
  type?: 'chat_input' | 'user' | 'message';
  description?: string;
  options?: ApplicationCommandOption[];
  integration_types?: Array<'guild_install' | 'user_install'>;
  contexts?: Array<'guild' | 'bot_dm' | 'private_channel'>;
  [key: string]: unknown;
}

export const COMMAND_KINDS = [
  {
    type: 'chat_input',
    label: 'Slash command',
    example: '/help',
    description: 'People type / in chat to find and run it.'
  },
  {
    type: 'user',
    label: 'User action',
    example: 'View profile',
    description: 'Appears in the Apps menu for a person.'
  },
  {
    type: 'message',
    label: 'Message action',
    example: 'Translate',
    description: 'Appears in the Apps menu for a message.'
  }
] as const;

export const INPUT_KINDS = [
  ['string', 'Text'],
  ['integer', 'Whole number'],
  ['number', 'Number'],
  ['boolean', 'Yes / no'],
  ['user', 'Person'],
  ['channel', 'Channel'],
  ['role', 'Role'],
  ['mentionable', 'Person or role'],
  ['attachment', 'File']
] as const;

export function readCommandDraft(text: string): CommandDraft[] {
  const value: unknown = JSON.parse(text);
  if (!Array.isArray(value)) throw new Error('Commands must be a JSON array.');
  for (const command of value) {
    if (
      !command ||
      typeof command !== 'object' ||
      Array.isArray(command) ||
      typeof command.name !== 'string' ||
      !COMMAND_KINDS.some((kind) => kind.type === (command.type ?? 'chat_input')) ||
      (command.description !== undefined && typeof command.description !== 'string') ||
      (command.options !== undefined &&
        (!Array.isArray(command.options) ||
          command.options.some(
            (option: unknown) =>
              !option ||
              typeof option !== 'object' ||
              typeof (option as ApplicationCommandOption).name !== 'string' ||
              !(
                [...INPUT_KINDS.map(([type]) => type), 'subcommand', 'subcommand_group'] as string[]
              ).includes((option as ApplicationCommandOption).type) ||
              ((option as ApplicationCommandOption).description !== undefined &&
                typeof (option as ApplicationCommandOption).description !== 'string') ||
              ((option as ApplicationCommandOption).required !== undefined &&
                typeof (option as ApplicationCommandOption).required !== 'boolean')
          ))) ||
      (command.integration_types !== undefined &&
        (!Array.isArray(command.integration_types) ||
          command.integration_types.some(
            (type: unknown) => !['guild_install', 'user_install'].includes(String(type))
          ))) ||
      (command.contexts !== undefined &&
        (!Array.isArray(command.contexts) ||
          command.contexts.some(
            (context: unknown) => !['guild', 'bot_dm', 'private_channel'].includes(String(context))
          )))
    )
      throw new Error(
        'Each command needs a name, a supported type, and valid input and availability lists.'
      );
  }
  return value as CommandDraft[];
}

function validSlashName(value: string): boolean {
  return (
    value === value.toLowerCase() &&
    Array.from(value).every((char) => {
      if (/^[\p{L}\p{N}_-]$/u.test(char)) return true;
      const point = char.codePointAt(0)!;
      return (
        /^\p{M}$/u.test(char) &&
        [
          [0x0900, 0x097f],
          [0xa8e0, 0xa8ff],
          [0x11b00, 0x11b5f],
          [0x0e00, 0x0e7f]
        ].some(([start, end]) => point >= start && point <= end)
      );
    })
  );
}

/** Early feedback for the form; the API validates the complete command contract. */
export function commandDraftError(text: string): string {
  try {
    const commands = readCommandDraft(text);
    const names = new Set<string>();
    for (const command of commands) {
      const type = command.type ?? 'chat_input';
      const label = command.name || 'New command';
      if (!command.name.trim() || Array.from(command.name).length > 32)
        return 'Give every command a name of 1–32 characters.';
      if (type === 'chat_input' && !validSlashName(command.name))
        return `${label}: use lowercase letters, numbers, hyphens, or underscores, without spaces or a leading slash.`;
      if (
        type !== 'chat_input' &&
        (command.name !== command.name.trim() || /\p{C}/u.test(command.name))
      )
        return `${label}: remove leading/trailing spaces and control characters.`;
      if (
        type === 'chat_input' &&
        (!command.description?.trim() || Array.from(command.description).length > 100)
      )
        return `${label}: add a description of 1–100 characters.`;
      if (type !== 'chat_input' && (command.description || command.options?.length))
        return `${label}: user and message actions cannot have a description or inputs.`;
      if (command.integration_types?.length === 0 || command.contexts?.length === 0) {
        return `${label}: choose at least one install type and one place where people can use it.`;
      }
      const key = `${type}:${command.name}`;
      if (names.has(key)) return `${label}: another command of this type already has that name.`;
      names.add(key);
      if (
        command.options?.length &&
        !command.options.some((option) => ['subcommand', 'subcommand_group'].includes(option.type))
      ) {
        const inputs = new Set<string>();
        let optionalSeen = false;
        for (const option of command.options) {
          if (!option.name || Array.from(option.name).length > 32 || !validSlashName(option.name))
            return `${label}: each input needs a lowercase name of 1–32 characters, without spaces.`;
          if (!option.description?.trim() || Array.from(option.description).length > 100)
            return `${label}: each input needs a description of 1–100 characters.`;
          if (inputs.has(option.name)) return `${label}: input names must be unique.`;
          inputs.add(option.name);
          if (optionalSeen && option.required)
            return `${label}: put required inputs before optional inputs.`;
          optionalSeen ||= !option.required;
        }
      }
      if (command.options && command.options.length > 25) return `${label}: use at most 25 inputs.`;
    }
    for (const kind of COMMAND_KINDS) {
      const limit = kind.type === 'chat_input' ? 100 : 15;
      if (commands.filter((command) => (command.type ?? 'chat_input') === kind.type).length > limit)
        return `You can publish at most ${limit} commands of type ${kind.label.toLowerCase()}.`;
    }
    return '';
  } catch (error) {
    return error instanceof SyntaxError
      ? 'The JSON is not valid. Check commas, quotes, and brackets.'
      : (error as Error).message;
  }
}

export function hasAdvancedInputSettings(option: ApplicationCommandOption): boolean {
  return Object.entries(option).some(
    ([key, value]) =>
      !['type', 'name', 'description', 'required'].includes(key) &&
      (Array.isArray(value)
        ? value.length > 0
        : value && typeof value === 'object'
          ? Object.keys(value).length > 0
          : value !== undefined && value !== null && value !== false)
  );
}
