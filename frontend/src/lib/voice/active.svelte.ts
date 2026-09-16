import type { VoiceSession } from './session';

export const activeVoice = $state<{ session: VoiceSession | null; revision: number }>({
  session: null,
  revision: 0
});
