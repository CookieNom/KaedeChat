import type { Attachment, Message } from './types';

export const MESSAGE_FLAG_IS_VOICE_MESSAGE = 1 << 13;

export function isVoiceMessage(message: Pick<Message, 'flags' | 'attachments'>): boolean {
  const attachments = message.attachments ?? [];
  return (
    Boolean(message.flags & MESSAGE_FLAG_IS_VOICE_MESSAGE) &&
    attachments.length === 1 &&
    attachments[0].content_type.startsWith('audio/')
  );
}

export function voiceWaveformSamples(value: string | null | undefined): number[] {
  if (!value || value.length > 344 || !/^[A-Za-z0-9+/]+={0,2}$/u.test(value)) return [];
  try {
    const bytes = Uint8Array.from(atob(value), (character) => character.charCodeAt(0));
    if (!bytes.length || bytes.length > 256) return [];
    return [...bytes].map((sample) => Math.max(0.12, sample / 255));
  } catch {
    return [];
  }
}

export function voiceDurationLabel(seconds: number | null | undefined): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds <= 0) return 'Audio';
  const total = Math.min(1_200, Math.round(seconds));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

export function voiceAttachment(message: Message): Attachment | null {
  return isVoiceMessage(message) ? (message.attachments?.[0] ?? null) : null;
}
