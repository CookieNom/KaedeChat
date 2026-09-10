// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import EphemeralInteractionTray from './EphemeralInteractionTray.svelte';

const { response, createInteraction } = vi.hoisted(() => ({
  response: {
    ephemeral: true,
    callback_type: 4,
    interaction_ref: '10@chat.example',
    response_ref: '20@chat.example',
    response_id: '20',
    data: {
      view_version: 1 as unknown,
      components: [
        { type: 1, components: [{ type: 2, style: 1, label: 'Confirm', custom_id: 'confirm' }] }
      ]
    }
  },
  createInteraction: vi.fn().mockResolvedValue({})
}));

vi.mock('$lib/chat/interaction-responses.svelte', () => ({
  interactionResponses: {
    byResponse: { '20@chat.example': response },
    context: () => ({ channelRef: '2@chat.example', applicationRef: '3@chat.example' }),
    clear: vi.fn()
  }
}));
vi.mock('$lib/chat/interactions', () => ({
  createInteraction,
  listInteractionPollVoters: vi.fn(),
  setInteractionPollVote: vi.fn()
}));

let component: ReturnType<typeof mount>;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.replaceChildren();
  vi.clearAllMocks();
});

it.each([
  1,
  Number.MAX_SAFE_INTEGER,
  Number.MAX_SAFE_INTEGER + 1,
  '9007199254740993',
  '1',
  true,
  1.5,
  0,
  null
])('only submits controls with a positive safe numeric version: %s', (version) => {
  response.data.view_version = version;
  component = mount(EphemeralInteractionTray, {
    target: document.body,
    props: { channelRef: '2@chat.example' }
  });
  flushSync();
  const button = [...document.querySelectorAll('button')].find(
    (item) => item.textContent?.trim() === 'Confirm'
  );
  if (version === 1 || version === Number.MAX_SAFE_INTEGER) {
    expect(button).toBeDefined();
    button!.click();
    expect(createInteraction).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ view_version: version }),
      expect.anything()
    );
  } else {
    expect(button).toBeUndefined();
    expect(createInteraction).not.toHaveBeenCalled();
  }
});
