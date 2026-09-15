// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { mount, unmount } from 'svelte';
import InboxMessagePreview from './InboxMessagePreview.svelte';
import { chatEntities } from '$lib/stores/entities.svelte';
import type { Message } from '$lib/chat/types';
const network = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock('$lib/api/client', async (original) => ({
  ...(await original<typeof import('$lib/api/client')>()),
  api: network.api
}));
let component: ReturnType<typeof mount>;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.replaceChildren();
  chatEntities.messages.replace([]);
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});
it.each([false, true])(
  'previews only the requested unread range or mention (%s), without acknowledging',
  async (mention) => {
    vi.stubGlobal(
      'IntersectionObserver',
      class {
        constructor(private callback: IntersectionObserverCallback) {}
        observe(target: Element) {
          this.callback(
            [{ isIntersecting: true, target } as IntersectionObserverEntry],
            this as unknown as IntersectionObserver
          );
        }
        disconnect() {}
        unobserve() {}
      }
    );
    const message = (id: string, content: string): Message =>
      ({
        id,
        origin_domain: 'remote.example',
        channel_id: '2',
        channel_domain: 'home.example',
        author_id: '7',
        author_domain: 'home.example',
        author: {
          id: '7',
          origin_domain: 'home.example',
          username: 'Maya',
          display_name: null,
          avatar_hash: null
        },
        content,
        created_at: '2026-09-15T01:35:00Z',
        edited_at: null,
        flags: 0,
        message_type: 0,
        attachments: [],
        embeds: [],
        components: [],
        reactions: [],
        sticker_items: []
      }) as unknown as Message;
    network.api.mockResolvedValue([
      message('102', 'Later unread'),
      message('99', 'Already read'),
      message('100', 'First unread')
    ]);
    component = mount(InboxMessagePreview, {
      target: document.body,
      props: { channel: '2@home.example', target: '100@remote.example', mention }
    });
    await vi.waitFor(() => expect(document.body.textContent).toContain('First unread'));
    expect(document.body.textContent).not.toContain('Already read');
    expect(document.body.textContent?.includes('Later unread')).toBe(!mention);
    expect(network.api).toHaveBeenCalledExactlyOnceWith(
      '/channels/2%40home.example/messages?around=100%40remote.example&limit=5',
      expect.objectContaining({ signal: expect.any(AbortSignal) })
    );
    expect(document.querySelector('.message-hover-toolbar')).toBeNull();
  }
);
