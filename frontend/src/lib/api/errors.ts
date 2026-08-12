export interface ApiErrorIssue {
  location?: unknown;
  message?: unknown;
  loc?: unknown;
  msg?: unknown;
  type?: unknown;
}

const ERROR_MESSAGES: Record<string, string> = {
  ADMIN_AUTHENTICATION_REQUIRED: 'Sign in with an administrator account to continue.',
  AUTHENTICATION_REQUIRED: 'Your session has expired. Sign in again to continue.',
  CSRF_GUARD: 'This page is out of date. Reload it and try again.',
  MISSING_PERMISSIONS: "You don't have permission to do that.",
  CANNOT_MANAGE_PERMISSIONS: "You can't change those permissions.",
  CANNOT_GRANT_PERMISSIONS: "You can't grant permissions you don't have.",
  ROLE_HIERARCHY: 'That member or role is higher than your highest role.',
  OWNER_IMMUNE: "The guild owner can't be moderated or have their roles changed.",
  CANNOT_MANAGE_SELF: "You can't use that action on yourself.",
  GUILD_OWNER_REQUIRED: 'Only the guild owner can do that.',
  NOT_A_GUILD_MEMBER: 'You are no longer a member of this guild.',
  BANNED_FROM_GUILD: 'You cannot join this guild because you are banned.',
  INSTANCE_BANNED_FROM_GUILD: 'Your home instance is blocked from this guild.',
  GUILD_NOT_FOUND: 'That guild no longer exists or you no longer have access to it.',
  CHANNEL_NOT_FOUND: 'That channel no longer exists or you no longer have access to it.',
  MESSAGE_NOT_FOUND: 'That message no longer exists or you no longer have access to it.',
  USER_NOT_FOUND: 'That user could not be found.',
  MEMBER_NOT_FOUND: 'That member is no longer in the guild.',
  GUILD_MEMBER_NOT_FOUND: 'That member is no longer in the guild.',
  ROLE_NOT_FOUND: 'That role no longer exists.',
  INVITE_NOT_FOUND: 'That invite is invalid, expired, or no longer available.',
  FRIEND_REQUEST_NOT_FOUND: 'That friend request is no longer available.',
  EMOJI_NOT_FOUND: 'That custom emoji no longer exists.',
  WEBHOOK_NOT_FOUND: 'That webhook no longer exists.',
  ATTACHMENT_NOT_FOUND: 'That attachment is no longer available.',
  MEDIA_NOT_FOUND: 'That media is no longer available.',
  KAED_MEDIA_NOT_FOUND: 'That media is no longer available.',
  CANNOT_DM_SELF: "You can't start a direct message with yourself.",
  CANNOT_DM_USER: 'This user is not accepting direct messages from you.',
  DM_PRIVACY_REJECTED: 'This user’s privacy settings do not allow this direct message.',
  CANNOT_FRIEND_SELF: "You can't send a friend request to yourself.",
  CANNOT_BLOCK_SELF: "You can't block yourself.",
  RELATIONSHIP_BLOCKED: 'This action is unavailable because one of you has blocked the other.',
  RELATIONSHIP_CONFLICT: 'That relationship changed. Refresh and try again.',
  ROLE_ORDER_INCOMPLETE:
    'The role list changed while you were reordering it. Reload and try again.',
  ROLE_ORDER_NOT_CONTIGUOUS: 'The role order is invalid. Reload and try again.',
  ROLE_POSITION_BATCH_REQUIRED: 'Reorder roles by dragging them in the role list.',
  ROLE_STATE_CHANGED: 'The roles changed somewhere else. Reload and try again.',
  CHANNEL_SET_CHANGED: 'The channel list changed somewhere else. Reload and try again.',
  SETTINGS_VERSION_CONFLICT: 'Your settings changed somewhere else. Reload and try again.',
  SETTINGS_VERSION_REQUIRED: 'Reload your settings before saving this change.',
  TARGET_CANNOT_CONNECT: "That member doesn't have permission to join this voice channel.",
  VOICE_NOT_CONNECTED: 'That member is no longer connected to voice.',
  VOICE_DISABLED: 'Voice is disabled on this instance.',
  VOICE_HOME_UNREACHABLE: 'The voice server is temporarily unavailable.',
  VOICE_CHANNEL_NOT_FOUND: 'That voice channel is no longer available.',
  VOICE_DENIED: "You don't have permission to join that voice channel.",
  CALL_NOT_FOUND: 'That call is no longer available.',
  CALL_REJECTED: 'The other person declined the call.',
  CALL_NOT_ACCEPTED: 'Accept the call before using its controls.',
  CALL_ALREADY_ACTIVE: 'A call is already active in this conversation.',
  SLOWMODE_RATE_LIMITED: 'Slow mode is active.',
  RATE_LIMITED: 'You are doing that too quickly.',
  KAED_RATE_LIMITED: 'The remote server is receiving too many requests.',
  LOGIN_RATE_LIMITED: 'There have been too many sign-in attempts.',
  MFA_RATE_LIMITED: 'There have been too many verification attempts.',
  WEBHOOK_RATE_LIMITED: 'This webhook is sending requests too quickly.',
  USE_EXTERNAL_EMOJIS_REQUIRED: "You don't have permission to use emoji from another guild here.",
  CUSTOM_EMOJI_SOURCE_ACCESS_REQUIRED: 'You no longer have access to that custom emoji.',
  CUSTOM_EMOJI_NOT_FOUND: 'That custom emoji no longer exists.',
  CUSTOM_EMOJI_INVALID: 'That custom emoji reference is invalid.',
  EMOJI_LIMIT_REACHED: 'This guild has reached its custom emoji limit.',
  EMOJI_NAME_TAKEN: 'This guild already has an emoji with that name.',
  EMOJI_TOO_LARGE: 'That emoji image exceeds this instance’s size limit.',
  ROLE_NOT_MENTIONABLE: 'That role cannot be mentioned.',
  INVALID_ROLE_MENTION: 'That role mention is no longer valid.',
  TOO_MANY_ROLE_MENTIONS: 'A message can mention at most 25 roles.',
  ROLE_MENTION_TOO_LARGE: 'That role mention would notify too many members.',
  ATTACHMENT_ALREADY_USED: 'That attachment was already sent. Attach the file again and retry.',
  ASSET_ALREADY_USED: 'That image was already used. Choose the file again and retry.',
  ATTACHMENT_NOT_OWNED: "You can't use an attachment uploaded by another account.",
  ATTACHMENT_PURPOSE_MISMATCH: 'That file was uploaded for a different purpose. Choose it again.',
  ATTACHMENT_TOO_LARGE: 'That attachment exceeds this instance’s size limit.',
  IMAGE_ASSET_TYPE_REQUIRED: 'Choose a PNG, JPEG, GIF, or WebP image.',
  UPLOAD_INCOMPLETE: 'The upload did not finish. Choose the file again and retry.',
  UPLOAD_SIZE_MISMATCH: 'The uploaded file size did not match the selected file. Choose it again.',
  UPLOAD_TYPE_MISMATCH: 'The uploaded file type did not match the selected file. Choose it again.',
  UPLOAD_TICKET_EXPIRED: 'The upload took too long. Choose the file again and retry.',
  UPLOAD_INFLIGHT_LIMIT: 'Too many files are uploading at once. Wait for one to finish.',
  UPLOAD_INFLIGHT_QUOTA_EXCEEDED: 'Your pending uploads exceed this instance’s storage limit.',
  USER_STORAGE_QUOTA_EXCEEDED: 'Your account has reached its attachment storage limit.',
  MEDIA_STORAGE_UNAVAILABLE: 'Media storage is temporarily unavailable.',
  MEDIA_NOT_AVAILABLE: 'That media is still processing or is no longer available.',
  REMOTE_MEDIA_BUSY: 'The remote server is still preparing that media.',
  REMOTE_MEDIA_CACHE_FULL:
    'This instance’s remote-media cache is full. Kaede is clearing older cached files.',
  REMOTE_MEDIA_REJECTED: 'The remote server rejected that media.',
  REMOTE_MEDIA_UNAVAILABLE: 'The remote media server is temporarily unavailable.',
  KAED_FED_INBOX_QUOTA_EXCEEDED:
    'This instance is temporarily at its retained federation-event limit. The remote server will retry automatically.',
  FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED:
    'This instance cannot cache another remote account right now. Contact your instance administrator if this continues.',
  FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED:
    'This instance cannot cache another remote server right now. Contact your instance administrator if this continues.',
  FEDERATION_OUTBOX_CAPACITY_EXCEEDED:
    'This instance’s delivery queue for that remote server is full. Nothing was saved; wait for queued federation work to clear and try again.',
  KAED_FED_IDENTITY_STORAGE_QUOTA_EXCEEDED:
    'The receiving instance cannot retain another remote account right now. This operation was not completed.',
  KAED_FED_INSTANCE_STORAGE_QUOTA_EXCEEDED:
    'The receiving instance cannot retain another remote server right now. This operation was not completed.',
  KAED_FED_OUTBOX_CAPACITY_EXCEEDED:
    'The receiving instance’s outbound federation queue is full. Kaede will retry automatically after queued work clears.',
  KAED_FED_RELATIONSHIP_REQUEST_QUOTA_EXCEEDED:
    'The receiving instance cannot accept another pending friend request right now. Your request was not delivered.',
  KAED_FED_REPLICA_QUOTA_EXCEEDED:
    'This guild’s local replica reached its cache limit. New messages and changes may be missing until your instance frees space.',
  KAED_FED_HISTORY_CAPACITY:
    'This instance is already importing the maximum amount of remote message history. The import will be retried automatically.',
  FEDERATED_GUILD_HISTORY_TEMPORARILY_UNAVAILABLE:
    'Older guild messages are temporarily unavailable. Kaede will retry automatically; recent messages and new activity remain available.',
  FEDERATED_GUILD_HISTORY_LIMIT_REACHED:
    'This instance reached its configured limit for cached guild history. Recent messages and new activity remain available; contact your instance administrator if you need older history.',
  FEDERATED_GUILD_HISTORY_REJECTED:
    'Older messages from this guild’s home instance could not be safely imported. Recent messages and new activity remain available.',
  FEDERATED_DM_STORAGE_QUOTA_EXCEEDED:
    'This instance could not retain more direct-message data. Recent remote messages are normally kept by removing the oldest cached copies; if this persists, contact your instance administrator.',
  KAED_FED_DM_STORAGE_QUOTA_EXCEEDED:
    'The receiving instance could not retain more direct-message data. Delivery cannot continue until it frees space or raises its limit.',
  KAED_FED_DELIVERY_EXPIRED:
    'The remote instance did not accept this operation before the delivery window ended. Try the operation again later.',
  KAED_FED_EVENT_TOO_LARGE:
    'This operation is too large to send between instances. Reduce its size and try again.',
  FEDERATED_DM_HISTORY_TRUNCATED:
    'This instance keeps recent messages here and loads older messages from their home instance as you scroll.',
  FEDERATED_DM_HISTORY_UNAVAILABLE:
    'Older messages could not be loaded from their home instance right now. Your recent messages are still available; try again in a moment.',
  FEDERATED_MODERATION_STATUS_INVALID:
    'The guild’s home instance returned invalid timeout details. Sending is still checked by the guild home.',
  FEDERATED_MODERATION_STATUS_UNAVAILABLE:
    'Your timeout details are temporarily unavailable from the guild’s home instance. Sending is still checked by the guild home.',
  SEARCH_DISABLED_FOR_E2EE:
    'Search is unavailable in end-to-end encrypted conversations because the server cannot read or index their contents.',
  SEARCH_DISABLED_BY_INSTANCE: 'Message search is disabled by this instance’s administrator.',
  SEARCH_UNAVAILABLE: 'Message search is temporarily unavailable. Try again shortly.',
  INVALID_SEARCH_CURSOR: 'That search page expired. Run the search again.',
  FEDERATED_SEARCH_RESPONSE_INVALID:
    'The other server returned an invalid search response. Locally cached messages may still be available.',
  FEDERATION_UNAVAILABLE: 'The remote Kaede server is temporarily unavailable.',
  FEDERATED_WRITE_UNAVAILABLE: 'The remote Kaede server could not save that change.',
  FEDERATION_LOOKUP_RATE_LIMITED: 'The remote Kaede server is receiving too many requests.',
  FEDERATION_INVITE_PREVIEW_BUSY: 'The remote server is still checking that invite.',
  GIF_PICKER_DISABLED: 'The GIF picker is disabled on this instance.',
  GIF_PROVIDER_UNAVAILABLE: 'The GIF provider is temporarily unavailable.',
  INTERNAL_SERVER_ERROR:
    'The server encountered an unexpected problem and could not complete this request.',
  INVALID_SERVER_RESPONSE:
    'Kaede received a server response it could not understand. Reload and try again; if it continues, update Kaede or contact your administrator.'
};

const GENERIC_SERVER_MESSAGES = new Set([
  'bad request',
  'unauthorized',
  'forbidden',
  'not found',
  'conflict',
  'unprocessable entity',
  'too many requests',
  'internal server error',
  'bad gateway',
  'service unavailable',
  'gateway timeout',
  'request failed',
  'request validation failed'
]);

const TECHNICAL_MESSAGE =
  /(?:traceback|stack trace|\bexception\b|axioserror|request failed with status code\s+\d+|sqlalchemy|postgres|redis|errno|\bselect\b.+\bfrom\b|\binsert\s+into\b|cannot read (?:properties|property) of|\bis not (?:a function|iterable|defined)\b|unexpected token|json at position|failed to execute ['"]|\/home\/|\/var\/|[A-Z]:\\|-----BEGIN|private[_ -]?key|authorization:|bearer\s+[A-Za-z0-9._~-]+|(?:token|secret|password|signature|credential|api[_-]?key)\s*=|https?:\/\/[^\s/@]+:[^\s/@]+@|[?&](?:token|key|secret|password|signature|credential|authorization)=[^\s&#]+)/i;

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : {};
}

function safeReference(value: unknown): string | null {
  return typeof value === 'string' && /^[A-Za-z0-9._-]{1,64}$/.test(value) ? value : null;
}

function retryDelay(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 86_400_000
    ? Math.round(value)
    : null;
}

function byteLimit(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0
    ? Math.min(value, Number.MAX_SAFE_INTEGER)
    : null;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} byte${bytes === 1 ? '' : 's'}`;
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KiB`;
  if (bytes < 1024 * 1024 * 1024) {
    const mib = bytes / (1024 * 1024);
    return `${Number.isInteger(mib) ? mib : mib.toFixed(1)} MiB`;
  }
  const gib = bytes / (1024 * 1024 * 1024);
  return `${Number.isInteger(gib) ? gib : gib.toFixed(1)} GiB`;
}

function suppliedMessage(detail: Record<string, unknown>, status: number): string | null {
  const supplied = typeof detail.message === 'string' ? detail.message.trim() : '';
  if (!supplied || supplied.length > 500) return null;
  if (status >= 500 || GENERIC_SERVER_MESSAGES.has(supplied.toLocaleLowerCase())) return null;
  if (TECHNICAL_MESSAGE.test(supplied) || /[\r\n\t]/.test(supplied)) return null;
  return supplied;
}

function sentence(value: string): string {
  if (!value) return value;
  const normalized = value[0].toLocaleUpperCase() + value.slice(1);
  return /[.!?]$/.test(normalized) ? normalized : `${normalized}.`;
}

function humanField(value: string): string {
  const last = value.split('.').filter(Boolean).at(-1) ?? 'value';
  return last.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toLocaleUpperCase());
}

function validationMessage(detail: Record<string, unknown>): string | null {
  if (!Array.isArray(detail.errors) || !detail.errors.length) return null;
  const issue = asRecord(detail.errors[0]);
  const issueLocation = issue.location ?? issue.loc;
  const location = Array.isArray(issueLocation)
    ? issueLocation
        .filter((part): part is string => typeof part === 'string')
        .filter((part) => !['body', 'query', 'path', 'header', 'cookie', '__root__'].includes(part))
        .join('.')
    : '';
  const issueMessage = issue.message ?? issue.msg;
  const rawMessage = typeof issueMessage === 'string' ? issueMessage.trim() : '';
  if (!location || !rawMessage || TECHNICAL_MESSAGE.test(rawMessage)) return null;
  const friendly = rawMessage
    .replace(/^field required$/i, 'This field is required')
    .replace(/^value is not a valid /i, 'Enter a valid ');
  const field = humanField(location);
  return `Check ${field}: ${sentence(friendly)}`;
}

function statusMessage(status: number): string {
  if (status === 400)
    return 'That request could not be completed. Check the information and try again.';
  if (status === 401) return 'Your session has expired. Sign in again to continue.';
  if (status === 403) return "You don't have permission to do that.";
  if (status === 404) return 'That item no longer exists or you no longer have access to it.';
  if (status === 405) return 'That action is not supported here.';
  if (status === 408 || status === 504) return 'The server took too long to respond.';
  if (status === 409) return 'That information changed somewhere else. Reload and try again.';
  if (status === 413)
    return 'The selected data is too large. Choose something smaller and try again.';
  if (status === 415) return 'That file or data type is not supported.';
  if (status === 422) return 'Some information is missing or invalid. Check it and try again.';
  if (status === 429) return 'You are doing that too quickly.';
  if (status === 502) return 'A remote server returned an invalid response.';
  if (status === 503) return 'The service is temporarily unavailable.';
  if (status === 507) return 'This server does not currently have enough storage capacity.';
  if (status >= 500)
    return 'The server encountered an unexpected problem and could not complete this request.';
  return 'The request could not be completed.';
}

function retryGuidance(status: number, code: string, retryAfterMs: number | null): string {
  const shouldRetry =
    status === 408 ||
    status === 429 ||
    (status >= 500 && status !== 507) ||
    code.endsWith('_UNAVAILABLE') ||
    code.endsWith('_BUSY') ||
    code.endsWith('_RATE_LIMITED');
  if (!shouldRetry) return '';
  if (retryAfterMs === null || retryAfterMs < 1000) return 'Try again shortly.';
  const seconds = Math.max(1, Math.ceil(retryAfterMs / 1000));
  if (seconds < 60) return `Try again in ${seconds} second${seconds === 1 ? '' : 's'}.`;
  const minutes = Math.ceil(seconds / 60);
  return `Try again in ${minutes} minute${minutes === 1 ? '' : 's'}.`;
}

export function apiErrorMessage(
  code: string,
  status: number,
  detail: Record<string, unknown>
): string {
  const retryAfterMs = retryDelay(detail.retry_after_ms);
  const validation = code === 'VALIDATION_ERROR' ? validationMessage(detail) : null;
  const configured = ERROR_MESSAGES[code];
  const supplied = suppliedMessage(detail, status);
  const unknown = validation === null && configured === undefined && supplied === null;
  const core = validation ?? configured ?? supplied ?? statusMessage(status);
  const retry = retryGuidance(status, code, retryAfterMs);
  const withRetry =
    retry && !/try again/i.test(core) ? `${sentence(core)} ${retry}` : sentence(core);
  const maxBytes = byteLimit(detail.max_bytes);
  const withLimit =
    maxBytes !== null && !withRetry.includes(formatBytes(maxBytes))
      ? `${withRetry} The maximum allowed size is ${formatBytes(maxBytes)}.`
      : withRetry;
  const traceId = safeReference(detail.trace_id);
  return (status >= 500 || unknown) && traceId
    ? `${withLimit} Error reference: ${traceId}.`
    : withLimit;
}

/** Formats wording produced locally by the trusted native bridge, not an HTTP response body. */
export function trustedClientErrorMessage(
  message: string,
  code: string,
  status: number,
  detail: Record<string, unknown>
): string {
  const trimmed = message.trim();
  if (!trimmed || trimmed.length > 500 || /[\r\n\t]/.test(trimmed)) {
    return apiErrorMessage(code, status, detail);
  }
  const retry = retryGuidance(status, code, retryDelay(detail.retry_after_ms));
  const withRetry =
    retry && !/try again/i.test(trimmed) ? `${sentence(trimmed)} ${retry}` : sentence(trimmed);
  const traceId = safeReference(detail.trace_id);
  return traceId ? `${withRetry} Error reference: ${traceId}.` : withRetry;
}

export class ApiError extends Error {
  readonly traceId: string | null;
  readonly retryAfterMs: number | null;

  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
    readonly detail: Record<string, unknown> = {}
  ) {
    super(message);
    this.name = 'ApiError';
    this.traceId = safeReference(detail.trace_id);
    this.retryAfterMs = retryDelay(detail.retry_after_ms);
  }
}

function looksLikeNetworkFailure(message: string): boolean {
  return /(?:failed to fetch|networkerror|network request failed|load failed|connection refused|connection reset|dns|offline)/i.test(
    message
  );
}

function looksLikeTimeout(error: Error): boolean {
  return error.name === 'TimeoutError' || /(?:timed? out|timeout)/i.test(error.message);
}

/** Converts transport and runtime failures into safe text for an end user. */
export function userErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) return error.message;
  if (!(error instanceof Error)) {
    const message = asRecord(error).message;
    return typeof message === 'string' ? userErrorMessage(new Error(message), fallback) : fallback;
  }
  if (error.name === 'AbortError') return 'The request was cancelled. Try again.';
  if (looksLikeTimeout(error))
    return 'The request took too long. Check your connection and try again.';
  if (looksLikeNetworkFailure(error.message)) {
    return 'Could not reach the server. Check your connection and try again.';
  }
  const message = error.message.trim();
  if (
    !message ||
    message.length > 300 ||
    /^[A-Z][A-Z0-9_]{2,}$/.test(message) ||
    TECHNICAL_MESSAGE.test(message) ||
    /[\r\n\t]/.test(message)
  ) {
    return fallback;
  }
  return sentence(message);
}

export function normalizeErrorDetail(value: unknown): Record<string, unknown> {
  const body = asRecord(value);
  const nested = body.detail;
  if (typeof nested === 'object' && nested !== null) return asRecord(nested);
  if (typeof nested === 'string') return { message: nested };
  return body;
}

export function validTraceId(value: unknown): string | null {
  return safeReference(value);
}

export function validRetryAfterMs(value: unknown): number | null {
  return retryDelay(value);
}
