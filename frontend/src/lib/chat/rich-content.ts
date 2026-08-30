import type { Attachment, Channel, Message, Role, UserSummary } from './types';
import type { EncryptedFileManifest } from '$lib/e2ee/media';
import { parseCanonicalEntityRef } from './refs';

export interface PartialEmoji {
  id?: string | null;
  name?: string | null;
  animated?: boolean;
}

export interface EmbedFooter {
  text: string;
  icon_url?: string | null;
}

export interface EmbedMedia {
  url: string;
}

export interface EmbedAuthor {
  name: string;
  url?: string | null;
  icon_url?: string | null;
}

export interface EmbedField {
  name: string;
  value: string;
  inline?: boolean;
}

export interface MessageEmbed {
  /** Output-only Discord embed discriminator (for example `poll_result`). */
  type?: string;
  title?: string | null;
  description?: string | null;
  url?: string | null;
  timestamp?: string | null;
  color?: number | null;
  footer?: EmbedFooter | null;
  image?: EmbedMedia | null;
  thumbnail?: EmbedMedia | null;
  author?: EmbedAuthor | null;
  fields?: EmbedField[];
}

export interface ButtonComponent {
  type: 2;
  id?: number | null;
  style: 1 | 2 | 3 | 4 | 5 | 6;
  label?: string | null;
  emoji?: PartialEmoji | null;
  custom_id?: string | null;
  url?: string | null;
  sku_id?: string | null;
  disabled?: boolean;
}

export interface SelectOption {
  label: string;
  value: string;
  description?: string | null;
  emoji?: PartialEmoji | null;
  default?: boolean;
}

export interface SelectDefaultValue {
  id: string;
  type: 'user' | 'role' | 'channel';
}

interface SelectComponentBase {
  id?: number | null;
  custom_id: string;
  placeholder?: string | null;
  min_values?: number;
  max_values?: number;
  disabled?: boolean;
  required?: boolean | null;
}

export interface StringSelectComponent extends SelectComponentBase {
  type: 3;
  options: SelectOption[];
}

export interface EntitySelectComponent extends SelectComponentBase {
  type: 5 | 6 | 7 | 8;
  default_values?: SelectDefaultValue[];
  channel_types?: number[];
}

export interface TextInputComponent {
  type: 4;
  id?: number | null;
  custom_id: string;
  style?: 1 | 2;
  label?: string | null;
  min_length?: number | null;
  max_length?: number | null;
  required?: boolean;
  value?: string | null;
  placeholder?: string | null;
}

export type MessageComponent =
  ButtonComponent | StringSelectComponent | EntitySelectComponent | TextInputComponent;

export interface ActionRow {
  type: 1;
  id?: number | null;
  components: MessageComponent[];
}

export interface UnfurledMediaItem {
  url: string;
}

export interface TextDisplayComponent {
  type: 10;
  id?: number | null;
  content: string;
}

export interface ThumbnailComponent {
  type: 11;
  id?: number | null;
  media: UnfurledMediaItem;
  description?: string | null;
  spoiler?: boolean;
}

export interface SectionComponent {
  type: 9;
  id?: number | null;
  components: TextDisplayComponent[];
  accessory: ButtonComponent | ThumbnailComponent;
}

export interface MediaGalleryItem {
  media: UnfurledMediaItem;
  description?: string | null;
  spoiler?: boolean;
}

export interface MediaGalleryComponent {
  type: 12;
  id?: number | null;
  items: MediaGalleryItem[];
}

export interface FileComponent {
  type: 13;
  id?: number | null;
  file: UnfurledMediaItem;
  spoiler?: boolean;
}

export interface SeparatorComponent {
  type: 14;
  id?: number | null;
  divider?: boolean;
  spacing?: 1 | 2;
}

export interface ContainerComponent {
  type: 17;
  id?: number | null;
  components: ContainerChild[];
  accent_color?: number | null;
  spoiler?: boolean;
}

export type ContainerChild =
  | ActionRow
  | TextDisplayComponent
  | SectionComponent
  | MediaGalleryComponent
  | SeparatorComponent
  | FileComponent;

export type MessageLayoutComponent =
  | ActionRow
  | SectionComponent
  | TextDisplayComponent
  | MediaGalleryComponent
  | FileComponent
  | SeparatorComponent
  | ContainerComponent;

export interface ChoiceOption {
  label: string;
  value: string;
  description?: string | null;
  default?: boolean;
}

export interface FileUploadComponent {
  type: 19;
  id?: number | null;
  custom_id: string;
  min_values?: number;
  max_values?: number;
  required?: boolean;
  file_types?: string[];
}

export function fileUploadAccept(fileTypes: readonly string[] | undefined): string | undefined {
  const values = (fileTypes ?? []).map((rawValue) => {
    const value = rawValue.toLowerCase();
    return value === 'image' || value === 'video' || value === 'audio' ? `${value}/*` : value;
  });
  return values.length ? values.join(',') : undefined;
}

/** Mirror Discord's client-side picker filter; the server remains authoritative. */
export function fileUploadMatches(
  fileTypes: readonly string[] | undefined,
  filename: string,
  contentType: string
): boolean {
  const filters = (fileTypes ?? []).map((value) => value.toLowerCase());
  if (!filters.length) return true;
  const name = filename.toLowerCase();
  const mediaType = contentType.toLowerCase().split('/', 1)[0];
  return filters.some((filter) =>
    filter === 'image' || filter === 'video' || filter === 'audio'
      ? mediaType === filter
      : name.endsWith(filter)
  );
}

export interface RadioGroupComponent {
  type: 21;
  id?: number | null;
  custom_id: string;
  options: ChoiceOption[];
  required?: boolean;
}

export interface CheckboxGroupComponent {
  type: 22;
  id?: number | null;
  custom_id: string;
  options: ChoiceOption[];
  min_values?: number;
  max_values?: number;
  required?: boolean;
}

export interface CheckboxV2Component {
  type: 23;
  id?: number | null;
  custom_id: string;
  default?: boolean;
}

export type ModalInputComponent =
  | TextInputComponent
  | StringSelectComponent
  | EntitySelectComponent
  | FileUploadComponent
  | RadioGroupComponent
  | CheckboxGroupComponent
  | CheckboxV2Component;

export interface LabelComponent {
  type: 18;
  id?: number | null;
  label: string;
  description?: string | null;
  component: ModalInputComponent;
}

export type ModalLayoutComponent = ActionRow | LabelComponent | TextDisplayComponent;

export interface PollMedia {
  text?: string | null;
  emoji?: PartialEmoji | null;
}

export interface PollAnswer {
  answer_id: number;
  poll_media: PollMedia;
}

export interface PollAnswerCount {
  id: number;
  count: number;
  me_voted: boolean;
}

export interface MessagePoll {
  question: PollMedia;
  answers: PollAnswer[];
  expiry: string;
  allow_multiselect: boolean;
  layout_type: 1;
  results: {
    is_finalized: boolean;
    answer_counts: PollAnswerCount[];
  };
}

export interface MessagePollResultAnswerCount {
  id: number;
  count: number;
}

export interface MessagePollResultPresentation {
  poll_message_ref: string;
  source_encryption_mode: 'plaintext' | 'e2ee';
  answer_counts: MessagePollResultAnswerCount[];
  total_votes: number;
  victor_answer_id: number | null;
  victor_answer_votes: number;
  question_text: string | null;
  victor_answer_text: string | null;
  victor_answer_emoji: PartialEmoji | null;
}

export interface PollCreatePayload {
  question: PollMedia;
  answers: Array<{ poll_media: PollMedia }>;
  duration: number;
  allow_multiselect: boolean;
  layout_type: 1;
}

export interface InteractionModal {
  title: string;
  custom_id: string;
  components: ModalLayoutComponent[];
}

export interface InteractionResponseEvent {
  id?: string;
  interaction_id: string;
  interaction_ref?: string;
  authority_domain?: string;
  user_ref?: string;
  invoker_ref?: string;
  channel_ref?: string;
  application_ref?: string;
  sequence?: number;
  response_type?: number;
  callback_type?: number;
  type?: number;
  response_id?: string;
  response_ref?: string;
  response_grant_id?: string;
  message_ref?: string | null;
  revision?: string;
  operation?: 'CREATE' | 'UPDATE' | 'DELETE';
  expires_at?: string;
  autocomplete_generation?: string | number | null;
  ephemeral?: boolean;
  deleted_at?: string | null;
  decryption_unavailable?: boolean;
  data?: Record<string, unknown>;
  payload?: Record<string, unknown>;
  modal?: InteractionModal;
  message?: Partial<Message>;
}

const FEDERATION_DOMAIN =
  /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i;
const CONTENT_TYPE = /^[a-z0-9!#$&^_.+-]+\/[a-z0-9!#$&^_.+-]+$/i;
const ATTACHMENT_STATES = new Set<Attachment['scan_status']>([
  'pending',
  'clean',
  'rejected',
  'infected',
  'failed',
  'encrypted'
]);

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function boundedInteger(value: unknown, minimum: number, maximum: number): number | null {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isSafeInteger(parsed) && parsed >= minimum && parsed <= maximum ? parsed : null;
}

function safeAttachmentFilename(value: unknown, id: string): string {
  if (typeof value !== 'string') return `attachment-${id}`;
  const printable = [...value]
    .filter((character) => {
      const code = character.codePointAt(0) ?? 0;
      return code >= 32 && code !== 127;
    })
    .join('');
  const leaf = printable.split(/[\\/]/).at(-1)?.trim();
  if (!leaf) return `attachment-${id}`;
  return [...leaf].slice(0, 255).join('');
}

/**
 * Parse the untrusted Gateway projection for a private response attachment.
 * Invalid entries are omitted; IDs and origins are kept as separate encoded
 * path segments by the media layer and filenames are display-only.
 */
export function interactionResponseAttachments(data: Record<string, unknown>): Attachment[] {
  if (!Array.isArray(data.attachments)) return [];
  const parsed: Attachment[] = [];
  const seen = new Set<string>();
  for (const raw of data.attachments.slice(0, 10)) {
    const item = objectValue(raw);
    if (!item) continue;
    const id = typeof item.id === 'string' ? item.id : '';
    const origin = typeof item.origin_domain === 'string' ? item.origin_domain : '';
    if (!/^[1-9]\d{0,18}$/.test(id) || !FEDERATION_DOMAIN.test(origin)) continue;
    const key = `${id}@${origin}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const rawState = typeof item.scan_status === 'string' ? item.scan_status : 'failed';
    const scanStatus = ATTACHMENT_STATES.has(rawState as Attachment['scan_status'])
      ? (rawState as Attachment['scan_status'])
      : 'failed';
    const contentType =
      typeof item.content_type === 'string' && CONTENT_TYPE.test(item.content_type)
        ? item.content_type.toLowerCase()
        : 'application/octet-stream';
    parsed.push({
      id,
      origin_domain: origin,
      filename: safeAttachmentFilename(item.filename, id),
      content_type: contentType,
      size: boundedInteger(item.size, 0, Number.MAX_SAFE_INTEGER) ?? 0,
      width: boundedInteger(item.width, 1, 100_000),
      height: boundedInteger(item.height, 1, 100_000),
      blurhash: typeof item.blurhash === 'string' ? item.blurhash.slice(0, 128) : null,
      scan_status: scanStatus,
      encryption_mode: item.encryption_mode === 'e2ee' ? 'e2ee' : 'plaintext',
      encryption_protocol: item.encryption_protocol === 'kaede-file-v1' ? 'kaede-file-v1' : null,
      variants: (objectValue(item.variants) as Attachment['variants'] | null) ?? {},
      private_media_url: typeof item.private_media_url === 'string' ? item.private_media_url : null
    });
  }
  return parsed;
}

export function interactionResponseEncryptedManifests(
  data: Record<string, unknown>
): Record<string, EncryptedFileManifest> {
  if (!Array.isArray(data.attachments)) return {};
  const result: Record<string, EncryptedFileManifest> = {};
  for (const raw of data.attachments.slice(0, 10)) {
    const item = objectValue(raw);
    const manifest = objectValue(item?.encrypted_manifest);
    if (!item || !manifest || item.encryption_mode !== 'e2ee') continue;
    const id = typeof item.id === 'string' ? item.id : '';
    const origin = typeof item.origin_domain === 'string' ? item.origin_domain : '';
    if (
      !/^[1-9]\d{0,18}$/u.test(id) ||
      !FEDERATION_DOMAIN.test(origin) ||
      manifest.version !== 1 ||
      manifest.protocol !== 'kaede-file-v1' ||
      manifest.attachment_id !== id ||
      manifest.attachment_domain !== origin
    ) {
      continue;
    }
    result[`${id}@${origin}`] = manifest as unknown as EncryptedFileManifest;
  }
  return result;
}

function pollMedia(value: unknown): PollMedia | null {
  const media = objectValue(value);
  if (!media) return null;
  const text = typeof media.text === 'string' && media.text.trim() ? media.text : null;
  const rawEmoji = objectValue(media.emoji);
  const emoji = rawEmoji
    ? {
        id: typeof rawEmoji.id === 'string' ? rawEmoji.id : null,
        name: typeof rawEmoji.name === 'string' ? rawEmoji.name : null,
        animated: rawEmoji.animated === true
      }
    : null;
  return text || emoji?.id || emoji?.name ? { text, emoji } : null;
}

/**
 * Normalize both stored poll projections and Discord-style poll create data.
 * Ephemeral create data has no vote endpoint or result rows, so answer IDs and
 * zero counts are supplied only for deterministic read-only presentation.
 */
export function interactionResponsePoll(data: Record<string, unknown>): MessagePoll | null {
  const raw = objectValue(data.poll);
  if (!raw || !Array.isArray(raw.answers)) return null;
  const question = pollMedia(raw.question);
  if (!question?.text) return null;
  const answers = raw.answers.slice(0, 10).flatMap((value, index) => {
    const answer = objectValue(value);
    const media = pollMedia(answer?.poll_media);
    if (!answer || !media) return [];
    return [
      {
        answer_id: boundedInteger(answer.answer_id, 1, 1_000_000) ?? index + 1,
        poll_media: media
      }
    ];
  });
  if (
    answers.length < 2 ||
    new Set(answers.map((answer) => answer.answer_id)).size !== answers.length
  )
    return null;
  const result = objectValue(raw.results);
  const counts = Array.isArray(result?.answer_counts)
    ? result.answer_counts.flatMap((value) => {
        const count = objectValue(value);
        const id = boundedInteger(count?.id, 1, 1_000_000);
        if (!count || id === null || !answers.some((answer) => answer.answer_id === id)) return [];
        return [
          {
            id,
            count: boundedInteger(count.count, 0, Number.MAX_SAFE_INTEGER) ?? 0,
            me_voted: count.me_voted === true
          }
        ];
      })
    : [];
  return {
    question,
    answers,
    expiry: typeof raw.expiry === 'string' ? raw.expiry : '',
    allow_multiselect: raw.allow_multiselect === true,
    layout_type: 1,
    results: {
      is_finalized: result?.is_finalized === true,
      answer_counts: answers.map(
        (answer) =>
          counts.find((count) => count.id === answer.answer_id) ?? {
            id: answer.answer_id,
            count: 0,
            me_voted: false
          }
      )
    }
  };
}

export function interactionResponseHasMessageContent(data: Record<string, unknown>): boolean {
  return Boolean(
    (typeof data.content === 'string' && data.content.length) ||
    (Array.isArray(data.embeds) && data.embeds.length) ||
    (Array.isArray(data.components) && data.components.length) ||
    interactionResponseAttachments(data).length ||
    interactionResponsePoll(data)
  );
}

export function ephemeralComponentInteractionBody(
  applicationRef: string,
  responseId: string,
  viewVersion: number,
  component: Exclude<MessageComponent, TextInputComponent>,
  values: string[] = []
): Record<string, unknown> | null {
  if (!('custom_id' in component) || !component.custom_id || viewVersion < 1) return null;
  return {
    application_ref: applicationRef,
    interaction_type: 'component',
    response_id: responseId,
    view_version: viewVersion,
    custom_id: component.custom_id,
    values
  };
}

export interface ComponentContext {
  users: UserSummary[];
  roles: Role[];
  channels: Channel[];
}

export interface EntitySelectOption {
  value: string;
  label: string;
  type: 'user' | 'role' | 'channel';
}

export function applicationRef(message: Message): string | null {
  if (!message.application_id || !message.application_domain) return null;
  return `${message.application_id}@${message.application_domain}`;
}

export function partialEmojiText(emoji: PartialEmoji | null | undefined): string {
  if (!emoji) return '';
  if (!emoji.id) return emoji.name ?? '';
  return emoji.name ? `:${emoji.name}:` : 'Custom emoji';
}

export function embedAccent(color: number | null | undefined): string {
  const bounded = Number.isInteger(color) && color! >= 0 && color! <= 0xffffff ? color! : 0x70747c;
  return `#${bounded.toString(16).padStart(6, '0')}`;
}

export function pollCount(poll: MessagePoll, answerId: number): PollAnswerCount {
  return (
    poll.results.answer_counts.find((item) => item.id === answerId) ?? {
      id: answerId,
      count: 0,
      me_voted: false
    }
  );
}

export function pollTotalVotes(poll: MessagePoll): number {
  return poll.results.answer_counts.reduce((total, item) => total + item.count, 0);
}

export function pollAnswerPercent(poll: MessagePoll, answerId: number): number {
  const total = pollTotalVotes(poll);
  return total === 0 ? 0 : Math.round((pollCount(poll, answerId).count / total) * 100);
}

export function pollIsClosed(poll: MessagePoll, now = Date.now()): boolean {
  const expiry = Date.parse(poll.expiry);
  return poll.results.is_finalized || !Number.isFinite(expiry) || expiry <= now;
}

const POLL_RESULT_FIELDS = new Set([
  'poll_question_text',
  'victor_answer_votes',
  'total_votes',
  'victor_answer_id',
  'victor_answer_text',
  'victor_answer_emoji_id',
  'victor_answer_emoji_name',
  'victor_answer_emoji_animated'
]);
const POLL_RESULT_PRIVATE_FIELDS = new Set([
  'poll_question_text',
  'victor_answer_text',
  'victor_answer_emoji_id',
  'victor_answer_emoji_name',
  'victor_answer_emoji_animated'
]);

function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function strictSafeInteger(value: unknown, minimum = 0, maximum = Number.MAX_SAFE_INTEGER) {
  return typeof value === 'number' &&
    Number.isSafeInteger(value) &&
    value >= minimum &&
    value <= maximum
    ? value
    : null;
}

/**
 * Validate the complete authority projection for Discord's automatic type-46
 * poll result. Invalid federation/Gateway input returns an unavailable state;
 * it is never rendered through the generic embed path.
 */
export function messagePollResult(
  message: Message,
  verifiedReferencedMessage: Message | null = null
): MessagePollResultPresentation | null {
  if (message.message_type !== 46) return null;
  const projection = objectValue(message.poll_result);
  if (
    !projection ||
    !exactKeys(projection, [
      'version',
      'poll_message_ref',
      'source_encryption_mode',
      'answer_counts',
      'total_votes',
      'victor_answer_id',
      'victor_answer_votes'
    ]) ||
    projection.version !== 1
  ) {
    return null;
  }
  const sourceRef = parseCanonicalEntityRef(projection.poll_message_ref);
  const directSourceRef =
    message.referenced_message_id && message.referenced_message_domain
      ? parseCanonicalEntityRef(
          `${message.referenced_message_id}@${message.referenced_message_domain}`
        )
      : null;
  if (
    !sourceRef ||
    !directSourceRef ||
    sourceRef.id !== directSourceRef.id ||
    sourceRef.origin_domain !== directSourceRef.origin_domain
  ) {
    return null;
  }
  const sourceMode = projection.source_encryption_mode;
  if (sourceMode !== 'plaintext' && sourceMode !== 'e2ee') return null;
  if (
    !Array.isArray(projection.answer_counts) ||
    projection.answer_counts.length < 1 ||
    projection.answer_counts.length > 10
  ) {
    return null;
  }
  const counts: MessagePollResultAnswerCount[] = [];
  for (const item of projection.answer_counts) {
    const count = objectValue(item);
    if (!count || !exactKeys(count, ['id', 'count'])) return null;
    const id = strictSafeInteger(count.id, 1, 10);
    const value = strictSafeInteger(count.count);
    if (id === null || value === null) return null;
    counts.push({ id, count: value });
  }
  const identifiers = counts.map((item) => item.id);
  if (identifiers.some((id, index) => index > 0 && id <= identifiers[index - 1])) return null;
  const totalVotes = strictSafeInteger(projection.total_votes);
  const victorVotes = strictSafeInteger(projection.victor_answer_votes);
  const rawVictor = projection.victor_answer_id;
  const victorId = rawVictor === null ? null : strictSafeInteger(rawVictor, 1, 10);
  if (totalVotes === null || victorVotes === null || (rawVictor !== null && victorId === null)) {
    return null;
  }
  const highest = Math.max(...counts.map((item) => item.count));
  const winners = counts.filter((item) => item.count === highest).map((item) => item.id);
  const expectedVictor = highest > 0 && winners.length === 1 ? winners[0] : null;
  if (
    counts.reduce((total, item) => total + item.count, 0) !== totalVotes ||
    highest !== victorVotes ||
    victorId !== expectedVictor
  ) {
    return null;
  }
  if (
    message.content != null ||
    message.e2ee != null ||
    (message.attachments?.length ?? 0) !== 0 ||
    (message.components?.length ?? 0) !== 0 ||
    (message.sticker_items?.length ?? 0) !== 0 ||
    message.poll != null ||
    (message.flags ?? 0) !== 0 ||
    message.tts === true ||
    !Array.isArray(message.embeds) ||
    message.embeds.length !== 1
  ) {
    return null;
  }
  const embed = objectValue(message.embeds[0]);
  if (!embed || !exactKeys(embed, ['type', 'fields']) || embed.type !== 'poll_result') return null;
  if (!Array.isArray(embed.fields) || embed.fields.length < 2 || embed.fields.length > 8) {
    return null;
  }
  const fields = new Map<string, string>();
  for (const item of embed.fields) {
    const field = objectValue(item);
    if (
      !field ||
      !exactKeys(field, ['name', 'value', 'inline']) ||
      typeof field.name !== 'string' ||
      !POLL_RESULT_FIELDS.has(field.name) ||
      fields.has(field.name) ||
      typeof field.value !== 'string' ||
      field.value.length < 1 ||
      field.value.length > 1_024 ||
      field.inline !== false
    ) {
      return null;
    }
    fields.set(field.name, field.value);
  }
  if (
    fields.get('victor_answer_votes') !== String(victorVotes) ||
    fields.get('total_votes') !== String(totalVotes) ||
    fields.get('victor_answer_id') !== (victorId === null ? undefined : String(victorId)) ||
    (victorId === null &&
      [...POLL_RESULT_PRIVATE_FIELDS].some(
        (name) => name !== 'poll_question_text' && fields.has(name)
      )) ||
    (fields.has('victor_answer_emoji_animated') &&
      !['true', 'false'].includes(fields.get('victor_answer_emoji_animated')!)) ||
    (sourceMode === 'e2ee' && [...POLL_RESULT_PRIVATE_FIELDS].some((name) => fields.has(name)))
  ) {
    return null;
  }

  let questionText = fields.get('poll_question_text') ?? null;
  let victorAnswerText = fields.get('victor_answer_text') ?? null;
  let victorAnswerEmoji: PartialEmoji | null =
    fields.has('victor_answer_emoji_id') || fields.has('victor_answer_emoji_name')
      ? {
          id: fields.get('victor_answer_emoji_id') ?? null,
          name: fields.get('victor_answer_emoji_name') ?? null,
          animated: fields.get('victor_answer_emoji_animated') === 'true'
        }
      : null;
  const availableReference = verifiedReferencedMessage;
  const availableReferenceMatches =
    availableReference != null &&
    `${availableReference.id}@${availableReference.origin_domain}` === projection.poll_message_ref;
  if (availableReferenceMatches && (sourceMode === 'e2ee') !== (availableReference.e2ee != null)) {
    return null;
  }
  if (sourceMode === 'e2ee') {
    questionText = null;
    victorAnswerText = null;
    victorAnswerEmoji = null;
    const referenced = availableReference;
    const referencedRef = referenced ? `${referenced.id}@${referenced.origin_domain}` : null;
    const poll = referenced?.poll;
    const pollIds = poll?.answers
      .map((answer) => answer.answer_id)
      .sort((left, right) => left - right);
    if (
      referencedRef === projection.poll_message_ref &&
      referenced?.e2ee != null &&
      referenced.e2ee_verified === true &&
      poll &&
      pollIds?.length === identifiers.length &&
      pollIds.every((id, index) => id === identifiers[index])
    ) {
      questionText = poll.question.text ?? null;
      const answer = poll.answers.find((item) => item.answer_id === victorId);
      victorAnswerText = answer?.poll_media.text ?? null;
      victorAnswerEmoji = answer?.poll_media.emoji ?? null;
    }
  }
  return {
    poll_message_ref: projection.poll_message_ref as string,
    source_encryption_mode: sourceMode,
    answer_counts: counts,
    total_votes: totalVotes,
    victor_answer_id: victorId,
    victor_answer_votes: victorVotes,
    question_text: questionText,
    victor_answer_text: victorAnswerText,
    victor_answer_emoji: victorAnswerEmoji
  };
}

export function componentInteractionBody(
  message: Message,
  component: Exclude<MessageComponent, TextInputComponent>,
  values: string[] = []
): Record<string, unknown> | null {
  const app = applicationRef(message);
  if (!app || !('custom_id' in component) || !component.custom_id) return null;
  return {
    application_ref: app,
    interaction_type: 'component',
    message_ref: `${message.id}@${message.origin_domain}`,
    ...(message.view_version ? { view_version: message.view_version } : {}),
    custom_id: component.custom_id,
    values
  };
}

export function modalSubmitBody(
  message: Message,
  responseId: string,
  modal: InteractionModal,
  values: Record<string, string | boolean | string[] | null>
): Record<string, unknown> | null {
  const app = applicationRef(message);
  if (!app || !responseId) return null;
  function submittedComponent(component: ModalInputComponent | MessageComponent) {
    const customId = 'custom_id' in component ? component.custom_id : '';
    const value = customId ? values[customId] : undefined;
    return {
      type: component.type,
      custom_id: customId,
      ...([3, 5, 6, 7, 8, 19, 22].includes(component.type as number)
        ? { values: Array.isArray(value) ? value : [] }
        : { value: value ?? (component.type === 21 ? null : '') })
    };
  }
  return {
    application_ref: app,
    interaction_type: 'modal_submit',
    response_id: responseId,
    custom_id: modal.custom_id,
    components: modal.components.flatMap((topLevel) =>
      topLevel.type === 10
        ? []
        : [
            topLevel.type === 18
              ? {
                  type: 18,
                  ...(topLevel.id == null ? {} : { id: topLevel.id }),
                  component: submittedComponent(topLevel.component)
                }
              : {
                  type: 1,
                  components: topLevel.components.map(submittedComponent)
                }
          ]
    )
  };
}

export function entitySelectOptions(
  component: EntitySelectComponent,
  context: ComponentContext
): EntitySelectOption[] {
  const users = context.users.map((user) => ({
    value: `${user.id}@${user.origin_domain}`,
    label: user.display_name || user.username,
    type: 'user' as const
  }));
  const roles = context.roles.map((role) => ({
    value: `${role.id}@${role.origin_domain}`,
    label: `@${role.name}`,
    type: 'role' as const
  }));
  const allowedChannelTypes = new Set(component.channel_types ?? []);
  const channels = context.channels
    .filter((channel) => allowedChannelTypes.size === 0 || allowedChannelTypes.has(channel.type))
    .map((channel) => ({
      value: `${channel.id}@${channel.origin_domain}`,
      label: `#${channel.name ?? 'channel'}`,
      type: 'channel' as const
    }));
  if (component.type === 5) return users;
  if (component.type === 6) return roles;
  if (component.type === 7) return [...users, ...roles];
  return channels;
}

export function selectDefaultValues(
  component: StringSelectComponent | EntitySelectComponent
): string[] {
  if (component.type === 3) {
    return component.options.filter((option) => option.default).map((option) => option.value);
  }
  return component.default_values?.map((item) => item.id) ?? [];
}

export function selectSubmissionState(
  component: StringSelectComponent | EntitySelectComponent,
  values: readonly string[]
): { staged: boolean; valid: boolean; minimum: number; maximum: number } {
  const minimum = component.min_values ?? 1;
  const maximum = component.max_values ?? 1;
  return {
    staged: maximum > 1,
    valid:
      values.length >= minimum &&
      values.length <= maximum &&
      new Set(values).size === values.length,
    minimum,
    maximum
  };
}

export function modalFromInteractionEvent(
  event: InteractionResponseEvent
): InteractionModal | null {
  const responseType = event.callback_type ?? event.response_type ?? event.type;
  const data = event.data ?? event.payload;
  const modal = event.modal ?? data;
  if (responseType !== 9 || !modal || typeof modal !== 'object') return null;
  const value = modal as Partial<InteractionModal>;
  if (
    typeof value.title !== 'string' ||
    typeof value.custom_id !== 'string' ||
    !Array.isArray(value.components)
  ) {
    return null;
  }
  return value as InteractionModal;
}
