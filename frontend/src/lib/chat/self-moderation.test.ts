import { describe, expect, it, vi } from 'vitest';
import {
  MAX_BROWSER_TIMER_DELAY_MS,
  activeSelfModerationStatus,
  selfModerationExpiryDelay,
  selfModerationGuidance,
  selfModerationRetryDelay,
  selfModerationTimerDelay,
  type SelfModerationStatus
} from './self-moderation';

const finite: SelfModerationStatus = {
  guild_id: '10',
  guild_domain: 'guild.example',
  timed_out: true,
  timeout_until: '2030-01-02T03:04:00.000Z',
  timeout_indefinite: false,
  reason: 'Repeated spam'
};

describe('self moderation status', () => {
  it('renders the private reason for an active affected-user status', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2029-01-01T00:00:00.000Z'));
    expect(selfModerationGuidance(finite, 'en-US')).toEqual({
      title: 'You are timed out until Jan 2, 2030, 3:04 AM.',
      message: 'Reason: Repeated spam'
    });
    vi.useRealTimers();
  });

  it('clears expired or internally inconsistent projections', () => {
    expect(activeSelfModerationStatus(finite, Date.parse('2031-01-01T00:00:00Z'))).toBeNull();
    expect(
      activeSelfModerationStatus({
        ...finite,
        timeout_indefinite: true,
        timeout_until: finite.timeout_until
      })
    ).toBeNull();
  });

  it('provides a bounded expiry delay for automatic UI clearing', () => {
    expect(selfModerationExpiryDelay(finite, Date.parse('2030-01-02T03:03:59Z'))).toBe(1000);
    expect(selfModerationExpiryDelay(finite, Date.parse('2031-01-01T00:00:00Z'))).toBe(0);
    expect(selfModerationExpiryDelay({ ...finite, timeout_indefinite: true })).toBeNull();
  });

  it('keeps a timeout active and reschedulable beyond the browser timer limit', () => {
    const now = Date.parse('2029-01-01T00:00:00Z');
    const longTimeout = {
      ...finite,
      timeout_until: new Date(now + MAX_BROWSER_TIMER_DELAY_MS + 86_400_000).toISOString()
    };
    expect(selfModerationTimerDelay(longTimeout, now)).toBe(MAX_BROWSER_TIMER_DELAY_MS);

    const firstWake = now + MAX_BROWSER_TIMER_DELAY_MS;
    expect(activeSelfModerationStatus(longTimeout, firstWake)).toBe(longTimeout);
    expect(selfModerationTimerDelay(longTimeout, firstWake)).toBe(86_400_250);
  });

  it('supports an indefinite timeout without inventing a reason', () => {
    expect(
      selfModerationGuidance({
        ...finite,
        timeout_until: null,
        timeout_indefinite: true,
        reason: null
      })
    ).toEqual({
      title: 'You are timed out in this guild.',
      message: 'The guild’s home instance did not provide a reason.'
    });
  });

  it('automatically retries only a live non-authoritative fallback', () => {
    const fallback = { ...finite, reason: null, details_available: false };
    expect(selfModerationRetryDelay(fallback)).toBe(15_000);
    expect(selfModerationGuidance(fallback)?.message).toBe(
      'Kaede is retrieving the reason from the guild’s home instance.'
    );
    expect(selfModerationRetryDelay({ ...finite, details_available: true })).toBeNull();
    expect(
      selfModerationRetryDelay({
        ...finite,
        details_available: false,
        timeout_until: '2020-01-01T00:00:00Z'
      })
    ).toBeNull();
  });
});
