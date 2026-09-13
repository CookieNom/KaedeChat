// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, tick, unmount } from 'svelte';
import LanguageSuggestion from './LanguageSuggestion.svelte';
import { applyLocale, markLanguageChosen, setSystemLanguages } from '$lib/ui/locale';
const { api } = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api }));
let component: ReturnType<typeof mount> | undefined;
async function render() {
  component = mount(LanguageSuggestion, { target: document.body });
  await tick();
}
beforeEach(() => {
  localStorage.clear();
  document.body.innerHTML = '';
  applyLocale('en-US');
  api.mockReset();
});
afterEach(async () => {
  if (component) await unmount(component);
  component = undefined;
  setSystemLanguages();
});
describe('language suggestion pane', () => {
  it('dismisses when English is explicitly chosen in settings', async () => {
    setSystemLanguages(['ja-JP']);
    await render();
    expect(document.querySelector('aside')).not.toBeNull();
    applyLocale('en');
    markLanguageChosen();
    await tick();
    expect(document.querySelector('aside')).toBeNull();
  });
  it('does not appear for an English device', async () => {
    setSystemLanguages(['en-GB', 'ja-JP']);
    await render();
    expect(document.querySelector('aside')).toBeNull();
  });
  it('shows both languages and remembers the red X dismissal', async () => {
    setSystemLanguages(['ja-JP']);
    await render();
    expect(document.querySelector('p[lang="en"]')?.textContent).toContain('Your device language');
    expect(document.querySelector('p[lang="ja"]')?.textContent).toContain('お使いの端末');
    const reject = document.querySelector<HTMLButtonElement>('button.decline')!;
    expect(reject.textContent).toBe('✕');
    reject.click();
    await tick();
    expect(document.querySelector('aside')).toBeNull();
    expect(localStorage.getItem('kaede.language-choice')).toBe('1');
    expect(api).not.toHaveBeenCalled();
  });
  it('accepts with the green check and saves system default', async () => {
    api.mockResolvedValue({ locale: 'system' });
    setSystemLanguages(['ja-JP']);
    await render();
    const accept = document.querySelector<HTMLButtonElement>('button.accept')!;
    expect(accept.textContent).toBe('✓');
    accept.click();
    await tick();
    await tick();
    expect(api).toHaveBeenCalledWith(
      '/users/@me/settings',
      expect.objectContaining({ body: JSON.stringify({ locale: 'system' }) })
    );
    expect(document.documentElement.lang).toBe('ja');
    expect(document.querySelector('aside')).toBeNull();
  });
  it('keeps the choice available after a failed save', async () => {
    api.mockRejectedValue(new Error('offline'));
    setSystemLanguages(['ja-JP']);
    await render();
    document.querySelector<HTMLButtonElement>('button.accept')!.click();
    await tick();
    await tick();
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('Could not save');
    expect(localStorage.getItem('kaede.language-choice')).toBeNull();
    expect(document.documentElement.lang).toBe('en');
  });
});
