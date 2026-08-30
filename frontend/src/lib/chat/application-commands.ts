import type { ApplicationCommandCompletionIdentity, CompletionOption } from './completion';
import { parseCanonicalEntityRef } from './refs';
import { preferredLocale } from '$lib/ui/locale';

export interface ApplicationCommandChoice {
  name: string;
  name_localizations?: Record<string, string>;
  value: string | number;
}

export interface ApplicationCommandOption {
  type:
    | 'subcommand'
    | 'subcommand_group'
    | 'string'
    | 'integer'
    | 'boolean'
    | 'user'
    | 'channel'
    | 'role'
    | 'mentionable'
    | 'number'
    | 'attachment';
  name: string;
  name_localizations?: Record<string, string>;
  description?: string;
  description_localizations?: Record<string, string>;
  required?: boolean;
  min_length?: number;
  max_length?: number;
  min_value?: number;
  max_value?: number;
  autocomplete?: boolean;
  choices?: ApplicationCommandChoice[];
  channel_types?: number[];
  file_types?: string[];
  options?: ApplicationCommandOption[];
}

export interface ApplicationCommandAutocompleteChoice {
  name: string;
  value: string | number;
}

export interface ApplicationCommand {
  id: string;
  application_ref: string;
  application_name: string;
  /** Authority-selected installation used for this discovered command. */
  integration_type: 'guild_install' | 'user_install' | 'dm_capability';
  /** Exact short-lived bot-DM grant selected by discovery. */
  dm_capability_id?: string | null;
  dm_capability_revision?: string | null;
  /** Exact context the authority will validate for invocation. */
  interaction_context: 'guild' | 'bot_dm' | 'private_channel';
  name: string;
  name_localizations?: Record<string, string>;
  type: 'chat_input' | 'user' | 'message';
  description?: string;
  description_localizations?: Record<string, string>;
  options?: ApplicationCommandOption[];
}

export type CommandComposerValue = string | boolean;
export type CommandComposerValues = Record<string, CommandComposerValue>;

export interface CommandOptionField {
  option: ApplicationCommandOption;
  path: string;
}

export interface CommandOptionSelector {
  path: string;
  label: string;
  options: ApplicationCommandOption[];
  selected: string;
}

export interface CommandComposerModel {
  selectors: CommandOptionSelector[];
  fields: CommandOptionField[];
}

export interface ApplicationCommandGroup {
  applicationRef: string;
  applicationName: string;
  commands: ApplicationCommand[];
}

/** USE_APPLICATION_COMMANDS gates guild installs, not external user-installed apps. */
export function applicationIntegrationAllowedByUsePermission(
  integrationType: ApplicationCommand['integration_type'] | null | undefined,
  canUseGuildCommands: boolean
): boolean {
  return (
    canUseGuildCommands || integrationType === 'user_install' || integrationType === 'dm_capability'
  );
}

export function applicationCommandAllowedByUsePermission(
  command: Pick<ApplicationCommand, 'integration_type'>,
  canUseGuildCommands: boolean
): boolean {
  return applicationIntegrationAllowedByUsePermission(
    command.integration_type,
    canUseGuildCommands
  );
}

export function applicationCommandAllowedByChannelPermissions(
  command: Pick<ApplicationCommand, 'integration_type' | 'type'>,
  canUseGuildCommands: boolean,
  canSendUserCommands: boolean
): boolean {
  return (
    applicationCommandAllowedByUsePermission(command, canUseGuildCommands) &&
    (command.type !== 'user' || canSendUserCommands)
  );
}

const CONTAINER_KEY = '$container:';
const COMMAND_TYPES = new Set<ApplicationCommand['type']>(['chat_input', 'user', 'message']);
const OPTION_TYPES = new Set<ApplicationCommandOption['type']>([
  'subcommand',
  'subcommand_group',
  'string',
  'integer',
  'boolean',
  'user',
  'channel',
  'role',
  'mentionable',
  'number',
  'attachment'
]);

function commandRecord(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} is invalid.`);
  }
  return value as Record<string, unknown>;
}

function commandLocalizations(value: unknown, label: string): Record<string, string> | undefined {
  if (value === undefined) return undefined;
  const raw = commandRecord(value, label);
  if (Object.values(raw).some((item) => typeof item !== 'string')) {
    throw new Error(`${label} is invalid.`);
  }
  return raw as Record<string, string>;
}

function commandChoices(value: unknown): ApplicationCommandChoice[] | undefined {
  if (value === undefined) return undefined;
  if (!Array.isArray(value) || value.length > 25) {
    throw new Error('Application command choices are invalid.');
  }
  return value.map((item) => {
    const raw = commandRecord(item, 'Application command choice');
    if (
      typeof raw.name !== 'string' ||
      (typeof raw.value !== 'string' &&
        (typeof raw.value !== 'number' || !Number.isFinite(raw.value)))
    ) {
      throw new Error('Application command choice is invalid.');
    }
    return {
      ...(raw as unknown as ApplicationCommandChoice),
      name_localizations: commandLocalizations(
        raw.name_localizations,
        'Application command choice localizations'
      )
    };
  });
}

export function parseApplicationCommandAutocompleteChoices(
  value: unknown
): ApplicationCommandAutocompleteChoice[] {
  if (!Array.isArray(value) || value.length > 25) {
    throw new Error('Application command autocomplete choices are invalid.');
  }
  return value.map((item) => {
    const raw = commandRecord(item, 'Application command autocomplete choice');
    if (
      typeof raw.name !== 'string' ||
      (typeof raw.value !== 'string' &&
        (typeof raw.value !== 'number' || !Number.isFinite(raw.value)))
    ) {
      throw new Error('Application command autocomplete choice is invalid.');
    }
    return { name: raw.name, value: raw.value };
  });
}

function commandScalarArray<T extends string | number>(
  value: unknown,
  label: string,
  valid: (item: unknown) => item is T
): T[] | undefined {
  if (value === undefined) return undefined;
  if (!Array.isArray(value) || value.some((item) => !valid(item))) {
    throw new Error(`${label} is invalid.`);
  }
  return [...value];
}

function commandOptions(value: unknown, depth = 0): ApplicationCommandOption[] | undefined {
  if (value === undefined) return undefined;
  if (!Array.isArray(value) || value.length > 25 || depth > 2) {
    throw new Error('Application command options are invalid.');
  }
  return value.map((item) => {
    const raw = commandRecord(item, 'Application command option');
    if (
      typeof raw.name !== 'string' ||
      typeof raw.type !== 'string' ||
      !OPTION_TYPES.has(raw.type as ApplicationCommandOption['type']) ||
      (raw.description !== undefined && typeof raw.description !== 'string') ||
      (raw.required !== undefined && typeof raw.required !== 'boolean') ||
      (raw.autocomplete !== undefined && typeof raw.autocomplete !== 'boolean')
    ) {
      throw new Error('Application command option is invalid.');
    }
    for (const field of ['min_length', 'max_length'] as const) {
      if (raw[field] !== undefined && !Number.isSafeInteger(raw[field])) {
        throw new Error('Application command option bounds are invalid.');
      }
    }
    for (const field of ['min_value', 'max_value'] as const) {
      if (
        raw[field] !== undefined &&
        (typeof raw[field] !== 'number' || !Number.isFinite(raw[field]))
      ) {
        throw new Error('Application command option bounds are invalid.');
      }
    }
    return {
      ...(raw as unknown as ApplicationCommandOption),
      name_localizations: commandLocalizations(
        raw.name_localizations,
        'Application command option name localizations'
      ),
      description_localizations: commandLocalizations(
        raw.description_localizations,
        'Application command option description localizations'
      ),
      choices: commandChoices(raw.choices),
      channel_types: commandScalarArray(
        raw.channel_types,
        'Application command channel types',
        (entry): entry is number => Number.isSafeInteger(entry)
      ),
      file_types: commandScalarArray(
        raw.file_types,
        'Application command file types',
        (entry): entry is string => typeof entry === 'string'
      ),
      options: commandOptions(raw.options, depth + 1)
    };
  });
}

/** Fail closed at the federated discovery boundary instead of dropping hostile children. */
export function parseApplicationCommands(value: unknown): ApplicationCommand[] {
  if (!Array.isArray(value)) throw new Error('Application commands are invalid.');
  const identities = new Set<string>();
  return value.map((item) => {
    const raw = commandRecord(item, 'Application command');
    if (
      typeof raw.id !== 'string' ||
      !raw.id ||
      typeof raw.application_ref !== 'string' ||
      !parseCanonicalEntityRef(raw.application_ref) ||
      typeof raw.application_name !== 'string' ||
      typeof raw.name !== 'string' ||
      typeof raw.type !== 'string' ||
      !COMMAND_TYPES.has(raw.type as ApplicationCommand['type']) ||
      !['guild_install', 'user_install', 'dm_capability'].includes(String(raw.integration_type)) ||
      !['guild', 'bot_dm', 'private_channel'].includes(String(raw.interaction_context)) ||
      (raw.description !== undefined && typeof raw.description !== 'string')
    ) {
      throw new Error('Application command is invalid.');
    }
    const capabilityBound = raw.integration_type === 'dm_capability';
    if (
      capabilityBound !==
        (typeof raw.dm_capability_id === 'string' &&
          /^kbdg_[A-Za-z0-9_-]{43}$/u.test(raw.dm_capability_id) &&
          typeof raw.dm_capability_revision === 'string' &&
          /^[1-9][0-9]{0,18}$/u.test(raw.dm_capability_revision) &&
          BigInt(raw.dm_capability_revision) <= 9_223_372_036_854_775_807n) ||
      (!capabilityBound && (raw.dm_capability_id ?? null) !== null) ||
      (!capabilityBound && (raw.dm_capability_revision ?? null) !== null)
    ) {
      throw new Error('Application command DM capability lineage is invalid.');
    }
    const identity = JSON.stringify([
      raw.application_ref,
      raw.id,
      raw.type,
      raw.integration_type,
      raw.interaction_context
    ]);
    if (identities.has(identity)) {
      throw new Error('Application command identity is duplicated.');
    }
    identities.add(identity);
    return {
      ...(raw as unknown as ApplicationCommand),
      name_localizations: commandLocalizations(
        raw.name_localizations,
        'Application command name localizations'
      ),
      description_localizations: commandLocalizations(
        raw.description_localizations,
        'Application command description localizations'
      ),
      options: commandOptions(raw.options)
    };
  });
}

/** Exact authority-selected command lineage submitted for invocation/autocomplete. */
export function applicationCommandRequestIdentity(
  command: ApplicationCommand
): Record<string, string> {
  if (
    command.integration_type === 'dm_capability' &&
    (!command.dm_capability_id || !command.dm_capability_revision)
  ) {
    throw new Error('Application command DM capability lineage is invalid.');
  }
  return {
    application_ref: command.application_ref,
    command_id: command.id,
    integration_type: command.integration_type,
    command_name: command.name,
    command_type: command.type,
    ...(command.integration_type === 'dm_capability'
      ? {
          dm_capability_id: command.dm_capability_id!,
          dm_capability_revision: command.dm_capability_revision!
        }
      : {})
  };
}

function commandLocaleFallbacks(locale: string): string[] {
  const normalized = locale.trim();
  const values = [normalized];
  if (normalized === 'en-US') values.push('en-GB');
  else if (normalized === 'en-GB') values.push('en-US');
  else if (normalized === 'es-419') values.push('es-ES');
  const language = normalized.split('-')[0];
  if (language && !values.includes(language)) values.push(language);
  return values;
}

export function localizedCommandText(
  fallback: string,
  localizations: Record<string, string> | undefined,
  locale = preferredLocale()
): string {
  for (const candidate of commandLocaleFallbacks(locale)) {
    const localized = localizations?.[candidate]?.trim();
    if (localized) return localized;
  }
  return fallback;
}

export function localizedCommandName(
  command: ApplicationCommand,
  locale = preferredLocale()
): string {
  return localizedCommandText(command.name, command.name_localizations, locale);
}

/** Resolve a displayed slash-command name without replacing its default wire identity. */
export function uniqueChatInputCommand(
  commands: ApplicationCommand[],
  invokedName: string,
  locale = preferredLocale()
): ApplicationCommand | null {
  const matching = commands.filter(
    (command) =>
      command.type === 'chat_input' &&
      (command.name === invokedName || localizedCommandName(command, locale) === invokedName)
  );
  return matching.length === 1 ? matching[0] : null;
}

export function applicationCommandIdentity(
  command: ApplicationCommand
): ApplicationCommandCompletionIdentity {
  return {
    id: command.id,
    applicationRef: command.application_ref,
    integrationType: command.integration_type,
    interactionContext: command.interaction_context
  };
}

/** Resolve only the exact command selected by autocomplete or the Apps launcher. */
export function applicationCommandByIdentity(
  commands: ApplicationCommand[],
  identity: ApplicationCommandCompletionIdentity | undefined
): ApplicationCommand | null {
  if (!identity) return null;
  return (
    commands.find(
      (command) =>
        command.id === identity.id &&
        command.application_ref === identity.applicationRef &&
        command.integration_type === identity.integrationType &&
        command.interaction_context === identity.interactionContext
    ) ?? null
  );
}

export function commandContainerKey(path: readonly string[]): string {
  return `${CONTAINER_KEY}${path.join('.')}`;
}

export function commandOptionPath(path: readonly string[], name: string): string {
  return [...path, name].join('.');
}

function scalarOption(option: ApplicationCommandOption): boolean {
  return option.type !== 'subcommand' && option.type !== 'subcommand_group';
}

/** Resolve the selected Discord-style subcommand/group path and its typed fields. */
export function commandComposerModel(
  options: ApplicationCommandOption[],
  values: CommandComposerValues
): CommandComposerModel {
  const selectors: CommandOptionSelector[] = [];
  const path: string[] = [];
  let current = options;
  for (let depth = 0; depth < 2; depth += 1) {
    const containers = current.filter((option) => !scalarOption(option));
    if (!containers.length) break;
    const key = commandContainerKey(path);
    const selected = typeof values[key] === 'string' ? values[key] : '';
    selectors.push({
      path: key,
      label:
        depth === 0
          ? containers.some((item) => item.type === 'subcommand_group')
            ? 'group or command'
            : 'command'
          : 'subcommand',
      options: containers,
      selected
    });
    const choice = containers.find((option) => option.name === selected);
    if (!choice) return { selectors, fields: [] };
    path.push(choice.name);
    current = choice.options ?? [];
  }
  return {
    selectors,
    fields: current
      .filter(scalarOption)
      .map((option) => ({ option, path: commandOptionPath(path, option.name) }))
  };
}

export function commandStringOptions(command: ApplicationCommand): ApplicationCommandOption[] {
  return (command.options ?? []).filter((option) => option.type === 'string');
}

export function commandComposerOptions(
  command: ApplicationCommand,
  values: CommandComposerValues = {}
): ApplicationCommandOption[] {
  return commandComposerModel(command.options ?? [], values).fields.map((field) => field.option);
}

export function commandOptionsComplete(
  command: ApplicationCommand,
  values: CommandComposerValues
): boolean {
  const model = commandComposerModel(command.options ?? [], values);
  if (model.selectors.some((selector) => !selector.selected)) return false;
  return model.fields.every(({ option, path }) => {
    const value = values[path];
    if (value === undefined || value === '') return !option.required;
    if (option.type === 'boolean') return typeof value === 'boolean';
    if (typeof value !== 'string') return false;
    const trimmed = value.trim();
    if (!trimmed) return !option.required;
    if (option.type === 'integer') {
      const parsed = Number(value);
      return Number.isSafeInteger(parsed) && withinNumericBounds(option, parsed);
    }
    if (option.type === 'number') {
      const parsed = Number(value);
      return Number.isFinite(parsed) && withinNumericBounds(option, parsed);
    }
    if (option.type === 'string') {
      return (
        trimmed.length >= (option.min_length ?? 0) && trimmed.length <= (option.max_length ?? 6000)
      );
    }
    return true;
  });
}

export function commandOptionPayload(
  command: ApplicationCommand,
  values: CommandComposerValues
): Record<string, unknown> {
  const model = commandComposerModel(command.options ?? [], values);
  let leaf: Record<string, unknown> = {};
  for (const { option, path } of model.fields) {
    const raw = values[path];
    if (raw === undefined || raw === '') continue;
    if (option.type === 'boolean' && typeof raw === 'boolean') leaf[option.name] = raw;
    else if (typeof raw === 'string' && option.type === 'integer') {
      const parsed = Number(raw);
      if (Number.isSafeInteger(parsed)) leaf[option.name] = parsed;
    } else if (typeof raw === 'string' && option.type === 'number') {
      const parsed = Number(raw);
      if (Number.isFinite(parsed)) leaf[option.name] = parsed;
    } else if (typeof raw === 'string' && raw.trim()) leaf[option.name] = raw.trim();
  }
  for (const selector of [...model.selectors].reverse()) {
    leaf = { [selector.selected]: leaf };
  }
  return leaf;
}

/** Attachment tickets consumed by the currently selected typed command leaf. */
export function commandAttachmentOptionIds(
  command: ApplicationCommand,
  values: CommandComposerValues
): string[] {
  const seen = new Set<string>();
  return commandComposerModel(command.options ?? [], values).fields.flatMap(({ option, path }) => {
    const value = values[path];
    if (
      option.type !== 'attachment' ||
      typeof value !== 'string' ||
      !/^[1-9]\d{0,18}$/.test(value) ||
      seen.has(value)
    )
      return [];
    seen.add(value);
    return [value];
  });
}

/** Whether a channel belongs in a Discord-style channel option picker. */
export function commandOptionAllowsChannelType(
  option: ApplicationCommandOption,
  channelType: number
): boolean {
  if (option.type !== 'channel') return false;
  const allowed = option.channel_types ?? [];
  return allowed.length === 0 || allowed.includes(channelType);
}

function withinNumericBounds(option: ApplicationCommandOption, value: number): boolean {
  const defaultMinimum = option.type === 'integer' ? Number.MIN_SAFE_INTEGER : -(2 ** 53);
  const defaultMaximum = option.type === 'integer' ? Number.MAX_SAFE_INTEGER : 2 ** 53;
  return (
    value >= (option.min_value ?? defaultMinimum) && value <= (option.max_value ?? defaultMaximum)
  );
}

export function commandCompletions(
  commands: ApplicationCommand[],
  query: string,
  locale = preferredLocale()
): CompletionOption[] {
  const needle = query.toLocaleLowerCase();
  return commands
    .filter((command) => {
      const displayName = localizedCommandName(command, locale);
      return (
        command.type === 'chat_input' &&
        (displayName.toLocaleLowerCase().includes(needle) ||
          command.name.toLocaleLowerCase().includes(needle) ||
          command.application_name.toLocaleLowerCase().includes(needle))
      );
    })
    .sort((left, right) => {
      const score = (command: ApplicationCommand) => {
        const name = localizedCommandName(command, locale).toLocaleLowerCase();
        if (name.startsWith(needle)) return 0;
        if (name.includes(needle)) return 1;
        return 2;
      };
      return (
        score(left) - score(right) ||
        localizedCommandName(left, locale).localeCompare(localizedCommandName(right, locale))
      );
    })
    .map((command) => {
      const name = localizedCommandName(command, locale);
      const description = localizedCommandText(
        command.description ?? '',
        command.description_localizations,
        locale
      );
      return {
        value: `/${name}`,
        label: `/${name}`,
        detail: [description, command.application_name].filter(Boolean).join(' · '),
        kind: 'application-command' as const,
        applicationCommand: applicationCommandIdentity(command)
      };
    });
}

/** App-first, searchable command groups shared by command launchers and context menus. */
export function applicationCommandGroups(
  commands: ApplicationCommand[],
  query: string,
  locale = preferredLocale(),
  commandTypes: ReadonlySet<ApplicationCommand['type']> = new Set(['chat_input'])
): ApplicationCommandGroup[] {
  const needle = query.trim().toLocaleLowerCase();
  const grouped = new Map<string, ApplicationCommandGroup>();
  for (const command of commands) {
    if (!commandTypes.has(command.type)) continue;
    const displayName = localizedCommandName(command, locale);
    const description = localizedCommandText(
      command.description ?? '',
      command.description_localizations,
      locale
    );
    if (
      needle &&
      !`${command.application_name} ${displayName} ${command.name} ${description}`
        .toLocaleLowerCase()
        .includes(needle)
    )
      continue;
    const key = `${command.application_ref}\u0000${command.application_name}`;
    const group = grouped.get(key) ?? {
      applicationRef: command.application_ref,
      applicationName: command.application_name,
      commands: []
    };
    group.commands.push(command);
    grouped.set(key, group);
  }
  return [...grouped.values()]
    .map((group) => ({
      ...group,
      commands: group.commands.sort((left, right) =>
        localizedCommandName(left, locale).localeCompare(localizedCommandName(right, locale))
      )
    }))
    .sort(
      (left, right) =>
        left.applicationName.localeCompare(right.applicationName) ||
        left.applicationRef.localeCompare(right.applicationRef)
    );
}

/** App-first, searchable slash-command groups used by composer launchers. */
export function applicationCommandLauncherGroups(
  commands: ApplicationCommand[],
  query: string,
  locale = preferredLocale()
): ApplicationCommandGroup[] {
  return applicationCommandGroups(commands, query, locale);
}

export type CommandInvocationResolution =
  | { kind: 'none' }
  | { kind: 'ambiguous'; commands: ApplicationCommand[] }
  | { kind: 'resolved'; command: ApplicationCommand; options: Record<string, unknown> };

/** Parse typed slash text while preserving name ambiguity across applications. */
export function resolveCommandInvocation(
  content: string,
  commands: ApplicationCommand[],
  locale = preferredLocale()
): CommandInvocationResolution {
  const match = /^\/([-_\p{L}\p{N}\p{sc=Devanagari}\p{sc=Thai}]{1,32})(?:\s+([\s\S]*))?$/u.exec(
    content.trim()
  );
  if (!match || match[1] !== match[1].toLocaleLowerCase()) return { kind: 'none' };
  const matching = commands.filter(
    (command) =>
      command.type === 'chat_input' &&
      (command.name === match[1] || localizedCommandName(command, locale) === match[1])
  );
  if (matching.length > 1) return { kind: 'ambiguous', commands: matching };
  if (matching.length === 0) return { kind: 'none' };
  const raw = (match[2] ?? '').trim();
  return {
    kind: 'resolved',
    command: matching[0],
    options: raw ? { raw } : {}
  };
}

export function commandInvocation(
  content: string,
  commands: ApplicationCommand[],
  locale = preferredLocale()
): { command: ApplicationCommand; options: Record<string, unknown> } | null {
  const resolution = resolveCommandInvocation(content, commands, locale);
  return resolution.kind === 'resolved'
    ? { command: resolution.command, options: resolution.options }
    : null;
}
