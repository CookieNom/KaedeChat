// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, tick, unmount } from 'svelte';
import VirtualMessageList from './VirtualMessageList.svelte';

let component: ReturnType<typeof mount>;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});

it.each([true, false])(
  'preserves bottom pinning (%s) when history loads before a retained ephemeral response',
  async (atBottom) => {
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        disconnect() {}
      }
    );
    const props = $state({ items: [{ key: 'ephemeral:20@chat.example' }] });
    component = mount(VirtualMessageList, {
      target: document.body,
      props: {
        get items() {
          return props.items;
        },
        renderItem: createRawSnippet(() => ({ render: () => '<p>Bot response</p>' }))
      }
    });
    flushSync();
    const viewport = document.querySelector<HTMLElement>('.virtual-message-viewport')!;
    let height = 1000;
    Object.defineProperties(viewport, {
      scrollHeight: { get: () => height },
      clientHeight: { value: 400 }
    });
    viewport.scrollTo = (options?: ScrollToOptions | number, y?: number) => {
      viewport.scrollTop = Number(typeof options === 'number' ? y : options?.top);
    };
    await tick();
    await tick();
    viewport.scrollTop = atBottom ? 600 : 300;
    viewport.dispatchEvent(new Event('scroll'));

    height = 1800;
    props.items = [{ key: 'message:10@chat.example' }, ...props.items];
    flushSync();
    await tick();
    await tick();

    expect(viewport.scrollTop).toBe(atBottom ? height : 300);
    expect(Boolean(document.querySelector('.new-message-pill'))).toBe(!atBottom);
  }
);

it('jumps directly to latest history and keeps the saved-position control available', async () => {
  const latest = vi.fn();
  const saved = vi.fn();
  const bottom = vi.fn();
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    }
  );
  component = mount(VirtualMessageList, {
    target: document.body,
    props: {
      items: [{ key: 'message:10@chat.example' }],
      hasLater: true,
      canJumpToRead: true,
      onJumpToLatest: latest,
      onJumpToRead: saved,
      onBottomChange: bottom,
      renderItem: createRawSnippet(() => ({ render: () => '<p>Old message</p>' }))
    }
  });
  flushSync();
  await tick();
  await tick();
  document.querySelector<HTMLButtonElement>('.new-message-pill')!.click();
  document.querySelector<HTMLButtonElement>('.read-position-banner')!.click();
  expect(latest).toHaveBeenCalledOnce();
  expect(saved).toHaveBeenCalledOnce();
  expect(bottom).not.toHaveBeenCalledWith(true);
  const viewport = document.querySelector<HTMLElement>('.virtual-message-viewport')!;
  viewport.dispatchEvent(
    new KeyboardEvent('keydown', { key: 'PageUp', shiftKey: true, bubbles: true })
  );
  viewport.dispatchEvent(
    new KeyboardEvent('keydown', { key: 'End', ctrlKey: true, bubbles: true })
  );
  expect(saved).toHaveBeenCalledTimes(2);
  expect(latest).toHaveBeenCalledTimes(2);
});

it('acknowledges only visible messages in a focused tab while catching up', async () => {
  vi.useFakeTimers();
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    }
  );
  const focused = vi.spyOn(document, 'hasFocus').mockReturnValue(false);
  const read = vi.fn();
  component = mount(VirtualMessageList, {
    target: document.body,
    props: {
      items: [{ key: 'message:10@chat.example' }, { key: 'message:20@chat.example' }],
      hasLater: true,
      onRead: read,
      renderItem: createRawSnippet(() => ({ render: () => '<p>History</p>' }))
    }
  });
  flushSync();
  await tick();
  await tick();
  const viewport = document.querySelector<HTMLElement>('.virtual-message-viewport')!;
  viewport.getBoundingClientRect = () => ({ top: 0, bottom: 400 }) as DOMRect;
  const rows = document.querySelectorAll<HTMLElement>('[data-virtual-key]');
  rows[0].getBoundingClientRect = () => ({ top: 100, bottom: 200 }) as DOMRect;
  rows[1].getBoundingClientRect = () => ({ top: 500, bottom: 600 }) as DOMRect;
  viewport.dispatchEvent(new Event('scroll'));
  await vi.advanceTimersByTimeAsync(250);
  expect(read).not.toHaveBeenCalled();
  focused.mockReturnValue(true);
  window.dispatchEvent(new Event('focus'));
  await vi.advanceTimersByTimeAsync(250);
  expect(read).toHaveBeenCalledExactlyOnceWith('message:10@chat.example');
  focused.mockRestore();
  vi.useRealTimers();
});
