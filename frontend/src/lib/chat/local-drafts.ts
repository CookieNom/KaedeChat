// Drafts deliberately never enter account settings, the gateway, or API payloads.
const memory = new Map<string, string>();

export function draftKey(account: string, conversation: string): string {
  return `kaede.draft.v1.${encodeURIComponent(account)}.${encodeURIComponent(conversation)}`;
}

export function readDraft(key: string): string {
  if (memory.has(key)) return memory.get(key)!;
  try {
    return localStorage.getItem(key) ?? '';
  } catch {
    return memory.get(key) ?? '';
  }
}

export function writeDraft(key: string, text: string): void {
  memory.set(key, text);
  try {
    if (text) localStorage.setItem(key, text);
    else localStorage.removeItem(key);
  } catch {
    // Keep the draft for this session if device storage is full or unavailable.
  }
}
