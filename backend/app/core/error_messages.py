# ruff: noqa: E501 -- keeping each public error code beside its complete message makes audits safer.
from __future__ import annotations

from http import HTTPStatus

# These messages are part of the public API contract. They deliberately explain
# what the user can do next without exposing database, storage, or federation
# implementation details.
ERROR_MESSAGES: dict[str, str] = {
    "ADMIN_AUTHENTICATION_REQUIRED": "Administrator authentication is required for this action.",
    "ALREADY_GUILD_OWNER": "That member already owns this guild.",
    "ASSET_ALREADY_USED": "That image is already assigned to another profile or guild asset.",
    "ATTACHMENT_ALREADY_USED": "That attachment has already been sent in another message.",
    "ATTACHMENT_NOT_FOUND": "That attachment could not be found or is no longer available.",
    "ATTACHMENT_NOT_OWNED": "You can only use attachments that you uploaded.",
    "ATTACHMENT_PURPOSE_MISMATCH": "That upload cannot be used for this type of image or attachment.",
    "ATTACHMENT_TOO_LARGE": "The selected attachment is larger than this server allows.",
    "AUDIT_REASON_TOO_LONG": "The moderation reason is too long. Shorten it and try again.",
    "AUTHENTICATION_REQUIRED": "Your session is missing or has expired. Sign in again.",
    "BAN_EXPIRY": "The ban expiration is invalid. Choose a future date and time.",
    "BANNED_FROM_GUILD": "You cannot join this guild because you are banned from it.",
    "BULK_DELETE_NOT_SUPPORTED": "These messages cannot be deleted together. Delete them individually instead.",
    "CALL_ALREADY_ACTIVE": "A call is already active in this conversation.",
    "CALL_CONTEXT_MISMATCH": "The call changed while this action was being processed. Reopen it and try again.",
    "CALL_FORBIDDEN": "You do not have permission to use this call.",
    "CALL_HOME_INVALID_RESPONSE": "The call server returned an invalid response. Try again shortly.",
    "CALL_HOME_UNREACHABLE": "The call server cannot be reached right now. Check your connection and try again.",
    "CALL_NOT_ACCEPTED": "Accept the call before trying to join it.",
    "CALL_NOT_FOUND": "That call has ended or could not be found.",
    "CALL_REJECTED": "The call request was rejected by the other server.",
    "CALL_REQUIRES_DM": "Calls can only be started from a direct-message conversation.",
    "CANNOT_BAN_HOME_INSTANCE": "You cannot ban the instance that hosts this guild.",
    "CANNOT_BLOCK_SELF": "You cannot block your own account.",
    "CANNOT_DM_SELF": "You cannot start a direct message with your own account.",
    "CANNOT_DM_USER": "This user is not accepting direct messages from you.",
    "CANNOT_FRIEND_SELF": "You cannot send a friend request to your own account.",
    "CANNOT_GRANT_PERMISSIONS": "You cannot grant permissions that your own role does not have.",
    "CANNOT_MANAGE_PERMISSIONS": "You do not have permission to change that role's permissions.",
    "CANNOT_MANAGE_SELF": "You cannot use this moderation action on your own account.",
    "CANNOT_TIMEOUT_SELF": "You cannot time out your own account.",
    "CHANNEL_HAS_NO_CATEGORY": "This channel is not inside a category, so its category permissions cannot be synchronized.",
    "CHANNEL_NOT_EMPTY": "Move or delete the channels inside this category before deleting it.",
    "CHANNEL_NOT_FOUND": "That channel could not be found or you no longer have access to it.",
    "CHANNEL_PARENT_INVALID": "The selected channel category is not valid.",
    "CHANNEL_SET_CHANGED": "The channel list changed elsewhere. Refresh it before reordering channels.",
    "CLIENT_NONCE_REQUIRED_FOR_FEDERATION": "A request identifier is required before this federated message can be sent. Try again.",
    "CSRF_GUARD": "This sign-in session cannot authorize that request. Refresh the page and try again.",
    "CUSTOM_EMOJI_INVALID": "That custom emoji reference is invalid.",
    "CUSTOM_EMOJI_NOT_FOUND": "That custom emoji could not be found or was deleted.",
    "CUSTOM_EMOJI_SOURCE_ACCESS_REQUIRED": "You no longer have access to the guild that owns that emoji.",
    "DM_PRIVACY_REJECTED": "This user's privacy settings do not allow a direct message or call from you.",
    "DM_REACTION_LIMIT_REACHED": "That direct-message post already has the maximum number of reactions. Remove an existing reaction before adding another.",
    "EMAIL_DISABLED": "Email features are disabled on this server.",
    "EMAIL_NOT_VERIFIED": "Verify your email address before signing in.",
    "EMAIL_REQUIRED": "Enter an email address to create an account on this server.",
    "EMAIL_UNAVAILABLE": "That email address cannot be used. Choose another address or sign in instead.",
    "EMOJI_LIMIT_REACHED": "This guild has reached its custom emoji limit. Delete an emoji before adding another.",
    "EMOJI_NAME_TAKEN": "An emoji with that name already exists in this guild.",
    "EMOJI_NOT_FOUND": "That emoji could not be found or was deleted.",
    "EMOJI_TOO_LARGE": "The selected emoji image is larger than this server allows.",
    "EVERYONE_ROLE_IMMUTABLE": "The Everyone role cannot be edited in that way.",
    "EVERYONE_ROLE_IMPLICIT": "The Everyone role is assigned automatically and cannot be added or removed manually.",
    "FEDERATED_WRITE_REJECTED": "The guild's home server rejected this change. Refresh the channel and try again.",
    "FEDERATED_WRITE_RESPONSE_INVALID": "The other server returned an invalid response. Try again shortly.",
    "FEDERATED_WRITE_UNAVAILABLE": "The other server is unavailable right now. Try again shortly.",
    "FEDERATED_WRITE_UNSUPPORTED": "The other server does not support this action yet.",
    "FEDERATED_GUILD_HISTORY_TEMPORARILY_UNAVAILABLE": "Older guild messages are temporarily unavailable. Kaede will retry the history sync automatically; recent messages and new activity remain available.",
    "FEDERATED_GUILD_HISTORY_LIMIT_REACHED": "This server reached its configured limit for cached guild history. Recent messages and new activity remain available; contact the server administrator if older history is needed.",
    "FEDERATED_GUILD_HISTORY_REJECTED": "Older messages from this guild's home server could not be safely imported. Recent messages and new activity remain available.",
    "FEDERATION_DM_AUTHORITY_MISMATCH": "The direct-message server returned a conversation owned by the wrong server. Try again shortly.",
    "FEDERATION_DM_AUTHORIZATION_FAILED": "The other user's server could not authorize this direct message. Try again later.",
    "FEDERATION_DM_IDENTITY_MISMATCH": "The other server returned the wrong direct-message participants. Try opening the conversation again later.",
    "FEDERATION_DM_OPEN_FAILED": "The other user's server could not open this direct message. Try again later.",
    "FEDERATION_DM_RESPONSE_INVALID": "The other user's server returned an invalid direct-message response. Try again shortly.",
    "FEDERATION_GUILD_JOIN_FAILED": "The guild's home server could not complete the join request.",
    "FEDERATION_GUILD_JOIN_TIMEOUT": "The guild's home server took too long to respond. Try joining again.",
    "FEDERATION_GUILD_LEAVE_FAILED": "The guild's home server could not complete the leave request. Try again.",
    "FEDERATION_INVITE_PREVIEW_BUSY": "The invite's home server is busy. Try again in a moment.",
    "FEDERATION_INVITE_RESOLVE_FAILED": "The invite could not be verified with its home server.",
    "FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED": "This server cannot cache another remote account right now. Contact the server administrator if this continues.",
    "FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED": "This server cannot cache another remote server right now. Contact the server administrator if this continues.",
    "FEDERATION_OUTBOX_CAPACITY_EXCEEDED": "This server's delivery queue for that remote server is full. Nothing was saved; wait for queued federation work to clear and try again.",
    "FEDERATION_IDENTITY_MISMATCH": "The remote server returned a profile that did not match the requested account. Try again later.",
    "FEDERATION_LOOKUP_FAILED": "The remote account or server could not be looked up.",
    "FEDERATION_LOOKUP_RATE_LIMITED": "Too many remote lookups were attempted. Wait a moment and try again.",
    "FEDERATION_SNAPSHOT_FAILED": "The guild could not be synchronized from its home server. Try again later.",
    "FEDERATION_UNAVAILABLE": "The remote server is unavailable right now. Try again later.",
    "FRIEND_REQUEST_NOT_FOUND": "That friend request no longer exists.",
    "GIF_PICKER_DISABLED": "The GIF picker is disabled on this server.",
    "GIF_PROVIDER_INVALID": "The configured GIF service returned an invalid response.",
    "GIF_PROVIDER_UNAVAILABLE": "The GIF service is unavailable right now. Try again later.",
    "GUILD_IS_LOCAL": "This operation is only valid for a guild hosted on another server.",
    "GUILD_MEMBER_NOT_FOUND": "That user is no longer a member of this guild.",
    "GUILD_NOT_FOUND": "That guild could not be found or you no longer have access to it.",
    "GUILD_NAVIGATION_GUILD_UNAVAILABLE": "Your guild layout includes a guild you can no longer access. Refresh the guild list and try again.",
    "GUILD_NAVIGATION_DUPLICATE_GUILD": "A guild can appear only once in your guild layout.",
    "GUILD_OWNER_REQUIRED": "Only the guild owner can perform this action.",
    "IMAGE_ASSET_TYPE_REQUIRED": "Choose a PNG, JPEG, GIF, or WebP image.",
    "IMPORT_TOO_LARGE": "The import file is larger than this server allows.",
    "INTERNAL_SERVER_ERROR": "Kaede could not complete this request because of a server error. Try again; if it continues, provide the error reference to support.",
    "INSTANCE_BAN_EXPIRY": "The instance-ban expiration is invalid. Choose a future date and time.",
    "INSTANCE_BANNED_FROM_GUILD": "You cannot join because your account's server is banned from this guild.",
    "INVALID_CHANNEL_PARENT": "Choose a valid category for this channel.",
    "INVALID_CSV": "The CSV file could not be read. Check its format and try again.",
    "INVALID_CREDENTIALS": "The username, email address, or password is incorrect.",
    "INVALID_DOMAIN": "Enter a valid server domain without a protocol or path.",
    "INVALID_MENTION": "One of the mentions in this message is invalid.",
    "INVALID_MESSAGE_REFERENCE": "The replied-to message is invalid or no longer accessible.",
    "INVALID_MFA": "The verification code is invalid or has expired. Enter a new code.",
    "INVALID_OVERWRITE_TARGET": "The selected permission target is invalid.",
    "INVALID_PAGINATION": "This page cursor is invalid or has expired. Reload the list.",
    "INVALID_ROLE_MENTION": "One of the role mentions in this message is invalid.",
    "INVALID_REFRESH_TOKEN": "Your session has expired. Sign in again.",
    "INVALID_TOKEN": "This security link or token is invalid or has expired. Request a new one.",
    "INVITE_NOT_FOUND": "That invite is invalid, expired, revoked, or no longer accessible.",
    "KAED_CALL_INVALID_TIMESTAMP": "The remote call update is too old or too far in the future to accept.",
    "KAED_DM_INVALID_PARTICIPANTS": "The remote direct-message request contains the wrong participants.",
    "KAED_DM_WRONG_AUTHORITY": "This direct message must be opened through its designated home server.",
    "KAED_FED_AUTHOR_ORIGIN_MISMATCH": "The federated event author does not belong to the server that sent it.",
    "KAED_FED_BAD_EVENT_SIGNATURE": "The federated event signature could not be verified.",
    "KAED_FED_BAD_SIGNATURE": "The federated request signature could not be verified.",
    "KAED_FED_BATCH_TOO_LARGE": "The federated request contains more data than this server allows.",
    "KAED_FED_CLOCK_SKEW": "The sending and receiving server clocks are too far apart to verify this request.",
    "KAED_FED_EVENT_ID_CONFLICT": "A different federated event already uses this event identifier.",
    "KAED_FED_EVENT_REJECTED": "The federated event was rejected because it failed an authorization or state check.",
    "KAED_FED_EVENT_RETRY": "The federated event could not be processed yet. The sending server should retry it.",
    "KAED_FED_EVENT_TIMESTAMP_INVALID": "The federated event timestamp is outside the accepted time window.",
    "KAED_FED_FULL_RESYNC": "The federated guild copy is out of date and must be synchronized again.",
    "KAED_FED_HISTORY_CURSOR_INVALID": "The message-history page cursor is invalid or outside its authorized range.",
    "KAED_FED_HISTORY_EXPIRED": "The message-history export has expired. Request a new export.",
    "KAED_FED_HISTORY_FORBIDDEN": "This server is not authorized to export that message history.",
    "KAED_FED_HISTORY_GRANT_STALE": "The message-history authorization changed. Refresh it and try again.",
    "KAED_FED_HISTORY_MESSAGE_TOO_LARGE": "A message in this history page is larger than the federation limit.",
    "KAED_FED_HISTORY_NOT_FOUND": "That message-history export or channel could not be found.",
    "KAED_FED_HISTORY_REVOKED": "Access to this message-history export was revoked. Request a new export if access is restored.",
    "KAED_FED_HISTORY_UNAVAILABLE": "Message-history export is temporarily unavailable. Try again later.",
    "KAED_FED_HOP_LIMIT": "The federated request passed through too many servers and was rejected.",
    "KAED_FED_INSTANCE_SILENCED": "The sending server is temporarily not allowed to publish guild events here.",
    "KAED_FED_INSTANCE_SUSPENDED": "The sending server is suspended and cannot federate with this server.",
    "KAED_FED_INVALID_BATCH": "The federated event batch is not valid JSON in the expected format.",
    "KAED_FED_INVALID_JSON": "The federated request is not valid, unambiguous JSON.",
    "KAED_FED_BAD_NONCE": "The federated request nonce is missing or invalid.",
    "KAED_FED_NONCE_REQUIRED": "This peer must use replay-protected federation requests.",
    "KAED_FED_REPLAYED_REQUEST": "This federated request was already processed.",
    "KAED_FED_INVALID_BATCH_SIZE": "A federated batch must contain between 1 and 100 events.",
    "KAED_FED_INVALID_CONTENT_LENGTH": "The federated request has an invalid Content-Length header.",
    "KAED_FED_INVALID_EVENT": "A federated event is missing required data or contains invalid data.",
    "KAED_FED_INBOX_QUOTA_EXCEEDED": "This server has reached its retained federation event limit. The sending server should retry later.",
    "KAED_FED_IDENTITY_STORAGE_QUOTA_EXCEEDED": "This server cannot retain another federated account identity right now. The sending server should stop this operation and may try again later.",
    "KAED_FED_INSTANCE_STORAGE_QUOTA_EXCEEDED": "This server cannot retain another federated server identity right now. The sending server should stop this operation and may try again later.",
    "KAED_FED_OUTBOX_CAPACITY_EXCEEDED": "This server's outbound federation queue is full. The sending server should retry automatically after queued work clears.",
    "KAED_FED_RELATIONSHIP_REQUEST_QUOTA_EXCEEDED": "The receiving server cannot accept another pending friend request right now. The request was not delivered.",
    "KAED_FED_REPLICA_QUOTA_EXCEEDED": "This remote guild replica reached its configured storage limit. Synchronization is paused until an administrator raises the limit or removes cached replica data.",
    "KAED_FED_HISTORY_CAPACITY": "This server is already importing the maximum configured amount of remote message history. The import will be retried automatically.",
    "FEDERATED_DM_STORAGE_QUOTA_EXCEEDED": "This server could not retain more direct-message data. Recent remote messages are normally kept by removing the oldest cached copies; contact the server administrator if this continues.",
    "KAED_FED_DM_STORAGE_QUOTA_EXCEEDED": "The receiving server could not retain more direct-message data. Delivery cannot continue until that server frees space or raises its limit.",
    "KAED_FED_DELIVERY_EXPIRED": "The remote server did not accept this operation before the delivery window ended. Try the operation again later.",
    "KAED_FED_EVENT_TOO_LARGE": "This operation is too large to send between servers. Reduce its size and try again.",
    "FEDERATED_DM_HISTORY_TRUNCATED": "This server keeps the newest part of this remote direct message locally and loads older messages from its home server as you scroll.",
    "FEDERATED_DM_HISTORY_UNAVAILABLE": "Older messages could not be loaded from their home server right now. Recent messages are still available here; try loading the older messages again in a moment.",
    "FEDERATED_MODERATION_STATUS_INVALID": "The guild's home server returned invalid timeout details. Sending is still checked by the guild home.",
    "FEDERATED_MODERATION_STATUS_UNAVAILABLE": "Your timeout details are temporarily unavailable from the guild's home server. Sending is still checked by the guild home.",
    "KAED_FED_INVALID_SNAPSHOT_CURSOR": "The guild-snapshot page cursor is incomplete or invalid.",
    "KAED_FED_KEY_HISTORY_OVERFLOW": "This server cannot publish another signing key until old key history expires.",
    "KAED_FED_KEY_REFRESH_RATE_LIMITED": "Too many signing-key refreshes were requested. Wait before trying again.",
    "KAED_FED_NOT_ALLOWLISTED": "The sending server is not on this server's federation allowlist.",
    "KAED_FED_RESYNC_RETRY": "The guild resynchronization could not finish yet. The sending server should retry it.",
    "KAED_FED_SIGNATURE_REQUIRED": "This federated request is missing its required signature.",
    "KAED_FED_SNAPSHOT_CHANGED": "The guild snapshot changed during download. Restart the synchronization.",
    "KAED_FED_UNKNOWN_KEY": "The sending server's signing key is unknown or expired, so the request could not be verified.",
    "KAED_FED_UNSUPPORTED_VERSION": "The sending server uses an unsupported federation protocol version.",
    "KAED_GUILD_INVALID_MENTION": "A federated message mentions an account that is not in this guild.",
    "KAED_GUILD_NONCE_STATE_CONFLICT": "A different federated message already uses this request identifier.",
    "KAED_MEDIA_NOT_FOUND": "The requested federated media file could not be found or is not accessible to this server.",
    "KAED_MEDIA_UNAVAILABLE": "The requested federated media file is temporarily unavailable. Try again later.",
    "KAED_PRESENCE_STALE_OR_UNKNOWN": "The presence update is older than the current state or refers to an unknown account.",
    "KAED_RATE_LIMITED": "This server is sending federated requests too quickly. Wait before trying again.",
    "KAED_VOICE_INVALID_ROOM": "The federated voice update refers to an invalid or mismatched room.",
    "KAED_VOICE_INVALID_STATE": "The federated voice participant state is invalid or out of date.",
    "KAED_VOICE_NOT_SUBSCRIBED": "This server no longer has an active subscription to that voice channel.",
    "LINK_PREVIEW_MEDIA_EXPIRED": "This link-preview image has expired. Reload the message to request it again.",
    "LINK_PREVIEW_MEDIA_UNAVAILABLE": "The link-preview image is unavailable right now.",
    "LINK_PREVIEW_MEDIA_UNSUPPORTED": "This link-preview image format is not supported.",
    "LINK_PREVIEW_UNAVAILABLE": "A preview could not be loaded for this link.",
    "LINK_PREVIEW_URL_INVALID": "That link is not a valid public HTTP or HTTPS address.",
    "LOCAL_USER_REQUIRED": "This action is only available for an account hosted on this server.",
    "LOGIN_RATE_LIMITED": "Too many sign-in attempts were made. Wait before trying again.",
    "MEDIA_NOT_AVAILABLE": "This media file is still processing or is no longer available.",
    "MEDIA_NOT_FOUND": "This media file could not be found or was deleted.",
    "MEDIA_STORAGE_UNAVAILABLE": "Media storage is unavailable right now. Try again later.",
    "MEDIA_VARIANT_NOT_FOUND": "The requested media size or format is not available.",
    "MEMBER_NOT_FOUND": "That user is no longer a member of this guild.",
    "MEMBER_TIMED_OUT": "You cannot send messages while you are timed out in this guild.",
    "MESSAGE_NOT_FOUND": "That message could not be found, was deleted, or is no longer accessible.",
    "MFA_RATE_LIMITED": "Too many verification attempts were made. Wait before trying again.",
    "MISSING_PERMISSIONS": "You do not have the permissions required for this action.",
    "NOT_A_GUILD_MEMBER": "You are not a member of this guild.",
    "NOT_TEXT_CHANNEL": "Messages can only be sent to a text channel or direct message.",
    "OWNER_IMMUNE": "The guild owner cannot be moderated.",
    "OWNER_MUST_TRANSFER_OR_DELETE_GUILD": "Transfer ownership or delete the guild before leaving it.",
    "OWNER_TRANSFER_REQUIRES_LOCAL_MEMBER": "Ownership can currently be transferred only to a member hosted on this server.",
    "PARENT_NOT_CATEGORY": "The selected parent is not a channel category.",
    "PASSWORD_WORK_BUSY": "The server is handling too many password requests. Wait a moment and try again.",
    "PUSH_DEVICE_NOT_FOUND": "This notification registration no longer exists. Re-enable notifications on the device.",
    "PUSH_DISABLED": "Mobile push notifications are disabled on this server.",
    "PUSH_EVENT_NOT_FOUND": "That notification has expired. Open Kaede to refresh your messages.",
    "RATE_LIMITED": "Too many requests were made. Wait a moment and try again.",
    "RELATIONSHIP_BLOCKED": "This action is unavailable because one of you has blocked the other.",
    "RELATIONSHIP_CONFLICT": "That relationship changed elsewhere. Refresh and try again.",
    "REMOTE_MEDIA_BUSY": "The server is processing too many remote media files. Try again in a moment.",
    "REMOTE_MEDIA_CACHE_FULL": "The remote media cache is full. Try again in a moment while older files are cleared.",
    "REMOTE_MEDIA_REJECTED": "The remote media file was rejected because it was unsafe or invalid.",
    "REMOTE_MEDIA_UNAVAILABLE": "The remote media file cannot be fetched right now. Try again later.",
    "REGISTRATION_CONFLICT": "That username or email address is unavailable. Choose another one or sign in.",
    "REQUEST_BODY_TOO_LARGE": "The submitted request is larger than this server allows.",
    "ROLE_HIERARCHY": "You cannot manage a member or role at or above your highest role.",
    "ROLE_MENTION_TOO_LARGE": "That role has too many members to mention at once.",
    "ROLE_NOT_FOUND": "That role could not be found or was deleted.",
    "ROLE_NOT_MENTIONABLE": "That role cannot be mentioned by your account.",
    "ROLE_ORDER_INCOMPLETE": "The role order is incomplete. Refresh the list and try again.",
    "ROLE_ORDER_NOT_CONTIGUOUS": "The role order contains invalid positions. Refresh the list and try again.",
    "ROLE_POSITION_BATCH_REQUIRED": "Reorder roles using the complete role list.",
    "ROLE_STATE_CHANGED": "The role list changed elsewhere. Refresh it before saving.",
    "SESSION_LIMIT": "This account has too many active sessions. Sign out another device and try again.",
    "SESSION_NOT_FOUND": "That signed-in session no longer exists. Refresh the device list.",
    "SETTINGS_MISSING": "Your settings could not be loaded. Reload Kaede and try again.",
    "SETTINGS_VERSION_CONFLICT": "These settings changed on another device. Reload them before saving.",
    "SETTINGS_VERSION_REQUIRED": "Reload these settings before saving changes.",
    "SERVICE_NOT_READY": "Kaede is still starting or one of its required services is unavailable.",
    "SLOWMODE_RATE_LIMITED": "Slow mode is active. Wait for the displayed timer before sending again.",
    "TARGET_CANNOT_CONNECT": "That member does not have permission to connect to this voice channel.",
    "TIMEOUT_MODE_CONFLICT": "Choose either a timed timeout or an indefinite timeout, not both.",
    "TIMEOUT_REQUIRES_TIMEZONE": "Include a timezone when choosing when the timeout ends.",
    "TIMEOUT_TOO_LONG": "The timeout is longer than this server allows.",
    "TOO_MANY_ROLE_MENTIONS": "This message mentions too many roles. Remove some role mentions and try again.",
    "TURNSTILE_ACTION_INVALID": "The verification challenge was created for a different action. Reload and try again.",
    "TURNSTILE_DISABLED": "Automated-request verification is not enabled on this server.",
    "TURNSTILE_INVALID": "The verification challenge expired or was unsuccessful. Complete it again.",
    "TURNSTILE_REQUEST_INVALID": "The verification request is invalid. Reload and try again.",
    "TURNSTILE_REQUIRED": "Complete the verification challenge before continuing.",
    "TURNSTILE_UNAVAILABLE": "The verification service is temporarily unavailable. Try again shortly.",
    "UPLOAD_INCOMPLETE": "The file did not finish uploading. Check your connection and try again.",
    "UPLOAD_INFLIGHT_LIMIT": "Too many files are already uploading. Wait for them to finish and try again.",
    "UPLOAD_INFLIGHT_QUOTA_EXCEEDED": "Pending uploads would exceed your storage quota. Wait or remove an upload.",
    "UPLOAD_SIZE_MISMATCH": "The uploaded file size changed during transfer. Select the file again and retry.",
    "UPLOAD_TICKET_EXPIRED": "The upload took too long and expired. Select the file and try again.",
    "UPLOAD_TYPE_MISMATCH": "The uploaded file type does not match the selected file. Choose another file.",
    "USE_EXTERNAL_EMOJIS_REQUIRED": "You do not have permission to use emojis from another guild here.",
    "USER_NOT_FOUND": "That user could not be found or is no longer available.",
    "USER_STORAGE_QUOTA_EXCEEDED": "This upload would exceed your storage quota. Remove files before trying again.",
    "VALIDATION_ERROR": "Some submitted information is invalid. Check it and try again.",
    "VOICE_CHANNEL_NOT_FOUND": "That voice channel could not be found or is no longer accessible.",
    "VOICE_DENIED": "You do not have permission to join this voice session.",
    "VOICE_DISABLED": "Voice chat is disabled on this server.",
    "VOICE_HOME_INVALID_RESPONSE": "The voice server returned an invalid response. Try again shortly.",
    "VOICE_HOME_UNREACHABLE": "The voice server cannot be reached right now. Check your connection and try again.",
    "VOICE_JOIN_REVOKED": "Your permission to join this voice channel changed. Leave and rejoin the channel.",
    "VOICE_NO_CHANGES": "No voice settings were changed.",
    "VOICE_NOT_CONNECTED": "Join the voice channel before using this control.",
    "VOICE_NOT_HOME": "This voice action must be handled by the guild's home server.",
    "VOICE_NOT_IN_GUILD": "You must be a guild member to join this voice channel.",
    "VOICE_USER_NOT_FOUND": "That voice participant has already left.",
    "VOICE_WEBHOOK_IN_PROGRESS": "Another voice update is still being processed. Try again in a moment.",
    "VOICE_WEBHOOK_INVALID": "The voice server sent an update that could not be verified.",
    "VOICE_WEBHOOK_TOO_LARGE": "The voice-server update was larger than this server allows.",
    "WEBHOOK_CREATOR_MISSING": "The account that created this webhook no longer exists.",
    "WEBHOOK_NOT_FOUND": "That webhook could not be found or was deleted.",
    "WEBHOOK_RATE_LIMITED": "This webhook is sending messages too quickly. Wait and try again.",
    "WEBHOOK_REQUIRES_TEXT_CHANNEL": "Webhooks can only post in text channels.",
}


STATUS_MESSAGES: dict[int, str] = {
    400: "The request could not be completed because some information was invalid.",
    401: "Your session is missing or has expired. Sign in again.",
    403: "You do not have permission to perform this action.",
    404: "The requested item could not be found or is no longer available.",
    405: "That action is not supported here.",
    409: "This item changed elsewhere. Refresh it and try again.",
    410: "The requested item has expired and must be requested again.",
    412: "This item changed elsewhere. Reload it before saving your changes.",
    413: "The selected file or request is larger than this server allows.",
    415: "The selected file format is not supported.",
    422: "Some submitted information is invalid. Check it and try again.",
    428: "Reload this item before saving so Kaede can verify that it is current.",
    429: "Too many requests were made. Wait a moment and try again.",
    500: "Kaede could not complete this request because of a server error. Try again; if it continues, provide the error reference to support.",
    502: "Another service returned an invalid response. Try again shortly.",
    503: "The required service is temporarily unavailable. Try again shortly.",
    504: "The request took too long to complete. Check your connection and try again.",
    507: "This server does not currently have enough storage capacity for that operation.",
    508: "The request passed through too many servers and could not be completed.",
}


_SUFFIX_MESSAGES: tuple[tuple[str, str], ...] = (
    ("_NOT_FOUND", "could not be found or is no longer available."),
    ("_UNAVAILABLE", "is temporarily unavailable. Try again shortly."),
    ("_UNREACHABLE", "cannot be reached right now. Check your connection and try again."),
    ("_RATE_LIMITED", "is being requested too quickly. Wait a moment and try again."),
    ("_TOO_LARGE", "is larger than this server allows."),
    ("_EXPIRED", "has expired. Request it again and retry."),
    ("_REVOKED", "was revoked and can no longer be used."),
    ("_DISABLED", "is disabled on this server."),
    ("_REQUIRED", "is required to complete this action."),
    ("_FORBIDDEN", "is not permitted for your account."),
    ("_REJECTED", "was rejected by the server."),
    ("_INVALID", "is invalid. Check it and try again."),
    ("_CONFLICT", "changed elsewhere. Refresh and try again."),
    ("_STALE", "is out of date. Refresh and try again."),
    ("_MISMATCH", "does not match the expected value. Refresh and try again."),
    ("_UNSUPPORTED", "is not supported by this server or client."),
    ("_BUSY", "is busy right now. Wait a moment and try again."),
    ("_IN_PROGRESS", "is already being processed. Wait a moment and try again."),
    ("_MISSING", "is missing. Reload and try again."),
    ("_FAILED", "could not be completed. Try again."),
)


_TOKEN_NAMES = {
    "DM": "direct message",
    "MFA": "verification",
    "GIF": "GIF",
    "CSV": "CSV",
    "CSRF": "browser session",
    "KAED": "federated request",
    "FED": "federated request",
}


def _subject(code: str, suffix: str = "", omit: frozenset[str] = frozenset()) -> str:
    base = code.removesuffix(suffix).strip("_")
    for prefix in ("KAED_FED_", "FEDERATION_", "KAED_"):
        if base.startswith(prefix):
            base = base.removeprefix(prefix)
            break
    words = [
        _TOKEN_NAMES.get(part, part.lower())
        for part in base.split("_")
        if part and part not in omit
    ]
    return " ".join(words) or "request"


def friendly_error_message(code: str, status_code: int) -> str:
    """Return a safe, comprehensible message for every public error code."""

    if message := ERROR_MESSAGES.get(code):
        return message
    if code.startswith("HTTP_"):
        return status_message(status_code)
    parts = frozenset(code.split("_"))
    if "INVALID" in parts:
        return (
            f"The {_subject(code, omit=frozenset({'INVALID'}))} is invalid. Check it and try again."
        )
    if "BAD" in parts:
        return f"The {_subject(code, omit=frozenset({'BAD'}))} could not be verified."
    if "WRONG" in parts:
        return f"The {_subject(code, omit=frozenset({'WRONG'}))} does not match the expected value."
    if "UNSUPPORTED" in parts:
        return f"The {_subject(code, omit=frozenset({'UNSUPPORTED'}))} is not supported."
    if "STALE" in parts:
        return f"The {_subject(code, omit=frozenset({'STALE', 'OR', 'UNKNOWN'}))} is out of date or unavailable. Refresh and try again."
    if code.endswith("_MUST_BE_FUTURE"):
        return f"The {_subject(code, '_MUST_BE_FUTURE')} must be a future date and time."
    if code.endswith("_REQUIRES_TIMEZONE"):
        return f"The {_subject(code, '_REQUIRES_TIMEZONE')} must include a timezone."
    if code.endswith("_RETRY"):
        return f"The {_subject(code, '_RETRY')} could not be completed. Try again."
    if code.endswith("_SILENCED"):
        return f"The {_subject(code, '_SILENCED')} is temporarily not accepting federated events."
    if code.endswith("_SUSPENDED"):
        return f"The {_subject(code, '_SUSPENDED')} is suspended and cannot perform this action."
    if code.endswith("_NOT_ALLOWLISTED"):
        return (
            f"The {_subject(code, '_NOT_ALLOWLISTED')} is not allowed to federate with this server."
        )
    if code.endswith("_NOT_SUBSCRIBED"):
        return f"The {_subject(code, '_NOT_SUBSCRIBED')} subscription is missing or expired. Rejoin and try again."
    if code.endswith("_UNKNOWN_KEY"):
        return f"The {_subject(code, '_UNKNOWN_KEY')} signing key is unknown, so the request could not be verified."
    if code.endswith("_CLOCK_SKEW"):
        return f"The {_subject(code, '_CLOCK_SKEW')} server clocks are too far apart to verify the request."
    if code.endswith("_HOP_LIMIT"):
        return (
            f"The {_subject(code, '_HOP_LIMIT')} passed through too many servers and was rejected."
        )
    if code.endswith("_OVERFLOW"):
        return f"The {_subject(code, '_OVERFLOW')} exceeded the server's safe limit."
    if code.endswith("_RESYNC"):
        return f"The {_subject(code, '_RESYNC')} must be synchronized again before continuing."
    if code.endswith("_CHANGED"):
        return f"The {_subject(code, '_CHANGED')} changed while the request was running. Refresh and try again."
    for suffix, ending in _SUFFIX_MESSAGES:
        if code.endswith(suffix):
            subject = _subject(code, suffix)
            return f"The {subject} {ending}"
    subject = _subject(code)
    if subject != "request":
        return f"The {subject} could not be processed. Check the request and try again."
    return status_message(status_code)


def status_message(status_code: int) -> str:
    if message := STATUS_MESSAGES.get(status_code):
        return message
    try:
        phrase = HTTPStatus(status_code).phrase
    except ValueError:
        phrase = "Request failed"
    return f"{phrase}. Check the request and try again."
