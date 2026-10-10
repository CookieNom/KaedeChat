// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { mount, unmount } from 'svelte';
import ReadInbox from './ReadInbox.svelte';
const network = vi.hoisted(() => ({ api: vi.fn(), goto: vi.fn() }));
vi.mock('$app/navigation', () => ({ goto: network.goto }));
vi.mock('$app/paths', () => ({ resolve: (path: string) => path }));
vi.mock('$lib/api/client', async (original) => ({
  ...(await original<typeof import('$lib/api/client')>()),
  api: network.api
}));
let component: ReturnType<typeof mount>;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(HTMLElement.prototype, 'showPopover');
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

it('loads private bookmarks, keeps failed removals, and jumps to the saved message', async () => {
  vi.stubGlobal(
    'IntersectionObserver',
    class {
      observe() {}
      disconnect() {}
    }
  );
  Object.defineProperty(HTMLElement.prototype, 'showPopover', {
    configurable: true,
    value: vi.fn()
  });
  const entry = {
    channel_id: '10',
    channel_domain: 'home.test',
    message_id: '20',
    message_domain: 'remote.test',
    channel_name: 'Friends',
    can_read_history: true
  };
  network.api.mockImplementation(async (path: string, init?: RequestInit) => {
    if (init?.method === 'DELETE') throw new Error('offline');
    return path.includes('/bookmarks') ? [entry] : [];
  });
  const close = vi.fn();
  component = mount(ReadInbox, { target: document.body, props: { onClose: close } });
  const tab = () => document.querySelector<HTMLButtonElement>('#inbox-tab-bookmarks')!;
  await vi.waitFor(() => expect(network.api).toHaveBeenCalled());
  await vi.waitFor(() => expect(tab().disabled).toBe(false));
  tab().click();
  await vi.waitFor(() => expect(document.body.textContent).toContain('Friends'));
  expect(tab().getAttribute('aria-selected')).toBe('true');
  expect(document.querySelectorAll('.inbox-actions button')).toHaveLength(1);
  document.querySelector<HTMLButtonElement>('[aria-label="Remove Bookmark"]')!.click();
  await vi.waitFor(() => expect(document.querySelector('[role="alert"]')).not.toBeNull());
  expect(document.body.textContent).toContain('Friends');
  document.querySelector<HTMLButtonElement>('.inbox-open')!.click();
  await vi.waitFor(() =>
    expect(network.goto).toHaveBeenCalledWith('/home/10%40home.test?around=20%40remote.test')
  );
  expect(close).toHaveBeenCalledOnce();
  network.api.mockResolvedValue([]);
  document.querySelector<HTMLButtonElement>('[aria-label="Remove Bookmark"]')!.click();
  await vi.waitFor(() =>
    expect(document.body.textContent).toContain('Collect your favorite memories')
  );
  expect(network.api).toHaveBeenCalledWith('/users/@me/inbox/bookmarks/20%40remote.test', {
    method: 'DELETE'
  });
});
