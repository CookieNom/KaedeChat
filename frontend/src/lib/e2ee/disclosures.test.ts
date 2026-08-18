import { describe, expect, it, vi } from 'vitest';

import {
  acknowledgeEncryptedRoom,
  confirmEncryptedRoomJoin,
  encryptedRoomJoinWarning,
  encryptedRoomWarningKey,
  hasAcknowledgedEncryptedRoom
} from './disclosures';

function memoryStorage(): Pick<Storage, 'getItem' | 'setItem'> {
  const values = new Map<string, string>();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value)
  };
}

describe('encrypted room disclosures', () => {
  it('scopes durable acknowledgements to both the account and channel', () => {
    const storage = memoryStorage();
    acknowledgeEncryptedRoom('1@home.test', '2@room.test', storage);

    expect(hasAcknowledgedEncryptedRoom('1@home.test', '2@room.test', storage)).toBe(true);
    expect(hasAcknowledgedEncryptedRoom('1@home.test', '3@room.test', storage)).toBe(false);
    expect(hasAcknowledgedEncryptedRoom('4@home.test', '2@room.test', storage)).toBe(false);
    expect(encryptedRoomWarningKey('1@home.test', '2@room.test')).toContain('v1');
  });

  it('does not acknowledge a warning the user declines', () => {
    const storage = memoryStorage();
    const confirm = vi.fn(() => false);

    expect(
      confirmEncryptedRoomJoin('1@home.test', '2@room.test', 'messages', confirm, storage)
    ).toBe(false);
    expect(hasAcknowledgedEncryptedRoom('1@home.test', '2@room.test', storage)).toBe(false);
    expect(confirm).toHaveBeenCalledOnce();
  });

  it('shows an unacknowledged warning once and then remembers it', () => {
    const storage = memoryStorage();
    const confirm = vi.fn(() => true);

    expect(
      confirmEncryptedRoomJoin('1@home.test', '2@room.test', 'conversation', confirm, storage)
    ).toBe(true);
    expect(
      confirmEncryptedRoomJoin('1@home.test', '2@room.test', 'conversation', confirm, storage)
    ).toBe(true);
    expect(confirm).toHaveBeenCalledOnce();
  });

  it('uses media-specific tradeoffs for encrypted voice rooms', () => {
    const warning = encryptedRoomJoinWarning('media');
    expect(warning).toContain('microphone, camera, screen video, and screen audio');
    expect(warning).toContain('Server recording and transcription are unavailable');
    expect(warning).toContain('safety number');
  });

  it('fails open for room access but never silently suppresses a warning when storage fails', () => {
    const storage = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      }
    };
    const confirm = vi.fn(() => true);

    expect(
      confirmEncryptedRoomJoin('1@home.test', '2@room.test', 'messages', confirm, storage)
    ).toBe(true);
    expect(confirm).toHaveBeenCalledOnce();
  });
});
