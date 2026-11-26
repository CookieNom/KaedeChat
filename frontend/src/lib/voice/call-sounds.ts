import { asset } from '$app/paths';

export function ringtoneFromSetting(value: unknown): 'marimba' | 'silent' | 'chime' {
  return value === 'marimba' || value === 'silent' ? value : 'chime';
}

export function playCallSound(sound: 'join' | 'leave' | 'chime' | 'marimba', loop = false) {
  if (typeof Audio === 'undefined') return;
  const player = new Audio(asset(`sounds/${sound}.wav`));
  player.loop = loop;
  void player.play().catch(() => {
    // Browsers can require user interaction before allowing sound.
  });
  return player;
}
