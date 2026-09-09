// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { mount, unmount, flushSync } from 'svelte';
import Page from '../../routes/(app)/developers/[applicationRef]/+page.svelte';
const network = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock('$lib/api/client', async (original) => ({
  ...(await original<typeof import('$lib/api/client')>()),
  api: network.api
}));
const ref = '20@apps.example';
const root = `/applications/${encodeURIComponent(ref)}`;
let component: ReturnType<typeof mount>;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.innerHTML = '';
});
const button = (text: string) =>
  [...document.querySelectorAll('button')].find((b) => b.textContent?.trim() === text)!;
const field = (text: string) =>
  [...document.querySelectorAll('label')]
    .find((l) => l.textContent?.trim().startsWith(text))!
    .querySelector('input, select, textarea') as HTMLInputElement;
function input(text: string, value: string) {
  const el = field(text);
  el.value = value;
  if (el instanceof HTMLSelectElement) {
    // happy-dom does not implement :checked for options, which Svelte uses.
    const query = el.querySelector.bind(el);
    vi.spyOn(el, 'querySelector').mockImplementation((selector: string) =>
      selector === ':checked' ? (el.selectedOptions[0] ?? null) : query(selector)
    );
  }
  el.dispatchEvent(new Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
  flushSync();
}

let saved: Record<string, unknown>;
let saveGate: Promise<void>;
let rejectSave: boolean;
let ignoreSave: boolean;
beforeEach(async () => {
  saveGate = Promise.resolve();
  rejectSave = false;
  ignoreSave = false;
  saved = {
    ref,
    origin_domain: 'apps.example',
    name: 'Test app',
    description: '',
    support_url: null,
    privacy_url: null,
    terms_url: null,
    directory_enabled: false,
    directory_approved: false,
    directory_summary: null,
    directory_category: null,
    directory_tags: [],
    directory_media: [],
    directory_external_links: [],
    directory_supported_locales: [],
    directory_description_localizations: {},
    status: 'draft',
    target_policy: 'open',
    default_scopes: [],
    default_intents: [],
    default_permissions: '0',
    supported_install_types: ['guild_install'],
    user_install_scopes: ['applications.commands', 'interactions.respond'],
    user_install_contexts: ['guild'],
    e2ee_modes: ['participant'],
    bot_user: { handle: 'test@apps.example' }
  };
  network.api.mockReset().mockImplementation(async (path: string, options: RequestInit = {}) => {
    if (path === root) {
      if (options.method === 'PATCH') {
        await saveGate;
        if (rejectSave) throw new Error('Offline');
        if (!ignoreSave) saved = { ...saved, ...JSON.parse(String(options.body)) };
      }
      return structuredClone(saved);
    }
    if (path.endsWith('/directory-preview')) throw new Error('Preview unavailable');
    return [];
  });
  component = mount(Page, { target: document.body, props: { data: { applicationRef: ref } } });
  await vi.waitFor(() => expect(field('Support URL')).toBeTruthy());
});

it('saves the complete settings draft and retains it after reload', async () => {
  input('Support URL', 'https://apps.example/support');
  input('Privacy policy URL', 'https://apps.example/privacy');
  input('Terms of service URL', 'https://apps.example/terms');
  input('Category', 'utilities');
  input('Tags', 'tools');
  input('Language', 'fr');
  button('Add language').click();
  flushSync();
  field('messages.send').click();
  flushSync();
  document.querySelectorAll<HTMLInputElement>('#access .chips input').forEach((el) => {
    if (el.parentElement?.textContent?.trim() === 'guild_messages') el.click();
  });
  flushSync();
  const permission = document.querySelector(
    '.permission-checklist input[type="checkbox"]'
  ) as HTMLInputElement;
  permission.click();
  flushSync();
  button('Save changes').click();
  await vi.waitFor(() =>
    expect(document.body.textContent).toContain('Application settings saved.')
  );
  expect(saved).toMatchObject({
    support_url: 'https://apps.example/support',
    privacy_url: 'https://apps.example/privacy',
    terms_url: 'https://apps.example/terms',
    directory_category: 'utilities',
    directory_tags: ['tools'],
    directory_supported_locales: ['fr'],
    default_scopes: expect.arrayContaining(['messages.send']),
    default_intents: ['guild_messages']
  });
  expect(saved.default_permissions).not.toBe('0');
  expect(field('Support URL').value).toBe(saved.support_url);
  expect(document.querySelector('[aria-label="Remove Français"]')).toBeTruthy();
  await unmount(component);
  component = mount(Page, { target: document.body, props: { data: { applicationRef: ref } } });
  await vi.waitFor(() => expect(field('Support URL').value).toBe(saved.support_url));
  expect(field('messages.send').checked).toBe(true);
});

it('keeps settings when publishing commands refreshes the inventory', async () => {
  input('Support URL', 'https://apps.example/draft');
  field('messages.send').click();
  flushSync();
  button('Publish commands').click();
  await vi.waitFor(() => expect(document.body.textContent).toContain('Commands published.'));
  await vi.waitFor(() => expect(button('Save changes').disabled).toBe(false));
  expect(field('Support URL').value).toBe('https://apps.example/draft');
  expect(field('messages.send').checked).toBe(true);
  expect(document.querySelector('.save-card')?.textContent).toContain('Unsaved changes');
});

it('keeps edits made during a pending save marked as unsaved', async () => {
  let finish!: () => void;
  saveGate = new Promise<void>((resolve) => {
    finish = resolve;
  });
  input('Support URL', 'https://apps.example/first');
  button('Save changes').click();
  flushSync();
  expect(button('Saving…').disabled).toBe(true);
  input('Support URL', 'https://apps.example/newer');
  finish();
  await vi.waitFor(() => expect(button('Save changes').disabled).toBe(false));
  expect(saved.support_url).toBe('https://apps.example/first');
  expect(field('Support URL').value).toBe('https://apps.example/newer');
  expect(document.querySelector('.save-card')?.textContent).toContain('Unsaved changes');
  button('Save changes').click();
  await vi.waitFor(() => expect(button('Save changes').disabled).toBe(true));
  expect(saved.support_url).toBe('https://apps.example/newer');
});

it.each(['failure', 'unconfirmed response'])(
  'preserves the draft after %s and supports retry',
  async (failure) => {
    rejectSave = failure === 'failure';
    ignoreSave = failure === 'unconfirmed response';
    input('Support URL', 'https://apps.example/draft');
    button('Save changes').click();
    await vi.waitFor(() => expect(button('Retry save')).toBeDefined());
    expect(field('Support URL').value).toBe('https://apps.example/draft');
    expect(document.querySelector('.save-card')?.textContent).toContain('Settings not saved');
    rejectSave = ignoreSave = false;
    button('Retry save').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.save-card')?.textContent).toContain(
        'Application settings saved.'
      )
    );
    expect(saved.support_url).toBe('https://apps.example/draft');
  }
);

it('updates only edited fields, preserving settings changed since the page loaded', async () => {
  saved.support_url = 'https://apps.example/updated-elsewhere';
  saved.default_intents = ['guild_messages'];
  input('Name', 'Renamed app');
  button('Save changes').click();
  await vi.waitFor(() =>
    expect(document.querySelector('.save-card')?.textContent).toContain(
      'Application settings saved.'
    )
  );
  const request = network.api.mock.calls.find(([, options]) => options?.method === 'PATCH')!;
  expect(JSON.parse(request[1].body)).toEqual({ name: 'Renamed app' });
  expect(saved.support_url).toBe('https://apps.example/updated-elsewhere');
  expect(saved.default_intents).toEqual(['guild_messages']);
  expect(field('Support URL').value).toBe(saved.support_url);
});
