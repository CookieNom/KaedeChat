// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { mount, unmount, flushSync } from 'svelte';
import Page from '../../routes/(app)/developers/[applicationRef]/+page.svelte';
const network = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock('$lib/api/client', async (original) => ({
  ...(await original<typeof import('$lib/api/client')>()),
  api: network.api
}));
vi.mock('$lib/media/uploads', () => ({ uploadObject: vi.fn().mockResolvedValue(undefined) }));
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
let rejectCommandPublish: boolean;
let publishedCommands: unknown[];
const navigate = (title: string) => {
  [...document.querySelectorAll<HTMLButtonElement>('[aria-label="Application sections"] button')]
    .find((item) => item.querySelector('strong')?.textContent === title)!
    .click();
  flushSync();
};
const addCommand = (type: string) => {
  [...document.querySelectorAll<HTMLButtonElement>('.add-kind')]
    .find((item) => item.querySelector('strong')?.textContent === `+ ${type}`)!
    .click();
  flushSync();
};
beforeEach(async () => {
  saveGate = Promise.resolve();
  rejectSave = false;
  ignoreSave = false;
  rejectCommandPublish = false;
  publishedCommands = [];
  saved = {
    ref,
    origin_domain: 'apps.example',
    name: 'Test app',
    icon_hash: null,
    banner_hash: null,
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
    bot_user: {
      id: '91287871893315585',
      origin_domain: 'bots.example',
      handle: 'test@bots.example'
    }
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
    if (path.endsWith('/commands')) {
      if (options.method === 'PUT') {
        if (rejectCommandPublish) throw new Error('Offline');
        publishedCommands = JSON.parse(String(options.body)).commands;
        return { commands: publishedCommands.length };
      }
      return structuredClone(publishedCommands);
    }
    if (path.endsWith('/credentials') && options.method === 'POST') return { token: 'test-token' };
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

it('keeps settings and command drafts when credential actions refresh inventory', async () => {
  input('Support URL', 'https://apps.example/draft');
  field('messages.send').click();
  flushSync();
  navigate('Commands');
  addCommand('Slash command');
  input('Command name', 'help');
  input('Command description', 'Get help');
  navigate('Credentials & workers');
  button('Create credential').click();
  await vi.waitFor(() =>
    expect(document.body.textContent).toContain('Control credential created.')
  );
  await vi.waitFor(() => expect(button('Save changes').disabled).toBe(false));
  expect(field('Support URL').value).toBe('https://apps.example/draft');
  expect(field('messages.send').checked).toBe(true);
  expect(document.querySelector('.save-card')?.textContent).toContain('Unsaved changes');
  navigate('Commands');
  expect(field('Command name').value).toBe('help');
  expect(button('Publish commands').disabled).toBe(false);
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

it('builds and publishes slash commands with inputs without requiring JSON', async () => {
  navigate('Commands');
  expect(document.querySelector('#commands textarea')).toBeNull();
  expect(document.querySelector('#commands')?.textContent).toContain('Create your first command');
  expect(button('Publish commands').disabled).toBe(true);
  addCommand('Slash command');
  button('Publish commands').click();
  flushSync();
  expect(publishedCommands).toEqual([]);
  expect(document.querySelector('.save-card')?.textContent).toContain('Give every command a name');
  input('Command name', 'search');
  input('Command description', 'Search the community');
  button('+ Add input').click();
  flushSync();
  input('Input name', 'query');
  input('Input description', 'What to look for');
  field('Required').click();
  flushSync();
  button('Publish commands').click();
  await vi.waitFor(() =>
    expect(document.querySelector('.save-card')?.textContent).toContain('Commands published.')
  );
  expect(publishedCommands).toEqual([
    {
      type: 'chat_input',
      name: 'search',
      description: 'Search the community',
      integration_types: ['guild_install'],
      contexts: ['guild'],
      options: [{ type: 'string', name: 'query', description: 'What to look for', required: true }]
    }
  ]);
  expect(button('Publish commands').disabled).toBe(true);
});

it.each(['User action', 'Message action'])(
  'creates a %s without slash-only fields',
  async (kind) => {
    navigate('Commands');
    addCommand(kind);
    input('Command name', 'View details');
    expect(document.querySelector('#commands')?.textContent).not.toContain('Command description');
    button('Publish commands').click();
    await vi.waitFor(() => expect(publishedCommands).toHaveLength(1));
    expect(publishedCommands[0]).toEqual({
      type: kind === 'User action' ? 'user' : 'message',
      name: 'View details',
      integration_types: ['guild_install'],
      contexts: ['guild']
    });
  }
);

it('preserves advanced definitions when switching to the form and editing a name', async () => {
  const definition = {
    type: 'chat_input',
    name: 'search',
    description: 'Search',
    nsfw: false,
    name_localizations: { fr: 'chercher' },
    default_member_permissions: ['VIEW_CHANNEL'],
    options: [
      {
        type: 'string',
        name: 'query',
        description: 'Query',
        choices: [{ name: 'Docs', value: 'docs' }],
        min_length: 1
      }
    ]
  };
  navigate('Commands');
  button('Advanced JSON').click();
  flushSync();
  input('Command definitions (JSON)', JSON.stringify([definition]));
  button('Use form editor').click();
  flushSync();
  input('Command name', 'find');
  expect(field('Input type').disabled).toBe(true);
  button('Publish commands').click();
  await vi.waitFor(() => expect(publishedCommands).toHaveLength(1));
  expect(publishedCommands[0]).toEqual({ ...definition, name: 'find' });
});

it('keeps failed command drafts and supports removal with undo before publication', async () => {
  navigate('Commands');
  addCommand('User action');
  input('Command name', 'Profile');
  rejectCommandPublish = true;
  button('Publish commands').click();
  await vi.waitFor(() =>
    expect(document.querySelector('.save-card')?.textContent).toContain('Commands not published')
  );
  expect(field('Command name').value).toBe('Profile');
  rejectCommandPublish = false;
  button('Publish commands').click();
  await vi.waitFor(() =>
    expect(document.querySelector('.save-card')?.textContent).toContain('Commands published.')
  );
  button('Remove command').click();
  flushSync();
  expect(document.querySelector('#commands')?.textContent).toContain(
    'Command removed from the draft'
  );
  button('Undo').click();
  flushSync();
  expect(field('Command name').value).toBe('Profile');
  button('Remove command').click();
  flushSync();
  button('Publish commands').click();
  await vi.waitFor(() => expect(publishedCommands).toEqual([]));
});

it('saves profile artwork immediately without marking app settings as unsaved', async () => {
  const original = network.api.getMockImplementation()!;
  network.api.mockImplementation(async (path: string, options: RequestInit = {}) => {
    if (path.endsWith('/assets/tickets')) return { id: '50' };
    if (path.endsWith('/assets') && options.method === 'POST') {
      saved.icon_hash = 'a'.repeat(64);
      return {
        id: '50',
        application_ref: ref,
        kind: 'icon',
        name: 'Profile picture',
        media_hash: saved.icon_hash,
        version: 1
      };
    }
    return original(path, options);
  });
  const picker = document.querySelector<HTMLInputElement>('#application-icon')!;
  Object.defineProperty(picker, 'files', {
    value: [new File(['image'], 'photo.png', { type: 'image/png' })]
  });
  picker.dispatchEvent(new Event('change', { bubbles: true }));
  await vi.waitFor(() => expect(document.querySelector('.avatar img')).not.toBeNull());
  expect(button('Save changes').disabled).toBe(true);
  input('Name', 'Unsaved bot name');
  expect(button('Save changes').disabled).toBe(false);
  expect(document.querySelector('.avatar img')?.getAttribute('src')).toContain('a'.repeat(64));
});

it('shows the exact bot snowflake and authority-qualified reference separately from the application ref', () => {
  expect((document.querySelector('#bot-snowflake') as HTMLInputElement).value).toBe(
    '91287871893315585'
  );
  expect((document.querySelector('#bot-reference') as HTMLInputElement).value).toBe(
    '91287871893315585@bots.example'
  );
  expect(document.querySelector('.identity-help')?.textContent).toContain(ref);
});
