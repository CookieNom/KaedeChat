export interface TypingParticipant {
  ref: string;
  name: string;
  expiresAt: number;
}

export function upsertTypingParticipant(
  participants: readonly TypingParticipant[],
  participant: Omit<TypingParticipant, 'expiresAt'>,
  now = Date.now(),
  lifetimeMs = 10_000
): TypingParticipant[] {
  return [
    ...participants.filter((item) => item.ref !== participant.ref && item.expiresAt > now),
    { ...participant, expiresAt: now + lifetimeMs }
  ];
}

export function activeTypingParticipants(
  participants: readonly TypingParticipant[],
  now = Date.now()
): TypingParticipant[] {
  return participants.filter((item) => item.expiresAt > now);
}

export function typingLabel(participants: readonly TypingParticipant[]): string {
  const names = participants.map((item) => item.name);
  if (names.length === 0) return '';
  if (names.length === 1) return `${names[0]} is typing…`;
  if (names.length === 2) return `${names[0]} and ${names[1]} are typing…`;
  if (names.length === 3) return `${names[0]}, ${names[1]}, and ${names[2]} are typing…`;
  return `${names[0]}, ${names[1]}, and ${names.length - 2} more are typing…`;
}

/** Own one conversation's expiry timer; reset also disposes it on navigation. */
export function createTypingState(onLabel: (label: string) => void) {
  let participants: TypingParticipant[] = [];
  let timer: ReturnType<typeof setTimeout> | null = null;
  function reset() {
    participants = [];
    onLabel('');
    if (timer !== null) clearTimeout(timer);
    timer = null;
  }
  function refresh() {
    participants = activeTypingParticipants(participants);
    onLabel(typingLabel(participants));
    if (timer !== null) clearTimeout(timer);
    timer = null;
    if (!participants.length) return;
    const nextExpiry = Math.min(...participants.map((item) => item.expiresAt));
    timer = setTimeout(refresh, Math.max(50, nextExpiry - Date.now() + 5));
  }
  return {
    reset,
    register(participant: Omit<TypingParticipant, 'expiresAt'>) {
      participants = upsertTypingParticipant(participants, participant);
      refresh();
    }
  };
}
