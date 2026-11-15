import type { ScalabilityMode } from 'livekit-client';

/** Keep in step with mobile VideoCodecs and desktop's video codec policy. */
export const VIDEO_CODECS = {
  av1: { label: 'AV1', premium: true, modes: ['L3T3_KEY', 'L1T3', 'L1T1'] },
  h265: { label: 'HEVC (H.265)', premium: true, modes: ['L1T3', 'L1T1'] },
  vp9: { label: 'VP9', premium: false, modes: ['L3T3_KEY', 'L1T3', 'L1T1'] },
  vp8: { label: 'VP8', premium: false, modes: ['L1T3', 'L1T1'] },
  h264: { label: 'H.264', premium: false, modes: ['L1T3', 'L1T1'] }
} as const satisfies Record<
  string,
  { label: string; premium: boolean; modes: readonly ScalabilityMode[] }
>;
export type Codec = keyof typeof VIDEO_CODECS;
export const CODECS = Object.keys(VIDEO_CODECS) as Codec[];
export const LEGACY_CODECS: readonly Codec[] = ['av1', 'vp8', 'h264'];
export const EXTENDED_CODECS: readonly Codec[] = ['h265', 'vp9'];
// Order breaks equal-audience ties, including viewers that support both codecs.
export const PREMIUM_CODECS = CODECS.filter((codec) => VIDEO_CODECS[codec].premium);
export function isPremiumCodec(codec: string | undefined): codec is Codec {
  return PREMIUM_CODECS.includes(codec as Codec);
}
