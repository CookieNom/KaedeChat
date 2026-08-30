import type { Message } from './types';

export type TtsPlaybackMode = 'all' | 'current' | 'never';

export interface TtsPreferences {
  enabled: boolean;
  playback: TtsPlaybackMode;
  rate: number;
}

const STORAGE_KEY = 'kaede.tts.v1';
const DEFAULTS: TtsPreferences = { enabled: false, playback: 'never', rate: 1 };
let current: TtsPreferences = DEFAULTS;

export function ttsPreferencesFromSettings(
  settings: Record<string, unknown> | null | undefined
): TtsPreferences {
  const playback = settings?.tts_playback;
  const rate = settings?.tts_rate;
  return {
    enabled: settings?.tts_enabled === true,
    playback:
      playback === 'all' || playback === 'current' || playback === 'never' ? playback : 'never',
    rate:
      typeof rate === 'number' && Number.isFinite(rate)
        ? Math.min(2, Math.max(0.5, rate))
        : DEFAULTS.rate
  };
}

export function applyTtsPreferences(settings: Record<string, unknown> | TtsPreferences): void {
  current =
    'playback' in settings && 'enabled' in settings
      ? ttsPreferencesFromSettings({
          tts_enabled: settings.enabled,
          tts_playback: settings.playback,
          tts_rate: settings.rate
        })
      : ttsPreferencesFromSettings(settings);
  if (typeof localStorage !== 'undefined') {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(current));
    } catch {
      // Private browsing may make storage unavailable; keep the in-memory choice.
    }
  }
}

export function currentTtsPreferences(): TtsPreferences {
  if (typeof localStorage !== 'undefined') {
    try {
      const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? 'null') as unknown;
      if (stored && typeof stored === 'object') {
        current = ttsPreferencesFromSettings({
          tts_enabled: (stored as Partial<TtsPreferences>).enabled,
          tts_playback: (stored as Partial<TtsPreferences>).playback,
          tts_rate: (stored as Partial<TtsPreferences>).rate
        });
      }
    } catch {
      // Ignore malformed or inaccessible client storage.
    }
  }
  return current;
}

export function ttsCommand(value: string): { matched: boolean; content: string } {
  const match = /^\/tts(?:\s+([\s\S]*))?$/iu.exec(value.trim());
  return match
    ? { matched: true, content: (match[1] ?? '').trim() }
    : { matched: false, content: '' };
}

export function shouldPlayTts(
  message: Pick<
    Message,
    | 'tts'
    | 'content'
    | 'channel_id'
    | 'channel_domain'
    | 'e2ee'
    | 'e2ee_verified'
    | 'decrypted_content'
  >,
  selectedChannelRef: string | null,
  preferences: TtsPreferences = currentTtsPreferences()
): boolean {
  const content = message.e2ee
    ? message.e2ee_verified === true
      ? message.decrypted_content
      : null
    : message.content;
  if (!preferences.enabled || !message.tts || !content?.trim()) return false;
  if (preferences.playback === 'all') return true;
  return (
    preferences.playback === 'current' &&
    selectedChannelRef === `${message.channel_id}@${message.channel_domain}`
  );
}

export function speakTtsMessage(
  message: Pick<
    Message,
    | 'tts'
    | 'content'
    | 'channel_id'
    | 'channel_domain'
    | 'author'
    | 'e2ee'
    | 'e2ee_verified'
    | 'decrypted_content'
  >,
  selectedChannelRef: string | null
): void {
  const preferences = currentTtsPreferences();
  if (
    typeof speechSynthesis === 'undefined' ||
    typeof SpeechSynthesisUtterance === 'undefined' ||
    !shouldPlayTts(message, selectedChannelRef, preferences)
  )
    return;
  const content = message.e2ee ? message.decrypted_content : message.content;
  const author = message.author?.display_name ?? message.author?.username;
  const utterance = new SpeechSynthesisUtterance(
    author ? `${author} said: ${content}` : (content ?? '')
  );
  utterance.rate = preferences.rate;
  speechSynthesis.speak(utterance);
}
