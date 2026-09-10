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
    expect(document.querySelector('.new-message-pill')).toBeNull();
  }
);
