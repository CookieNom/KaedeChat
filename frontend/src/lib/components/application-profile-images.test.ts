// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { mount, unmount } from 'svelte';
import ApplicationMediaManager from './ApplicationMediaManager.svelte';
const mocks = vi.hoisted(() => ({ api: vi.fn(), upload: vi.fn() }));
vi.mock('$lib/api/client', () => ({
  api: mocks.api,
  userErrorMessage: (error: Error) => error.message
}));
vi.mock('$lib/media/uploads', () => ({ uploadObject: mocks.upload }));
let component: ReturnType<typeof mount>;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.innerHTML = '';
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it('uploads both profile images, waits for scanning, and removes the active image', async () => {
  const assets: Record<string, unknown>[] = [];
  let commits = 0;
  mocks.upload.mockResolvedValue(undefined);
  mocks.api.mockImplementation(async (path: string, options: RequestInit = {}) => {
    if (path.endsWith('/tickets')) return { id: '50' };
    if (options.method === 'POST') {
      const payload = JSON.parse(String(options.body));
      if (++commits === 1)
        return {
          status: 'processing',
          application_ref: '20@apps.example',
          attachment: { scan_status: 'pending' }
        };
      const asset = {
        ...payload,
        id: String(commits),
        application_ref: '20@apps.example',
        media_hash: payload.kind === 'icon' ? 'a'.repeat(64) : 'b'.repeat(64),
        version: 1
      };
      assets.push(asset);
      return asset;
    }
    if (options.method === 'DELETE') return {};
    return [];
  });
  component = mount(ApplicationMediaManager, {
    target: document.body,
    props: { applicationRef: '20@apps.example' }
  });
  await vi.waitFor(() => expect(document.querySelector('#application-icon')).not.toBeNull());
  async function select(kind: string) {
    const input = document.querySelector<HTMLInputElement>(`#application-${kind}`)!;
    const file = new File(['image'], 'photo.png', { type: 'image/png' });
    Object.defineProperty(input, 'files', { value: [file], configurable: true });
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await vi.waitFor(
      () =>
        expect(
          document.querySelector(
            `.profile-preview${kind === 'icon' ? '.avatar' : ':not(.avatar)'} img`
          )
        ).not.toBeNull(),
      { timeout: 2500 }
    );
  }
  await select('icon');
  expect(commits).toBe(2);
  await select('cover');
  expect(assets.map((asset) => asset.kind)).toEqual(['icon', 'cover']);
  expect(document.body.textContent).toContain('No need to save app settings again');
  vi.stubGlobal('confirm', () => true);
  [...document.querySelectorAll('button')]
    .find((button) => button.textContent?.trim() === 'Remove profile picture')!
    .click();
  await vi.waitFor(() => expect(document.querySelector('.avatar img')).toBeNull());
  expect(document.querySelector('.profile-preview:not(.avatar) img')).not.toBeNull();
});

it('keeps the existing image when uploading fails', async () => {
  mocks.api.mockImplementation(async (path: string) => {
    if (path.endsWith('/tickets')) throw new Error('Upload failed');
    return [];
  });
  component = mount(ApplicationMediaManager, {
    target: document.body,
    props: { applicationRef: '20@apps.example', iconHash: 'a'.repeat(64) }
  });
  await vi.waitFor(() => expect(document.querySelector('#application-icon')).not.toBeNull());
  const input = document.querySelector<HTMLInputElement>('#application-icon')!;
  Object.defineProperty(input, 'files', {
    value: [new File(['image'], 'photo.png', { type: 'image/png' })]
  });
  input.dispatchEvent(new Event('change', { bubbles: true }));
  await vi.waitFor(() =>
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('Upload failed')
  );
  expect(document.querySelector('.avatar img')?.getAttribute('src')).toContain('a'.repeat(64));
});
