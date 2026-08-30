import { entityKey } from '$lib/chat/refs';

type VoiceChannelRef = { id: string; origin_domain: string };

/**
 * Resolve the authoritative room start for one federated channel.
 * `undefined` means the dispatch does not apply; `null` is an explicit clear.
 */
export function voiceStartTimeFromDispatch(
  eventName: 'CHANNEL_INFO' | 'VOICE_CHANNEL_START_TIME_UPDATE',
  payload: unknown,
  channel: VoiceChannelRef
): number | null | undefined {
  if (!payload || typeof payload !== 'object') return undefined;
  const data = payload as Record<string, unknown>;
  if (eventName === 'VOICE_CHANNEL_START_TIME_UPDATE') {
    return voiceStartTimeFromItem(data, data.guild_domain, channel);
  }
  if (!Array.isArray(data.channels)) return undefined;
  for (const raw of data.channels) {
    if (!raw || typeof raw !== 'object') continue;
    const resolved = voiceStartTimeFromItem(
      raw as Record<string, unknown>,
      data.guild_domain,
      channel
    );
    if (resolved !== undefined) return resolved;
  }
  return undefined;
}

function voiceStartTimeFromItem(
  item: Record<string, unknown>,
  fallbackDomain: unknown,
  channel: VoiceChannelRef
): number | null | undefined {
  const domain = item.origin_domain ?? fallbackDomain;
  if (
    typeof item.id !== 'string' ||
    typeof domain !== 'string' ||
    entityKey({ id: item.id, origin_domain: domain }) !== entityKey(channel) ||
    !Object.hasOwn(item, 'voice_start_time')
  ) {
    return undefined;
  }
  const value = item.voice_start_time;
  if (value === null) return null;
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : undefined;
}

/** Render Discord-style compact elapsed voice time from epoch seconds. */
export function formatVoiceElapsed(startedAt: number | null, now = Date.now()): string | null {
  if (!Number.isSafeInteger(startedAt) || startedAt === null || startedAt <= 0) return null;
  const elapsed = Math.max(0, Math.floor(now / 1_000) - startedAt);
  const hours = Math.floor(elapsed / 3_600);
  const minutes = Math.floor((elapsed % 3_600) / 60);
  const seconds = elapsed % 60;
  return hours > 0
    ? `${hours}:${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`
    : `${minutes}:${seconds.toString().padStart(2, '0')}`;
}
