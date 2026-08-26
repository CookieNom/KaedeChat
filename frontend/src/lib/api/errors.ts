export interface ApiErrorIssue {
  location?: unknown;
  message?: unknown;
  loc?: unknown;
  msg?: unknown;
  type?: unknown;
}

const ERROR_MESSAGES: Record<string, string> = {
  ADMIN_AUTHENTICATION_REQUIRED: 'Sign in with an administrator account to continue.',
  ADMIN_GRANT_NOT_FOUND: 'That administrator grant no longer exists.',
  APPLICATION_NOT_FOUND: 'That application no longer exists or you cannot manage it.',
  APPLICATION_PERMISSION_DENIED: "You don't have permission to manage that application.",
  BOT_ALREADY_INSTALLED: 'That bot is already installed in this guild.',
  BOT_CONTROL_AUTH_REQUIRED: 'Provide an application control credential for this operation.',
  BOT_CONTROL_SCOPE_REQUIRED: 'That control credential does not allow this operation.',
  BOT_CONTROL_TOKEN_INVALID: 'That application control credential is invalid, expired, or revoked.',
  BOT_CREDENTIAL_NOT_FOUND: 'That application credential no longer exists.',
  BOT_DM_ATTACHMENTS_UNAVAILABLE:
    'Bot file uploads are currently available only through a guild installation.',
  BOT_E2EE_CONTENT_UNAVAILABLE:
    'This bot cannot read plaintext from an end-to-end encrypted channel.',
  BOT_E2EE_DISABLED: 'This bot is not enabled for encrypted interactions in this guild.',
  BOT_E2EE_ENVELOPE_REQUIRED:
    'Send an encrypted envelope when this bot posts in an end-to-end encrypted channel.',
  BOT_INSTANCE_BLOCKED: 'This bot’s home instance is blocked by the target instance.',
  BOT_INVITE_NOT_FOUND: 'That bot invitation is invalid, disabled, or no longer available.',
  BOT_NOT_INSTALLED: 'That bot is not installed in this guild.',
  BOT_SCOPE_REQUIRED: 'This bot was not granted the API scope required for that action.',
  BOT_TARGET_NOT_DELEGATED: 'This worker is not authorized to connect to that instance.',
  BOT_WORKER_NOT_FOUND: 'That bot worker is invalid, expired, or revoked.',
  COMMAND_NAME_DUPLICATE: 'Each application command must have a unique name and type.',
  COMMAND_SET_TOO_LARGE: 'This command set has too many commands or options.',
  DEVELOPER_TEAM_NOT_FOUND: 'That developer team no longer exists or you cannot access it.',
  E2EE_INTERACTION_PAYLOAD_REQUIRED:
    'This encrypted channel requires an encrypted command payload.',
  E2EE_ACTIVATION_DISABLED: 'New end-to-end encrypted rooms are not enabled on this instance yet.',
  E2EE_MEMBERSHIP_COMMIT_REQUIRED:
    'Secure the updated member list on an approved device before changing this encrypted group.',
  E2EE_NOT_ENABLED: 'This conversation does not accept encrypted message envelopes.',
  E2EE_ENVELOPE_REQUIRED: 'This conversation requires an end-to-end encrypted message.',
  E2EE_MLS_ENVELOPE_REQUIRED: 'This conversation requires a current MLS 1.0 encrypted message.',
  E2EE_POLICY_CONTEXT_MISMATCH:
    'The encrypted message was created for a different room policy or MLS epoch. Refresh encryption state and try again.',
  E2EE_ATTACHMENTS_NOT_READY:
    'Encrypted attachment support is unavailable on this client. No file was uploaded as plaintext.',
  E2EE_DEVICE_CHALLENGE_EXPIRED: 'The device setup request expired. Start device setup again.',
  E2EE_DEVICE_CHALLENGE_MISMATCH:
    'The device setup request does not match this account or session.',
  E2EE_DEVICE_PROOF_INVALID: 'The device could not prove that it owns its encryption identity key.',
  E2EE_DEVICE_REVOKED: 'This encryption device was revoked and cannot be registered again.',
  E2EE_DEVICE_IDENTITY_CONFLICT:
    'This encryption identity is already bound to different device credentials.',
  E2EE_DEVICE_LIMIT_REACHED: 'This account has reached its active encryption-device limit.',
  E2EE_DEVICE_NOT_FOUND: 'The encryption device was not found.',
  E2EE_RECOVERY_AUTHORIZATION_REQUIRED:
    'The one-time encryption-recovery authorization expired or was already used. Select the backup and confirm the restore again.',
  E2EE_ACCOUNT_VAULT_BUSY:
    'Your encrypted account vault is busy on another device. Wait a moment and try again.',
  E2EE_ACCOUNT_VAULT_CONTEXT_MISMATCH:
    'The encrypted-room update is not bound to the current account vault. Sign in again and retry.',
  E2EE_ACCOUNT_VAULT_LEASE_EXPIRED:
    'The encrypted account-vault lock expired before this update completed. Try again.',
  E2EE_ACCOUNT_VAULT_REVISION_CONFLICT:
    'Your encrypted account vault changed on another device. Refresh before trying again.',
  E2EE_ACCOUNT_VAULT_ATTESTATION_REQUIRED:
    'Your home server could not attest the encrypted account state for this room update.',
  E2EE_SENDER_DEVICE_INVALID:
    'This encrypted message was not created by one of your active encryption devices. Refresh device keys and try again.',
  E2EE_KEY_PACKAGE_EXPIRY_INVALID: 'The encryption key-package expiry is invalid.',
  E2EE_KEY_PACKAGE_LIMIT_REACHED: 'This device already has enough unused encryption key packages.',
  E2EE_KEY_PACKAGE_CONFLICT: 'The encryption key package conflicts with an existing package.',
  E2EE_KEY_PACKAGE_UNAVAILABLE:
    'An enrolled device has no unused encryption key package. Ask its user to open Kaede and retry.',
  E2EE_NO_OTHER_DEVICES: 'No other enrolled device is available for this encrypted room.',
  E2EE_PARTICIPANT_DEVICE_MISSING:
    'Every participant needs an enrolled encryption device before encryption can be enabled.',
  E2EE_PARTICIPANT_HOME_REJECTED: "A participant's home instance rejected encrypted-room setup.",
  E2EE_PARTICIPANT_HOME_UNREACHABLE:
    "A participant's home instance could not be reached for encrypted-room setup.",
  E2EE_POLICY_ALREADY_EXISTS: 'This conversation already has an encryption policy.',
  E2EE_ROOM_MEMBER_LIMIT: 'This conversation has too many members for encrypted-room setup.',
  E2EE_AUTHORITY_REMOTE:
    "Encrypted-room settings must be completed by the conversation's home instance.",
  E2EE_OPERATION_INVALID: 'The encrypted message operation is not valid for this request.',
  E2EE_OPERATION_CONFLICT:
    'This encrypted-room update conflicts with an earlier request. Refresh and try again.',
  E2EE_OPERATION_EXPIRED:
    'This encrypted-room update expired. Review the member list and start it again.',
  E2EE_OPERATION_IN_PROGRESS:
    'Another encrypted-room update is already in progress. Wait for it to finish and retry.',
  E2EE_OPERATION_NOT_FOUND: 'That encrypted-room update could not be found.',
  E2EE_OPERATION_STALE:
    'The room membership changed while encryption was being secured. Review the members and retry.',
  E2EE_ATTACHMENT_REQUIRED: 'Encrypted conversations accept only client-encrypted file uploads.',
  E2EE_REMOTE_DEVICE_DISCOVERY_REQUIRED:
    "The remote user's encryption devices could not be verified.",
  E2EE_REKEY_REQUIRED:
    'Encrypted messaging is paused until an authorized member secures the updated participant list.',
  E2EE_MEDIA_ROTATION_UNAVAILABLE:
    'The previous encrypted media session could not be safely closed. Try again shortly.',
  E2EE_REKEY_PROPOSAL_EXPIRED: 'The key-rotation proposal expired. Start securing the room again.',
  E2EE_ROOM_AUTHORITY_UNREACHABLE:
    "The conversation's home instance could not be reached for encryption setup.",
  E2EE_ROOM_AUTHORITY_INVALID_RESPONSE:
    "The conversation's home instance returned an invalid encryption response.",
  E2EE_ROOM_AUTHORITY_REJECTED: "The conversation's home instance rejected encryption setup.",
  MESSAGE_ENCRYPTION_POLICY_INVALID:
    "This conversation's encryption policy is invalid, so the message was not sent.",
  E2EE_REPORT_DISCLOSURE_REQUIRED:
    'Decrypt the message here and confirm that you want to share its message evidence with Trust & Safety.',
  FEDERATED_COMMANDS_UNAVAILABLE:
    'Commands from that remote application are temporarily unavailable.',
  FEDERATED_WRITE_UNSUPPORTED:
    'This remote resource is read-only here. Make the change on its home instance.',
  INTERACTION_ALREADY_RESPONDED: 'This interaction already has a response.',
  INTERACTION_EXPIRED: 'This interaction expired before the bot responded.',
  INTERACTION_NOT_FOUND: 'That interaction no longer exists or is not assigned to this bot.',
  OWNER_CLI_MANAGED: 'Instance owner access can only be changed from the server CLI.',
  OWNER_REQUIRED: 'Only an instance owner can perform that action.',
  REPORT_NOT_FOUND: 'That safety report no longer exists or you cannot access it.',
  REPORT_DISCLOSURE_UNEXPECTED:
    'Decrypted evidence can only be included when reporting an encrypted message.',
  TEAM_LAST_OWNER: 'Add another team owner before removing or changing the last owner.',
  TEAM_OWNER_REQUIRED: 'Only a developer team owner can perform that action.',
  TEMPLATE_EXCEEDS_APPLICATION: 'The invite requests access that the application has not enabled.',
  WORKER_EXCEEDS_APPLICATION:
    'The worker requests scopes or intents that the application has not enabled.',
  AUTHENTICATION_REQUIRED: 'Your session has expired. Sign in again to continue.',
  CSRF_GUARD: 'This page is out of date. Reload it and try again.',
  MISSING_PERMISSIONS: "You don't have permission to do that.",
  VOICE_ACTIVE_ELSEWHERE:
    'Voice is already active on another device. Choose where you want to continue.',
  CANNOT_MANAGE_PERMISSIONS: "You can't change those permissions.",
  CANNOT_GRANT_PERMISSIONS: "You can't grant permissions you don't have.",
  ROLE_HIERARCHY: 'That member or role is higher than your highest role.',
  OWNER_IMMUNE: "The guild owner can't be moderated or have their roles changed.",
  CANNOT_MANAGE_SELF: "You can't use that action on yourself.",
  GUILD_OWNER_REQUIRED: 'Only the guild owner can do that.',
  NOT_A_GUILD_MEMBER: 'You are no longer a member of this guild.',
  BANNED_FROM_GUILD: 'You cannot join this guild because you are banned.',
  INSTANCE_BANNED_FROM_GUILD: 'Your home instance is blocked from this guild.',
  BOT_USER_BANNED: 'This bot cannot be installed because its account is banned from this guild.',
  BOT_INSTANCE_BANNED:
    'This bot cannot be installed because its home instance is banned from this guild.',
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
  GROUP_DM_NOT_FOUND: 'That group conversation no longer exists or you are no longer a member.',
  GROUP_DM_NOT_MEMBER: 'You are no longer a member of that group conversation.',
  GROUP_DM_DUPLICATE_MEMBER: 'Choose each group member only once.',
  GROUP_DM_INVITE_NOT_FRIEND: 'Only existing friends can be added to a group conversation.',
  GROUP_DM_FULL: 'That group conversation already has the maximum of 10 members.',
  GROUP_DM_ALREADY_MEMBER: 'That friend is already in the group conversation.',
  GROUP_DM_MEMBER_NOT_FOUND: 'That person is no longer in the group conversation.',
  GROUP_DM_OWNER_REQUIRED: 'Only the group creator can remove another member.',
  GROUP_DM_OWNER_CANNOT_REMOVE_SELF: 'Leave the group to transfer ownership automatically.',
  GROUP_DM_HOME_UNREACHABLE:
    "The group conversation's home server is unavailable. Try again shortly.",
  GROUP_DM_INVITEE_HOME_UNREACHABLE:
    "That friend's home server could not confirm the invitation. Try again shortly.",
  GROUP_DM_INVITEE_HOME_REJECTED: "That friend's home server did not accept the invitation.",
  GROUP_DM_MUTATION_REJECTED: "The group conversation's home server rejected that change.",
  GROUP_DM_HOME_INVALID_RESPONSE:
    "The group conversation's home server returned an invalid response.",
  KAED_GROUP_DM_INVITEE_NOT_LOCAL: 'That account is not hosted by this server.',
  KAED_GROUP_DM_WRONG_AUTHORITY: 'That group conversation must be changed through its home server.',
  KAED_GROUP_DM_INVITEE_HOME_UNREACHABLE:
    "That friend's home server could not confirm the invitation. Try again shortly.",
  KAED_GROUP_DM_INVITE_NOT_FRIEND: 'Only existing friends can be added to a group conversation.',
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
  TRACKER_ASSIGNEE_NOT_MEMBER: 'That assignee is no longer a member of this guild.',
  TRACKER_CAPACITY_INVALID: 'This tracker contains more data than the instance can safely load.',
  TRACKER_CLIENT_NONCE_CONFLICT:
    'This create request was already used for different task details. Close the editor and try again.',
  TRACKER_LANE_LIMIT_REACHED: 'This tracker has reached its status limit.',
  TRACKER_LANE_NOT_EMPTY: 'Move or delete every task in this status before deleting it.',
  TRACKER_LANE_NOT_FOUND: 'That status no longer exists. The tracker may have changed elsewhere.',
  TRACKER_LAST_LANE: 'A tracker must keep at least one status.',
  TRACKER_NOT_FOUND: 'This task tracker no longer exists or you cannot view it.',
  TRACKER_POSITION_INVALID:
    'That position is no longer available. Refresh the tracker and try again.',
  TRACKER_TASK_LIMIT_REACHED: 'This tracker has reached its task limit.',
  TRACKER_TASK_NOT_FOUND: 'That task no longer exists. The tracker may have changed elsewhere.',
  TRACKER_VERSION_CONFLICT:
    'This tracker changed somewhere else. The latest version will be loaded before you retry.',
  TRACKER_VERSION_REQUIRED: 'Reload this tracker before changing it.',
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
