export interface SelfModerationStatus {
  guild_id: string;
  guild_domain: string;
  timed_out: boolean;
  timeout_until: string | null;
  timeout_indefinite: boolean;
  reason: string | null;
  details_available?: boolean;
}

export interface SelfModerationGuidance {
  title: string;
  message: string;
}

export const MAX_BROWSER_TIMER_DELAY_MS = 2_147_483_647;
const EXPIRY_GRACE_MS = 250;
export const SELF_MODERATION_RETRY_DELAY_MS = 15_000;

export function selfModerationExpiryDelay(
  status: SelfModerationStatus | null,
  now = Date.now()
): number | null {
  if (!status?.timed_out || status.timeout_indefinite || !status.timeout_until) return null;
  const expiry = Date.parse(status.timeout_until);
  return Number.isFinite(expiry) ? Math.max(0, expiry - now) : null;
}

export function selfModerationTimerDelay(
  status: SelfModerationStatus | null,
  now = Date.now()
): number | null {
  const remaining = selfModerationExpiryDelay(status, now);
  if (remaining === null) return null;
  return Math.min(remaining + EXPIRY_GRACE_MS, MAX_BROWSER_TIMER_DELAY_MS);
}

export function selfModerationRetryDelay(status: SelfModerationStatus | null): number | null {
  return activeSelfModerationStatus(status) && status?.details_available === false
    ? SELF_MODERATION_RETRY_DELAY_MS
    : null;
}

export function activeSelfModerationStatus(
  status: SelfModerationStatus | null,
  now = Date.now()
): SelfModerationStatus | null {
  if (!status?.timed_out) return null;
  if (status.timeout_indefinite) return status.timeout_until === null ? status : null;
  const delay = selfModerationExpiryDelay(status, now);
  return delay !== null && delay > 0 ? status : null;
}

export function selfModerationGuidance(
  status: SelfModerationStatus | null,
  locale?: string
): SelfModerationGuidance | null {
  const active = activeSelfModerationStatus(status);
  if (!active) return null;
  const title = active.timeout_indefinite
    ? 'You are timed out in this guild.'
    : `You are timed out until ${new Intl.DateTimeFormat(locale, {
        dateStyle: 'medium',
        timeStyle: 'short'
      }).format(new Date(active.timeout_until as string))}.`;
  const reason = active.reason?.trim();
  return {
    title,
    message: reason
      ? `Reason: ${reason}`
      : active.details_available === false
        ? 'Kaede is retrieving the reason from the guild’s home instance.'
        : 'The guild’s home instance did not provide a reason.'
  };
}
