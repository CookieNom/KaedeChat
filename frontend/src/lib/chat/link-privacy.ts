// Keep the original URL spelling: only remove query fields named `si`.
export function stripLinkTracking(content: string): string {
  return content.replace(/https?:\/\/[^\s<>"`]+/gi, (match) => {
    const suffix = match.match(/[.,!;:)\]]+$/)?.[0] ?? '';
    const raw = match.slice(0, match.length - suffix.length);
    let url: URL;
    try {
      url = new URL(raw);
    } catch {
      return match;
    }
    const host = url.hostname.toLowerCase();
    if (!(
      host === 'youtu.be' ||
      host === 'youtube.com' ||
      host.endsWith('.youtube.com') ||
      host === 'open.spotify.com'
    ))
      return match;
    const question = raw.indexOf('?');
    const hash = raw.indexOf('#');
    if (question < 0 || (hash >= 0 && hash < question)) return match;
    const end = hash < 0 ? raw.length : hash;
    const fields = raw.slice(question + 1, end).split('&');
    const kept = fields.filter((field) => new URLSearchParams(field).keys().next().value !== 'si');
    if (kept.length === fields.length) return match;
    return (
      raw.slice(0, question) + (kept.length ? '?' + kept.join('&') : '') + raw.slice(end) + suffix
    );
  });
}

const choices = new Map<string, boolean>();
const prompts = new Map<string, Promise<boolean>>();

function ask(): Promise<boolean> {
  return new Promise((resolve) => {
    const dialog = document.createElement('dialog');
    dialog.setAttribute('aria-labelledby', 'link-privacy-title');
    dialog.style.cssText =
      'width:28rem;max-width:calc(100vw - 2rem);padding:1.5rem;border-radius:12px;border:1px solid var(--line);background:var(--surface);color:var(--text);box-shadow:var(--shadow-lg);';
    const title = document.createElement('h2');
    title.id = 'link-privacy-title';
    title.textContent = 'Remove link tracking?';
    const description = document.createElement('p');
    description.style.cssText = 'margin:1rem 0;line-height:1.5;';
    description.textContent =
      'YouTube and Spotify sharing links can contain an si tracking code that may reveal information about how you shared the link. Automatically remove only si when sending this and future messages? Your choice is saved for this account on this device.';
    const actions = document.createElement('div');
    actions.style.cssText = 'display:flex;gap:.75rem;flex-wrap:wrap;';
    dialog.append(title, description, actions);
    for (const [label, enabled] of [
      ['Keep tracking', false],
      ['Remove tracking', true]
    ] as const) {
      const button = document.createElement('button');
      button.textContent = label;
      button.className = enabled ? 'primary-button' : 'secondary-button';
      button.style.flex = '1';
      button.onclick = () => {
        dialog.close();
        dialog.remove();
        resolve(enabled);
      };
      actions.append(button);
    }
    dialog.addEventListener('cancel', (event) => event.preventDefault());
    document.body.append(dialog);
    dialog.showModal();
  });
}

export async function preparePrivateLinks(
  content: string,
  account: string,
  prompt = ask
): Promise<string> {
  const stripped = stripLinkTracking(content);
  if (stripped === content) return content;
  const key = `kaede:strip-link-si:${account}`;
  let enabled = choices.get(key);
  if (enabled === undefined) {
    try {
      const saved = localStorage.getItem(key);
      if (saved === 'true' || saved === 'false') enabled = saved === 'true';
    } catch {
      /* Session memory still remembers the choice when storage is unavailable. */
    }
  }
  if (enabled === undefined) {
    let pending = prompts.get(key);
    if (!pending) {
      pending = prompt();
      prompts.set(key, pending);
    }
    try {
      enabled = await pending;
    } finally {
      prompts.delete(key);
    }
    choices.set(key, enabled);
    try {
      localStorage.setItem(key, String(enabled));
    } catch {
      /* Use session memory. */
    }
  }
  return enabled ? stripped : content;
}
