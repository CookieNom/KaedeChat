// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import GifPicker from './GifPicker.svelte';
import { api } from '$lib/api/client';

vi.mock('$lib/api/client', () => ({ api: vi.fn(), userErrorMessage: () => 'Failed' }));
let component: ReturnType<typeof mount>;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});

it('loads near the bottom once, preserves results, and stops at the last page', async () => {
  let intersect: (entries: { isIntersecting: boolean }[]) => void;
  const disconnect = vi.fn();
  vi.stubGlobal(
    'IntersectionObserver',
    class {
      constructor(callback: typeof intersect) {
        intersect = callback;
      }
      observe = vi.fn();
      disconnect = disconnect;
    }
  );
  const gif = (id: string) => ({
    id,
    title: id,
    preview_url: `https://example.com/${id}.gif`,
    url: `https://example.com/${id}.gif`
  });
  vi.mocked(api)
    .mockResolvedValueOnce({ items: [gif('first')], next_page: 2 })
    .mockResolvedValueOnce({ items: [gif('second')], next_page: null });
  component = mount(GifPicker, {
    target: document.body,
    props: { onSelect: vi.fn(), onClose: vi.fn() }
  });
  flushSync();
  await vi.waitFor(() => expect(document.querySelector('[aria-label="first"]')).not.toBeNull());
  intersect!([{ isIntersecting: false }]);
  expect(api).toHaveBeenCalledTimes(1);
  intersect!([{ isIntersecting: true }]);
  intersect!([{ isIntersecting: true }]);
  expect(api).toHaveBeenCalledTimes(2);
  expect(api).toHaveBeenLastCalledWith('/gifs?page=2&limit=24', expect.anything());
  await vi.waitFor(() => expect(document.querySelector('[aria-label="second"]')).not.toBeNull());
  expect(document.querySelector('[aria-label="first"]')).not.toBeNull();
  intersect!([{ isIntersecting: true }]);
  expect(api).toHaveBeenCalledTimes(2);
  expect(disconnect).toHaveBeenCalled();
  expect(document.body.textContent).not.toContain('Load more');
});
