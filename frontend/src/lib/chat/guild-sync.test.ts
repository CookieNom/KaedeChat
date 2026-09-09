import { describe, expect, it } from 'vitest';
import { guildHistorySyncGuidance, guildReplicaSyncGuidance } from './guild-sync';

describe('guild synchronization guidance', () => {
  it('keeps automatic history retries nonblocking and explains recent messages remain usable', () => {
    expect(
      guildHistorySyncGuidance({
        history_sync_status: 'retrying',
        history_sync_error_code: 'KAED_FED_HISTORY_CAPACITY',
        history_sync_retry_after_ms: 60_001,
        history_sync_resource: null
      })
    ).toEqual({
      title: expect.stringMatching(/history.*capacity/i),
      message: expect.stringMatching(/recent messages.*available.*retry.*automatic.*61 seconds/i),
      severity: 'status'
    });
  });

  it('explains the exact terminal history budget without blocking new activity', () => {
    const guidance = guildHistorySyncGuidance({
      history_sync_status: 'failed',
      history_sync_error_code: 'FEDERATED_GUILD_HISTORY_LIMIT_REACHED',
      history_sync_retry_after_ms: null,
      history_sync_resource: 'messages'
    });

    expect(guidance?.severity).toBe('alert');
    expect(guidance?.message).toMatch(/(recent|new).*messages.*work/i);
    expect(guidance?.message).toMatch(/history.*messages.*limit/i);
  });

  it('renders replica identity capacity and clears when the guild recovers', () => {
    expect(
      guildReplicaSyncGuidance({
        sync_status: 'quota_paused',
        sync_error_code: 'FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED'
      })?.message
    ).toMatch(/remote account/i);
    expect(guildReplicaSyncGuidance({ sync_status: 'ready', sync_error_code: null })).toBeNull();
    expect(
      guildHistorySyncGuidance({
        history_sync_status: 'ready',
        history_sync_error_code: null,
        history_sync_retry_after_ms: null,
        history_sync_resource: null
      })
    ).toBeNull();
  });
});
