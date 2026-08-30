export type EncryptedRoomKind = 'conversation' | 'messages' | 'media';

type DisclosureStorage = Pick<Storage, 'getItem' | 'setItem'>;

const ROOM_WARNING_VERSION = 'v1';
const ROOM_WARNING_PREFIX = `kaede.e2ee-room-warning.${ROOM_WARNING_VERSION}`;

function browserDisclosureStorage(): DisclosureStorage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function encryptedRoomWarningKey(accountRef: string, channelRef: string): string {
  return `${ROOM_WARNING_PREFIX}.${encodeURIComponent(accountRef)}.${encodeURIComponent(channelRef)}`;
}

export function hasAcknowledgedEncryptedRoom(
  accountRef: string,
  channelRef: string,
  storage?: DisclosureStorage
): boolean {
  try {
    return (
      (storage ?? browserDisclosureStorage())?.getItem(
        encryptedRoomWarningKey(accountRef, channelRef)
      ) === 'acknowledged'
    );
  } catch {
    // A blocked or full storage area must never suppress a safety disclosure.
    return false;
  }
}

export function acknowledgeEncryptedRoom(
  accountRef: string,
  channelRef: string,
  storage?: DisclosureStorage
): void {
  try {
    (storage ?? browserDisclosureStorage())?.setItem(
      encryptedRoomWarningKey(accountRef, channelRef),
      'acknowledged'
    );
  } catch {
    // The room remains usable, but the warning will be shown again next time.
  }
}

export function encryptedRoomJoinWarning(kind: EncryptedRoomKind): string {
  const protectedContent =
    kind === 'media'
      ? 'This voice channel encrypts microphone, camera, screen video, and screen audio at participant devices.'
      : kind === 'messages'
        ? 'New messages and files in this channel are encrypted at participant devices. Supported calls in this channel are encrypted too.'
        : 'New messages and files in this conversation are encrypted at participant devices. Supported calls are encrypted too.';
  const unavailableFeatures =
    kind === 'media'
      ? 'Server recording and transcription are unavailable, and an unsupported client cannot join.'
      : 'Server message search, automatic previews, server file previews and scanning, call recording, and transcription are unavailable. Webhooks receive no access automatically; a verified webhook device can receive only future content after an explicit grant, rekey, and history floor. A verified participant-mode app can likewise receive future content only after explicit admission. Notifications are generic, and unsupported clients cannot participate.';
  const historyAndRecovery =
    kind === 'media'
      ? ''
      : ' Existing plaintext history is not retroactively protected. Losing the synchronized encrypted account vault, all trusted local state, and the recovery backup permanently loses encrypted history.';

  return (
    `You are opening an end-to-end encrypted room for the first time on this account. ${protectedContent} ` +
    `${unavailableFeatures}${historyAndRecovery} ` +
    'The instance can still see participants, timing, sizes, track types, and traffic metadata, and another participant can save or record content on their device. ' +
    'Participant identities are unverified until everyone compares the safety number through a separate trusted channel; use the lock in the room header to view it, and compare it again after membership or identity changes. ' +
    'Removed members keep content they already received or recorded.\n\n' +
    'Continue into this encrypted room?'
  );
}

export function confirmEncryptedRoomJoin(
  accountRef: string,
  channelRef: string,
  kind: EncryptedRoomKind,
  confirm: (message: string) => boolean = (message) => window.confirm(message),
  storage?: DisclosureStorage
): boolean {
  if (hasAcknowledgedEncryptedRoom(accountRef, channelRef, storage)) return true;
  if (!confirm(encryptedRoomJoinWarning(kind))) return false;
  acknowledgeEncryptedRoom(accountRef, channelRef, storage);
  return true;
}
