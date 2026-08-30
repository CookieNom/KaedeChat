import type { Channel, Message } from './types';
import { entityRef, parseCanonicalEntityRef } from './refs';
import {
  prepareMessageForward,
  submitPreparedMessageForward,
  type ForwardMessageResult,
  type PreparedForwardDestination,
  type PreparedForwardResponse,
  type PreparedForwardSource
} from './interactions';
import {
  canonicalSortedCustomEmojiRefs,
  canonicalInteractionJson,
  encryptedForwardAttachmentSemantics,
  encryptedForwardSnapshotProjectionDigest,
  validateEncryptedForwardSnapshot,
  type KaedeE2EEClient
} from '$lib/e2ee/client';
import {
  decryptEncryptedAttachment,
  uploadEncryptedChannelFile,
  type EncryptedFileManifest
} from '$lib/e2ee/media';
import { attachmentMediaPath, authenticatedMediaBlob } from '$lib/media/authenticated';
import { uploadChannelFile, type UploadTicket, type VoiceUploadMetadata } from '$lib/media/uploads';
import { base64url, clearBytes, sha256 } from '$lib/e2ee/encoding';

const FORWARD_NONCE = /^[A-Za-z0-9._:-]{1,64}$/u;
const CANONICAL_DIGEST = /^[A-Za-z0-9_-]{43}$/u;
const FORWARD_PROOF_CONTENT_FIELDS = [
  'version',
  'requester_ref',
  'requester_type',
  'source_message_ref',
  'source_channel_ref',
  'destination_channel_ref',
  'destination_encryption_mode',
  'source_encryption_mode',
  'source_projection_version',
  'source_projection_digest',
  'source_created_at',
  'source_edited_at',
  'source_flags',
  'source_message_type',
  'source_nsfw',
  'source_attachment_refs',
  'source_sticker_items',
  'source_custom_emoji_refs',
  'source_snapshot',
  'application_ref',
  'e2ee_device_id',
  'nonce',
  'expires_at'
] as const;

interface PreparedForwardOptions {
  source: Message;
  sourceChannel: Channel;
  destinations: Channel[];
  requesterRef: string;
  note?: string;
  e2eeClient?: KaedeE2EEClient | null;
  signal?: AbortSignal;
}

export function preparedForwardNeedsCopies(
  sourceMode: 'plaintext' | 'e2ee',
  destinationModes: readonly ('plaintext' | 'e2ee')[],
  attachmentCount: number
): boolean {
  return (
    attachmentCount > 0 || sourceMode === 'e2ee' || destinationModes.some((mode) => mode === 'e2ee')
  );
}

function sameCanonicalJson(left: unknown, right: unknown): boolean {
  const leftBytes = canonicalInteractionJson(left);
  const rightBytes = canonicalInteractionJson(right);
  try {
    if (leftBytes.length !== rightBytes.length) return false;
    let different = 0;
    for (let index = 0; index < leftBytes.length; index += 1) {
      different |= leftBytes[index] ^ rightBytes[index];
    }
    return different === 0;
  } finally {
    clearBytes(leftBytes);
    clearBytes(rightBytes);
  }
}

function samePreparedSource(left: PreparedForwardSource, right: PreparedForwardSource): boolean {
  return (
    left.message_ref === right.message_ref &&
    left.channel_ref === right.channel_ref &&
    left.encryption_mode === right.encryption_mode &&
    left.projection_version === right.projection_version &&
    left.projection_digest === right.projection_digest &&
    left.created_at === right.created_at &&
    left.edited_at === right.edited_at &&
    left.flags === right.flags &&
    left.message_type === right.message_type &&
    left.nsfw === right.nsfw &&
    sameCanonicalJson(left.attachment_refs, right.attachment_refs) &&
    sameCanonicalJson(left.snapshot, right.snapshot)
  );
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} is invalid.`);
  }
  return value as Record<string, unknown>;
}

function exactFields(value: Record<string, unknown>, fields: readonly string[]): boolean {
  return Object.keys(value).length === fields.length && fields.every((field) => field in value);
}

function canonicalTimestamp(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    /(?:Z|[+-][0-9]{2}:[0-9]{2})$/u.test(value) &&
    Number.isFinite(Date.parse(value))
  );
}

function canonicalProofStickerItems(value: unknown): boolean {
  if (!Array.isArray(value) || value.length > 9) return false;
  const refs: string[] = [];
  for (const item of value) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return false;
    const raw = item as Record<string, unknown>;
    if (
      !exactFields(raw, ['id', 'origin_domain', 'name', 'format_type', 'media_hash']) ||
      typeof raw.id !== 'string' ||
      typeof raw.origin_domain !== 'string' ||
      !parseCanonicalEntityRef(`${raw.id}@${raw.origin_domain}`) ||
      typeof raw.name !== 'string' ||
      raw.name !== raw.name.trim() ||
      [...raw.name].length < 2 ||
      [...raw.name].length > 30 ||
      [...raw.name].some((character) => {
        const code = character.codePointAt(0)!;
        return code < 32 || code === 127;
      }) ||
      !Number.isSafeInteger(raw.format_type) ||
      ![1, 2, 3, 4].includes(Number(raw.format_type)) ||
      typeof raw.media_hash !== 'string' ||
      !/^[0-9a-f]{64}$/u.test(raw.media_hash)
    ) {
      return false;
    }
    refs.push(`${raw.id}@${raw.origin_domain}`);
  }
  return sameCanonicalJson(refs, [...new Set(refs)].sort());
}

function validatePreparedSource(
  value: unknown,
  sourceChannelRef: string,
  sourceMessageRef: string
): PreparedForwardSource {
  const raw = record(value, 'Forward source authorization');
  if (
    !exactFields(raw, [
      'message_ref',
      'channel_ref',
      'encryption_mode',
      'projection_version',
      'projection_digest',
      'created_at',
      'edited_at',
      'flags',
      'message_type',
      'nsfw',
      'attachment_refs',
      'snapshot'
    ]) ||
    raw.message_ref !== sourceMessageRef ||
    raw.channel_ref !== sourceChannelRef ||
    !['plaintext', 'e2ee'].includes(String(raw.encryption_mode)) ||
    raw.projection_version !== 2 ||
    typeof raw.projection_digest !== 'string' ||
    !CANONICAL_DIGEST.test(raw.projection_digest) ||
    !canonicalTimestamp(raw.created_at) ||
    (raw.edited_at !== null && !canonicalTimestamp(raw.edited_at)) ||
    (raw.edited_at !== null && Date.parse(raw.edited_at) < Date.parse(raw.created_at)) ||
    !Number.isSafeInteger(raw.flags) ||
    Number(raw.flags) < 0 ||
    !Number.isSafeInteger(raw.message_type) ||
    typeof raw.nsfw !== 'boolean' ||
    !Array.isArray(raw.attachment_refs) ||
    raw.attachment_refs.length > 10 ||
    raw.attachment_refs.some((item) =>
      typeof item !== 'string' ? true : parseCanonicalEntityRef(item) === null
    ) ||
    !sameCanonicalJson(raw.attachment_refs, [...new Set(raw.attachment_refs)].sort()) ||
    (raw.encryption_mode === 'plaintext') !==
      (raw.snapshot !== null && typeof raw.snapshot === 'object' && !Array.isArray(raw.snapshot))
  ) {
    throw new Error('Forward source authorization is invalid.');
  }
  return raw as unknown as PreparedForwardSource;
}

function validateForwardAuthorization(
  value: unknown,
  source: PreparedForwardSource,
  requesterRef: string,
  destinationChannelRef: string,
  destinationMode: string,
  nonce: string
): Record<string, unknown> {
  const envelope = record(value, 'Forward source proof');
  const sourceChannel = parseCanonicalEntityRef(source.channel_ref)!;
  if (
    !['event_id', 'origin', 'type', 'ts', 'actor', 'context', 'content', 'signatures'].every(
      (field) => field in envelope
    ) ||
    typeof envelope.event_id !== 'string' ||
    !/^kcfe_[A-Za-z0-9_-]{16,59}$/u.test(envelope.event_id) ||
    envelope.origin !== sourceChannel.origin_domain ||
    envelope.type !== 'message.forward.source.authorized' ||
    !Number.isSafeInteger(envelope.ts) ||
    Number(envelope.ts) < 0 ||
    !envelope.signatures ||
    typeof envelope.signatures !== 'object' ||
    Array.isArray(envelope.signatures)
  ) {
    throw new Error('Forward source proof envelope is invalid.');
  }
  const content = record(envelope.content, 'Forward source proof content');
  const context = record(envelope.context, 'Forward source proof context');
  if (
    !exactFields(content, FORWARD_PROOF_CONTENT_FIELDS) ||
    !exactFields(context, ['source_channel_ref']) ||
    context.source_channel_ref !== source.channel_ref ||
    content.version !== 1 ||
    content.requester_type !== 'human' ||
    content.requester_ref !== requesterRef ||
    content.source_message_ref !== source.message_ref ||
    content.source_channel_ref !== source.channel_ref ||
    content.destination_channel_ref !== destinationChannelRef ||
    content.destination_encryption_mode !== destinationMode ||
    content.source_encryption_mode !== source.encryption_mode ||
    content.source_projection_version !== source.projection_version ||
    content.source_projection_digest !== source.projection_digest ||
    content.source_created_at !== source.created_at ||
    content.source_edited_at !== source.edited_at ||
    content.source_flags !== source.flags ||
    content.source_message_type !== source.message_type ||
    content.source_nsfw !== source.nsfw ||
    !sameCanonicalJson(content.source_attachment_refs, source.attachment_refs) ||
    !canonicalProofStickerItems(content.source_sticker_items) ||
    !canonicalSortedCustomEmojiRefs(content.source_custom_emoji_refs) ||
    !sameCanonicalJson(content.source_snapshot, source.snapshot) ||
    content.application_ref !== null ||
    content.e2ee_device_id !== null ||
    content.nonce !== nonce ||
    !canonicalTimestamp(content.expires_at) ||
    Date.parse(content.expires_at) <= Date.now()
  ) {
    throw new Error('Forward source proof binding is invalid.');
  }
  return envelope;
}

export function validatePreparedForwardResponse(
  value: unknown,
  sourceChannelRef: string,
  sourceMessageRef: string,
  requesterRef: string,
  requested: ReadonlyMap<string, string>,
  channels: ReadonlyMap<string, Channel>
): PreparedForwardResponse {
  const raw = record(value, 'Prepared forward response');
  if (!exactFields(raw, ['source', 'destinations']) || !Array.isArray(raw.destinations)) {
    throw new Error('Prepared forward response is invalid.');
  }
  const source = validatePreparedSource(raw.source, sourceChannelRef, sourceMessageRef);
  if (raw.destinations.length !== requested.size) {
    throw new Error('Prepared forward response omitted a destination.');
  }
  const destinations = raw.destinations.map((item): PreparedForwardDestination => {
    const destination = record(item, 'Prepared forward destination');
    if (
      !exactFields(destination, [
        'channel_id',
        'client_nonce',
        'encryption_mode',
        'requires_plaintext_disclosure',
        'authorization'
      ]) ||
      typeof destination.channel_id !== 'string' ||
      !parseCanonicalEntityRef(destination.channel_id) ||
      typeof destination.client_nonce !== 'string' ||
      !FORWARD_NONCE.test(destination.client_nonce) ||
      requested.get(destination.channel_id) !== destination.client_nonce ||
      !['plaintext', 'e2ee'].includes(String(destination.encryption_mode)) ||
      (channels.get(destination.channel_id)?.encryption_mode ?? 'plaintext') !==
        destination.encryption_mode ||
      typeof destination.requires_plaintext_disclosure !== 'boolean' ||
      destination.requires_plaintext_disclosure !==
        (source.encryption_mode === 'e2ee' && destination.encryption_mode === 'plaintext') ||
      !destination.authorization ||
      typeof destination.authorization !== 'object' ||
      Array.isArray(destination.authorization)
    ) {
      throw new Error('Prepared forward destination is invalid.');
    }
    validateForwardAuthorization(
      destination.authorization,
      source,
      requesterRef,
      destination.channel_id,
      String(destination.encryption_mode),
      destination.client_nonce
    );
    return destination as unknown as PreparedForwardDestination;
  });
  if (new Set(destinations.map((item) => item.channel_id)).size !== destinations.length) {
    throw new Error('Prepared forward response duplicated a destination.');
  }
  return { source, destinations };
}

export function validateForwardMessageResult(
  value: unknown,
  sourceMessageRef: string,
  requestedDestinations: ReadonlySet<string>
): ForwardMessageResult {
  const raw = record(value, 'Forward result');
  if (
    !exactFields(raw, ['forwards', 'failures']) ||
    !Array.isArray(raw.forwards) ||
    !Array.isArray(raw.failures)
  ) {
    throw new Error('Forward result is invalid.');
  }
  const observed = new Set<string>();
  const forwards = raw.forwards.map((value) => {
    const item = record(value, 'Forward result');
    const message = record(item.message, 'Forwarded message');
    const destination = parseCanonicalEntityRef(item.destination_channel_ref);
    const messageRef = parseCanonicalEntityRef(
      `${String(message.id)}@${String(message.origin_domain)}`
    );
    const forwardedRef =
      typeof message.forwarded_message_id === 'string' &&
      typeof message.forwarded_message_domain === 'string'
        ? `${message.forwarded_message_id}@${message.forwarded_message_domain}`
        : null;
    if (
      !exactFields(item, ['destination_channel_ref', 'message']) ||
      !destination ||
      !requestedDestinations.has(String(item.destination_channel_ref)) ||
      observed.has(String(item.destination_channel_ref)) ||
      !messageRef ||
      messageRef.origin_domain !== destination.origin_domain ||
      message.channel_id !== destination.id ||
      message.channel_domain !== destination.origin_domain ||
      forwardedRef !== sourceMessageRef ||
      (message.forwarded_message_ref != null && message.forwarded_message_ref !== sourceMessageRef)
    ) {
      throw new Error('Forward result changed its requested lineage.');
    }
    observed.add(String(item.destination_channel_ref));
    return item as unknown as ForwardMessageResult['forwards'][number];
  });
  const failures = raw.failures.map((value) => {
    const item = record(value, 'Forward failure');
    const destination = parseCanonicalEntityRef(item.destination_channel_ref);
    if (
      !exactFields(item, ['destination_channel_ref', 'status', 'error']) ||
      !destination ||
      !requestedDestinations.has(String(item.destination_channel_ref)) ||
      observed.has(String(item.destination_channel_ref)) ||
      typeof item.status !== 'number' ||
      !Number.isInteger(item.status) ||
      item.status < 400 ||
      item.status > 599 ||
      !item.error ||
      typeof item.error !== 'object' ||
      Array.isArray(item.error)
    ) {
      throw new Error('Forward result changed its requested lineage.');
    }
    observed.add(String(item.destination_channel_ref));
    return item as unknown as ForwardMessageResult['failures'][number];
  });
  if (
    observed.size !== requestedDestinations.size ||
    [...requestedDestinations].some((destination) => !observed.has(destination))
  ) {
    throw new Error('Forward result omitted a requested destination.');
  }
  return { forwards, failures };
}

function attachmentBindingRef(value: unknown): string {
  const raw = record(value, 'Forward attachment binding');
  const id = raw.protocol === 'kaede-file-v1' ? raw.attachment_id : raw.id;
  const domain = raw.protocol === 'kaede-file-v1' ? raw.attachment_domain : raw.origin_domain;
  const parsed = parseCanonicalEntityRef(`${id}@${domain}`);
  if (!parsed || parsed.id !== id || parsed.origin_domain !== domain) {
    throw new Error('Forward attachment binding is invalid.');
  }
  return `${id}@${domain}`;
}

function sourceSnapshot(source: Message, prepared: PreparedForwardSource): Record<string, unknown> {
  let snapshot: Record<string, unknown>;
  if (prepared.encryption_mode === 'plaintext') {
    snapshot = validateEncryptedForwardSnapshot(prepared.snapshot);
  } else {
    const envelope = source.e2ee;
    if (
      !source.e2ee_verified ||
      !envelope ||
      envelope.forward_projection_version !== 2 ||
      envelope.forward_projection_digest !== prepared.projection_digest ||
      !Array.isArray(source.decrypted_attachments)
    ) {
      throw new Error('The encrypted source is not verified or safely forwardable.');
    }
    snapshot = validateEncryptedForwardSnapshot({
      content: source.decrypted_content ?? null,
      embeds: source.embeds ?? [],
      components: source.components ?? [],
      attachments: source.decrypted_attachments,
      mention_user_refs: [...source.mention_user_refs]
        .sort((left, right) =>
          `${left.id}@${left.origin_domain}`.localeCompare(`${right.id}@${right.origin_domain}`)
        )
        .map((item) => ({ id: item.id, origin_domain: item.origin_domain })),
      sticker_items: (source.sticker_items ?? []).map((item) => ({
        id: item.id,
        origin_domain: item.origin_domain,
        name: item.name,
        format_type: item.format_type
      })),
      message_snapshots: source.decrypted_forward_snapshot
        ? [source.decrypted_forward_snapshot]
        : [],
      message_type: source.message_type,
      flags: source.flags & ((1 << 2) | (1 << 13) | (1 << 15)),
      created_at: source.created_at,
      edited_at: source.edited_at ?? null
    });
  }
  return snapshot;
}

async function verifiedSourceSnapshot(
  source: Message,
  prepared: PreparedForwardSource
): Promise<Record<string, unknown>> {
  const snapshot = sourceSnapshot(source, prepared);
  if ((await encryptedForwardSnapshotProjectionDigest(snapshot)) !== prepared.projection_digest) {
    throw new Error('The locally verified source does not match its authority commitment.');
  }
  return snapshot;
}

function voiceMetadata(value: unknown): VoiceUploadMetadata | undefined {
  const semantics = encryptedForwardAttachmentSemantics(value);
  const duration = semantics.duration_millis;
  const waveform = semantics.waveform;
  return typeof duration === 'number' && typeof waveform === 'string'
    ? { durationSecs: duration / 1_000, waveform }
    : undefined;
}

async function sourceFiles(
  source: Message,
  snapshot: Record<string, unknown>,
  signal?: AbortSignal
): Promise<File[]> {
  const attachments = snapshot.attachments as unknown[];
  return Promise.all(
    attachments.map(async (item) => {
      const raw = record(item, 'Forward source attachment');
      const binding = attachmentBindingRef(raw);
      const projected = source.attachments?.find((attachment) => entityRef(attachment) === binding);
      let plaintext: Blob;
      if (raw.protocol === 'kaede-file-v1') {
        plaintext = await decryptEncryptedAttachment(
          raw as unknown as EncryptedFileManifest,
          projected?.history_media_url,
          projected?.private_media_url
        );
      } else {
        if (!projected) throw new Error('Forward source attachment is unavailable.');
        plaintext = await authenticatedMediaBlob(
          {
            path: attachmentMediaPath(
              projected.origin_domain,
              projected.id,
              'original',
              projected.history_media_url,
              projected.private_media_url
            ),
            contentType: projected.content_type
          },
          projected.size,
          signal
        );
      }
      const semantics = encryptedForwardAttachmentSemantics(raw);
      if (plaintext.size !== semantics.plaintext_size) {
        throw new Error('Forward source attachment size was modified.');
      }
      const bytes = new Uint8Array(await plaintext.arrayBuffer());
      const digest = await sha256(bytes);
      try {
        if (base64url(digest) !== semantics.plaintext_sha256) {
          throw new Error('Forward source attachment content was modified.');
        }
      } finally {
        clearBytes(bytes);
        clearBytes(digest);
      }
      return new File([plaintext], String(semantics.filename), {
        type: String(semantics.content_type)
      });
    })
  );
}

export function rebindForwardSnapshot(
  source: Record<string, unknown>,
  replacements: readonly unknown[]
): Record<string, unknown> {
  const snapshot = structuredClone(validateEncryptedForwardSnapshot(source));
  const originalAttachments = snapshot.attachments as unknown[];
  if (originalAttachments.length !== replacements.length) {
    throw new Error('Forward destination attachment count is invalid.');
  }
  const replacementRefs = replacements.map(attachmentBindingRef);
  const originalRefs = originalAttachments.map(attachmentBindingRef);
  if (
    new Set(replacementRefs).size !== replacementRefs.length ||
    replacementRefs.some((ref) => originalRefs.includes(ref))
  ) {
    throw new Error('Forward destination attachments were not freshly rebound.');
  }
  for (let index = 0; index < replacements.length; index += 1) {
    const original = record(originalAttachments[index], 'Forward source attachment');
    const replacement = record(replacements[index], 'Forward destination attachment');
    if (
      !sameCanonicalJson(
        encryptedForwardAttachmentSemantics(original),
        encryptedForwardAttachmentSemantics(replacement)
      ) ||
      (original.protocol === 'kaede-file-v1' &&
        replacement.protocol === 'kaede-file-v1' &&
        ['file_id', 'key', 'ciphertext_sha256'].some(
          (field) => original[field] === replacement[field]
        ))
    ) {
      throw new Error('Forward destination attachment is not a fresh semantic copy.');
    }
  }
  const sourceIndex = new Map(
    originalAttachments.map((item, index) => [attachmentBindingRef(item), index])
  );
  if (sourceIndex.size !== originalAttachments.length) {
    throw new Error('Forward source attachment bindings are ambiguous.');
  }
  const nested = snapshot.message_snapshots as Record<string, unknown>[];
  if (nested.length) {
    const nestedAttachments = nested[0].attachments as unknown[];
    const used = new Set<number>();
    nested[0].attachments = nestedAttachments.map((item) => {
      const index = sourceIndex.get(attachmentBindingRef(item));
      if (
        index === undefined ||
        used.has(index) ||
        !sameCanonicalJson(
          encryptedForwardAttachmentSemantics(item),
          encryptedForwardAttachmentSemantics(originalAttachments[index])
        )
      ) {
        throw new Error('Nested forward attachment binding is invalid.');
      }
      used.add(index);
      return replacements[index];
    });
    if (used.size !== originalAttachments.length) {
      throw new Error('Nested forward attachments do not cover the source files.');
    }
  }
  snapshot.attachments = [...replacements];
  return validateEncryptedForwardSnapshot(snapshot);
}

function plaintextSnapshotAttachment(
  ticket: UploadTicket,
  file: File,
  sourceAttachment: unknown
): Record<string, unknown> {
  const semantics = encryptedForwardAttachmentSemantics(sourceAttachment);
  const voice = voiceMetadata(sourceAttachment);
  return {
    id: ticket.id,
    origin_domain: ticket.origin_domain,
    filename: file.name,
    content_type: file.type || 'application/octet-stream',
    size: file.size,
    plaintext_sha256: semantics.plaintext_sha256,
    width: null,
    height: null,
    ...(voice ? { duration_secs: voice.durationSecs, waveform: voice.waveform } : {}),
    blurhash: null,
    scan_status: 'pending',
    encryption_mode: 'plaintext',
    encryption_protocol: null,
    variants: {}
  };
}

async function destinationMessage(
  destination: PreparedForwardDestination,
  channel: Channel,
  source: PreparedForwardSource,
  snapshot: Record<string, unknown>,
  files: readonly File[],
  note: string,
  client: KaedeE2EEClient | null | undefined,
  signal?: AbortSignal
): Promise<Record<string, unknown>> {
  const sourceAttachments = snapshot.attachments as unknown[];
  const needsCopies = preparedForwardNeedsCopies(
    source.encryption_mode,
    [destination.encryption_mode],
    sourceAttachments.length
  );
  let attachmentIds: string[] = [];
  let rebound: Record<string, unknown> | null = null;
  let encryptedManifests: EncryptedFileManifest[] = [];
  if (needsCopies) {
    if (destination.encryption_mode === 'e2ee') {
      const uploads = [];
      // Rich-v2 canonicalizes attachment refs while the source commitment
      // preserves semantic order. Sequential tickets keep both orders equal.
      for (let index = 0; index < files.length; index += 1) {
        uploads.push(
          await uploadEncryptedChannelFile(
            destination.channel_id,
            files[index],
            () => {},
            signal,
            voiceMetadata(sourceAttachments[index])
          )
        );
      }
      attachmentIds = uploads.map((item) => item.ticket.id);
      encryptedManifests = uploads.map((item) => item.manifest);
      rebound = rebindForwardSnapshot(snapshot, encryptedManifests);
    } else {
      const tickets = [];
      for (let index = 0; index < files.length; index += 1) {
        tickets.push(
          await uploadChannelFile(
            destination.channel_id,
            files[index],
            () => {},
            signal,
            voiceMetadata(sourceAttachments[index])
          )
        );
      }
      attachmentIds = tickets.map((item) => item.id);
      rebound = rebindForwardSnapshot(
        snapshot,
        tickets.map((ticket, index) =>
          plaintextSnapshotAttachment(ticket, files[index], sourceAttachments[index])
        )
      );
    }
    if ((await encryptedForwardSnapshotProjectionDigest(rebound)) !== source.projection_digest) {
      throw new Error('Forward destination files do not match the source commitment.');
    }
  }
  const common: Record<string, unknown> = {
    attachment_ids: attachmentIds,
    forwarded_message_id: source.message_ref,
    forward_source_proof: destination.authorization,
    client_nonce: destination.client_nonce
  };
  if (destination.encryption_mode === 'plaintext') {
    return {
      ...common,
      ...(note ? { content: note } : {}),
      ...(source.encryption_mode === 'e2ee' ? { forward_snapshot: rebound } : {})
    };
  }
  if (!client || !rebound) {
    throw new Error('Encryption is unavailable for a selected forward destination.');
  }
  const e2ee = await client.encryptMessage(channel, note, {
    attachments: encryptedManifests,
    rich: {
      forward: {
        snapshot: rebound,
        sourceProjectionDigest: source.projection_digest,
        sourceMessageRef: source.message_ref,
        sourceChannelRef: source.channel_ref,
        sourceCreatedAt: source.created_at,
        sourceEditedAt: source.edited_at,
        sourceFlags: source.flags,
        sourceMessageType: source.message_type
      }
    }
  });
  return { ...common, e2ee };
}

export async function executePreparedForward({
  source,
  sourceChannel,
  destinations,
  requesterRef,
  note = '',
  e2eeClient,
  signal
}: PreparedForwardOptions): Promise<ForwardMessageResult> {
  if (!destinations.length || destinations.length > 5) {
    throw new Error('Select between one and five forward destinations.');
  }
  const sourceChannelRef = entityRef(sourceChannel);
  const sourceMessageRef = entityRef(source);
  if (!parseCanonicalEntityRef(requesterRef)) {
    throw new Error('The forwarding requester identity is invalid.');
  }
  const channels = new Map(destinations.map((channel) => [entityRef(channel), channel]));
  if (channels.size !== destinations.length) {
    throw new Error('Forward destinations must be unique.');
  }
  const requests = destinations.map((channel) => ({
    channel_id: entityRef(channel),
    client_nonce: `forward-${crypto.randomUUID()}`
  }));
  const requested = new Map(requests.map((item) => [item.channel_id, item.client_nonce]));
  const rawPrepared = await prepareMessageForward(sourceChannelRef, sourceMessageRef, requests);
  const prepared = validatePreparedForwardResponse(
    rawPrepared,
    sourceChannelRef,
    sourceMessageRef,
    requesterRef,
    requested,
    channels
  );
  if (
    prepared.source.encryption_mode !==
    (sourceChannel.encryption_mode === 'e2ee' ? 'e2ee' : 'plaintext')
  ) {
    throw new Error('Forward source encryption mode changed during preparation.');
  }
  const snapshot = await verifiedSourceSnapshot(source, prepared.source);
  const needsCopies = preparedForwardNeedsCopies(
    prepared.source.encryption_mode,
    prepared.destinations.map((item) => item.encryption_mode),
    (snapshot.attachments as unknown[]).length
  );
  const files = needsCopies ? await sourceFiles(source, snapshot, signal) : [];
  const trimmedNote = note.trim();
  const messages = await Promise.all(
    prepared.destinations.map(async (destination) => {
      const channel = channels.get(destination.channel_id)!;
      return {
        destination_channel_id: destination.channel_id,
        message: await destinationMessage(
          destination,
          channel,
          prepared.source,
          snapshot,
          files,
          trimmedNote,
          e2eeClient,
          signal
        )
      };
    })
  );
  // Proofs expire after 90 seconds, while downloading and freshly encrypting
  // several large files may legitimately take longer. Refresh with the same
  // idempotency nonces immediately before submit and fail if any source or
  // destination binding changed while the client prepared ciphertext.
  const refreshed = validatePreparedForwardResponse(
    await prepareMessageForward(sourceChannelRef, sourceMessageRef, requests),
    sourceChannelRef,
    sourceMessageRef,
    requesterRef,
    requested,
    channels
  );
  if (!samePreparedSource(prepared.source, refreshed.source)) {
    throw new Error('The forward source changed while files were prepared.');
  }
  const refreshedByChannel = new Map(
    refreshed.destinations.map((item) => [item.channel_id, item] as const)
  );
  const refreshedMessages = messages.map(({ destination_channel_id, message }) => {
    const destination = refreshedByChannel.get(destination_channel_id);
    if (!destination) throw new Error('A forward destination changed during preparation.');
    return {
      destination_channel_id,
      message: { ...message, forward_source_proof: destination.authorization }
    };
  });
  return validateForwardMessageResult(
    await submitPreparedMessageForward(sourceChannelRef, sourceMessageRef, refreshedMessages),
    sourceMessageRef,
    new Set(requested.keys())
  );
}
