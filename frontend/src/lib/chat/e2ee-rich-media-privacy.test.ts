// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, tick } from 'svelte';
import { createClassComponent } from 'svelte/legacy';
import RichEmbed from '$lib/components/RichEmbed.svelte';
import MessageComponents from '$lib/components/MessageComponents.svelte';
import MessageRow from '$lib/components/MessageRow.svelte';
import ForwardedMessage from '$lib/components/ForwardedMessage.svelte';
import EphemeralInteractionTray from '$lib/components/EphemeralInteractionTray.svelte';
import {
  interactionResponses,
  type InteractionRequestContext
} from './interaction-responses.svelte';
import type { Message } from './types';
const calls = vi.hoisted(() => ({ api: vi.fn(), media: vi.fn(), decrypt: vi.fn() }));
vi.mock('$lib/api/client', async (original) => ({
  ...(await original<typeof import('$lib/api/client')>()),
  api: calls.api
}));
vi.mock('$lib/media/authenticated', async (original) => ({
  ...(await original<typeof import('$lib/media/authenticated')>()),
  authenticatedMedia: calls.media
}));
vi.mock('$lib/e2ee/media', async (original) => ({
  ...(await original<typeof import('$lib/e2ee/media')>()),
  downloadEncryptedFile: calls.decrypt
}));
let component: ReturnType<typeof createClassComponent> | undefined;
const urls = ['author', 'image', 'thumbnail', 'footer'].map(
  (part) => `https://external.example/${part}.png`
);
const embed = {
  author: { name: 'Author', icon_url: urls[0] },
  image: { url: urls[1] },
  thumbnail: { url: urls[2] },
  footer: { text: 'Footer', icon_url: urls[3] }
};
const layout = {
  type: 12 as const,
  items: [{ media: { url: urls[1] }, description: 'Authored image' }]
};
const message = {
  id: '3',
  origin_domain: 'chat.example',
  channel_id: '2',
  channel_domain: 'chat.example',
  content: '',
  flags: 0,
  message_type: 0,
  created_at: '2026-01-01T00:00:00Z',
  edited_at: null,
  author: {
    id: '7',
    origin_domain: 'chat.example',
    username: 'author',
    display_name: null,
    avatar_hash: null
  },
  attachments: [],
  embeds: [embed],
  components: [layout],
  sticker_items: [],
  reactions: []
} as unknown as Message;
const previews = () => calls.api.mock.calls.filter(([path]) => path === '/link-previews');
beforeEach(() => {
  calls.api
    .mockReset()
    .mockImplementation(async (path, options) =>
      path === '/link-previews'
        ? {
            url: JSON.parse(options.body).url,
            media_url: JSON.parse(options.body).url,
            media_type: 'image'
          }
        : { source_channel_ref: '2@chat.example', source_message_ref: '3@chat.example' }
    );
  calls.media.mockClear();
  calls.decrypt.mockClear();
});
afterEach(() => {
  component?.$destroy();
  component = undefined;
  document.body.replaceChildren();
  interactionResponses.reset();
});
const noExternalActivity = () => {
  expect(previews()).toEqual([]);
  expect(document.querySelector('img[src], video[src], audio[src]')).toBeNull();
  expect(calls.media).not.toHaveBeenCalled();
  expect(calls.decrypt).not.toHaveBeenCalled();
};
describe('encrypted rich media privacy', () => {
  it('gates every automatic embed preview behind the external-media policy', async () => {
    component = createClassComponent({
      component: RichEmbed,
      target: document.body,
      props: { embed, allowExternalMedia: false }
    });
    flushSync();
    await tick();
    noExternalActivity();
    expect(
      Array.from(document.querySelectorAll('a'))
        .map((a) => a.href)
        .sort()
    ).toEqual([...urls].sort());
    component.$set({ allowExternalMedia: true });
    await tick();
    await vi.waitFor(() => expect(document.querySelectorAll('img[src]')).toHaveLength(4));
    expect(
      previews()
        .map(([, options]) => JSON.parse(options.body).url)
        .sort()
    ).toEqual([...urls].sort());
    calls.api.mockClear();
    component.$set({ allowExternalMedia: false });
    await tick();
    noExternalActivity();
  });
  it('never auto-loads authored V2 media while the policy is disabled', async () => {
    component = createClassComponent({
      component: MessageComponents,
      target: document.body,
      props: { components: [layout], channel: '2@chat.example', allowExternalMedia: false }
    });
    flushSync();
    await tick();
    noExternalActivity();
    expect(document.querySelector('a')?.href).toBe(urls[1]);
    component.$set({ allowExternalMedia: true });
    await tick();
    expect(document.querySelector('img')?.src).toBe(urls[1]);
    component.$set({ allowExternalMedia: false });
    await tick();
    noExternalActivity();
  });
  it('passes a fail-closed policy through messages, forwards, and private responses', async () => {
    component = createClassComponent({
      component: MessageRow,
      target: document.body,
      props: {
        message: {
          ...message,
          e2ee: {},
          e2ee_verified: false,
          decrypted_content: 'Untrusted plaintext'
        }
      }
    });
    flushSync();
    await tick();
    noExternalActivity();
    expect(document.body.textContent).not.toContain('Untrusted plaintext');
    component.$set({
      message: {
        ...message,
        e2ee: {},
        e2ee_verified: true,
        decrypted_content: 'Verified plaintext'
      }
    });
    await tick();
    expect(document.body.textContent).toContain('Verified plaintext');
    expect(document.querySelector(`a[href="${urls[1]}"]`)).not.toBeNull();
    noExternalActivity();
    component.$destroy();
    document.body.replaceChildren();
    component = createClassComponent({
      component: ForwardedMessage,
      target: document.body,
      props: {
        message: {
          ...message,
          message_snapshots: [
            {
              message: {
                ...message,
                embeds: [embed],
                components: [layout],
                sticker_items: [],
                attachments: []
              }
            }
          ]
        },
        allowExternalMedia: false,
        allowEncryptedManifests: false
      }
    });
    flushSync();
    await tick();
    expect(document.querySelector(`a[href="${urls[1]}"]`)).not.toBeNull();
    noExternalActivity();
    component.$destroy();
    document.body.replaceChildren();
    interactionResponses.contexts['10@chat.example'] = {
      channelRef: '2@chat.example',
      applicationRef: '11@chat.example',
      e2ee: {}
    } as InteractionRequestContext;
    interactionResponses.byResponse['12@chat.example'] = {
      interaction_id: '10',
      interaction_ref: '10@chat.example',
      response_ref: '12@chat.example',
      ephemeral: true,
      callback_type: 4,
      data: { embeds: [embed], components: [layout] }
    };
    component = createClassComponent({
      component: EphemeralInteractionTray,
      target: document.body,
      props: { channelRef: '2@chat.example' }
    });
    flushSync();
    await tick();
    expect(document.querySelector('[aria-label="Private bot responses"]')).not.toBeNull();
    expect(document.querySelector(`a[href="${urls[1]}"]`)).not.toBeNull();
    noExternalActivity();
  });
});
