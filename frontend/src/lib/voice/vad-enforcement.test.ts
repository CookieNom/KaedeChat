// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, tick } from 'svelte';
import { createClassComponent } from 'svelte/legacy';
import { Permission } from '$lib/generated/permissions';
import { chatEntities } from '$lib/stores/entities.svelte';
import type { Channel, Guild, UserSummary } from '$lib/chat/types';
import type { VoiceOccupant } from './occupancy';

const mocks = vi.hoisted(() => {
  class VoiceDouble extends EventTarget {
    connected = true;
    connecting = false;
    encrypted = false;
    microphone = false;
    deafened = false;
    camera = false;
    screen = false;
    canSpeak = true;
    canStream = true;
    pushToTalkRequired = true;
    participantList = [
      {
        key: '7@guild.example',
        identity: '7@guild.example',
        name: 'Speaker',
        local: true,
        speaking: false,
        microphone: true,
        camera: false,
        screen: false
      }
    ];
    participants = () => this.participantList;
    priority = new Set<string>();
    prioritySpeakers = () => this.priority;
    videoTiles: Array<{
      key: string;
      identity: string;
      name: string;
      local: boolean;
      source: string;
      track: { attach: () => HTMLVideoElement; detach: () => HTMLVideoElement[] };
    }> = [];
    tiles = () => this.videoTiles;
    attachAudio = () => () => undefined;
    reconcileBrowserPermissions = vi.fn(async () => undefined);
    reconcileParticipantPermissions = vi.fn(async () => undefined);
    startPushToTalk = vi.fn(async () => {
      this.microphone = true;
      this.dispatchEvent(new Event('change'));
    });
    stopPushToTalk = vi.fn(async () => {
      this.microphone = false;
      this.dispatchEvent(new Event('change'));
    });
    disconnect = vi.fn(async () => {
      this.connected = false;
      this.dispatchEvent(new Event('change'));
    });
    constructor() {
      super();
      instances.push(this);
    }
  }
  const instances: VoiceDouble[] = [];
  return { VoiceDouble, instances, request: vi.fn() };
});
vi.mock('./session', async (original) => ({
  ...(await original<typeof import('./session')>()),
  VoiceSession: mocks.VoiceDouble
}));
vi.mock('$lib/api/client', async (original) => ({
  ...(await original<typeof import('$lib/api/client')>()),
  api: mocks.request
}));
vi.mock('$lib/platform/native', async (original) => ({
  ...(await original<typeof import('$lib/platform/native')>()),
  isNativeDesktop: () => false
}));
vi.mock('$lib/gateway/runtime.svelte', () => ({
  authenticatedGateway: { client: { setSelfVoiceState: vi.fn() } }
}));
import VoiceDock from './VoiceDock.svelte';

const user = (id: string): UserSummary => ({
  id,
  origin_domain: 'guild.example',
  username: `user-${id}`,
  display_name: null,
  avatar_hash: null,
  handle: `user-${id}@guild.example`
});
const channel: Channel = {
  id: '2',
  origin_domain: 'guild.example',
  guild_id: '1',
  guild_domain: 'guild.example',
  type: 2,
  name: 'Voice',
  topic: null,
  position: 0,
  parent_id: null,
  parent_domain: null,
  rate_limit_per_user: 0,
  last_message_id: null,
  last_message_domain: null
};
const basicPermissions = Permission.CONNECT | Permission.SPEAK | Permission.STREAM;
let component: ReturnType<typeof createClassComponent> | undefined;
const current = () => mocks.instances.at(-1)!;
async function render(permissions = basicPermissions, occupants: VoiceOccupant[] = []) {
  component = createClassComponent({
    component: VoiceDock,
    target: document.body,
    props: { channelRef: '2@guild.example', permissions: permissions.toString(), occupants }
  });
  flushSync();
  await tick();
}
const hold = () => document.querySelector<HTMLButtonElement>('button[aria-label="Hold to talk"]')!;
beforeEach(() => {
  mocks.instances.length = 0;
  mocks.request.mockReset().mockResolvedValue({
    id: '5',
    topic: 'Stage',
    channel_id: '2',
    channel_domain: 'guild.example'
  });
  chatEntities.ingestCurrentUser(user('7'));
  chatEntities.channels.upsert(channel);
});
afterEach(() => {
  component?.$destroy();
  component = undefined;
  document.body.replaceChildren();
  chatEntities.clearSession();
  vi.restoreAllMocks();
});

describe('browser voice-activity permission UI', () => {
  it('renders an enforced pointer-and-keyboard hold-to-talk control for no-VAD grants', async () => {
    await render();
    const button = hold();
    expect(button.disabled).toBe(false);
    button.setPointerCapture = vi.fn();
    for (const release of ['pointerup', 'pointercancel']) {
      button.dispatchEvent(new PointerEvent('pointerdown', { pointerId: 1, bubbles: true }));
      await tick();
      expect(button.getAttribute('aria-pressed')).toBe('true');
      button.dispatchEvent(new PointerEvent(release, { pointerId: 1, bubbles: true }));
      await tick();
      expect(button.getAttribute('aria-pressed')).toBe('false');
    }
    for (const key of [' ', 'Enter']) {
      button.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
      await tick();
      expect(button.getAttribute('aria-pressed')).toBe('true');
      button.dispatchEvent(new KeyboardEvent('keyup', { key, bubbles: true }));
      await tick();
      expect(button.getAttribute('aria-pressed')).toBe('false');
    }
    expect(current().startPushToTalk).toHaveBeenCalledTimes(4);
    expect(current().stopPushToTalk).toHaveBeenCalledTimes(4);
  });

  it('closes push-to-talk capture when the tab loses focus or becomes hidden', async () => {
    await render();
    hold().dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }));
    await tick();
    expect(current().microphone).toBe(true);
    window.dispatchEvent(new Event('blur'));
    await tick();
    expect(current().microphone).toBe(false);
    hold().dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }));
    await tick();
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    document.dispatchEvent(new Event('visibilitychange'));
    await tick();
    expect(current().microphone).toBe(false);
    expect(current().stopPushToTalk).toHaveBeenCalledTimes(2);
  });

  it('reconciles live browser grants when effective channel permissions change', async () => {
    await render();
    expect(current().reconcileBrowserPermissions).toHaveBeenLastCalledWith({
      reference: '2@guild.example',
      canConnect: true,
      canSpeak: true,
      canStream: true,
      canUseVad: false
    });
    component!.$set({ permissions: (Permission.CONNECT | Permission.STREAM).toString() });
    await tick();
    expect(current().reconcileBrowserPermissions).toHaveBeenLastCalledWith({
      reference: '2@guild.example',
      canConnect: true,
      canSpeak: false,
      canStream: true,
      canUseVad: false
    });
    component!.$set({ permissions: (basicPermissions | Permission.USE_VAD).toString() });
    await tick();
    expect(current().disconnect).toHaveBeenCalledOnce();
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]')?.textContent).toMatch(/rejoin/i)
    );
  });

  it('uses the authoritative Stage speaker grant for live promotion and VAD', async () => {
    chatEntities.channels.upsert({ ...channel, type: 13 });
    const occupant = {
      identity: '7@guild.example',
      user_id: '7',
      user_domain: 'guild.example',
      suppressed: true,
      can_speak: false,
      can_stream: false
    } as VoiceOccupant;
    await render(Permission.CONNECT, [occupant]);
    expect(current().reconcileParticipantPermissions).toHaveBeenLastCalledWith({
      canSpeak: false,
      canStream: false,
      canUseVad: false
    });
    component!.$set({
      occupants: [{ ...occupant, suppressed: false, can_speak: true, can_stream: true }]
    });
    await tick();
    expect(current().reconcileParticipantPermissions).toHaveBeenLastCalledWith({
      canSpeak: true,
      canStream: true,
      canUseVad: true
    });
    expect(current().reconcileBrowserPermissions).not.toHaveBeenCalled();
  });

  it('gates Stage moderation by the target member hierarchy', async () => {
    chatEntities.channels.upsert({ ...channel, type: 13 });
    const role = (id: string, position: number) => ({
      id,
      origin_domain: 'guild.example',
      guild_id: '1',
      guild_domain: 'guild.example',
      name: id,
      permissions: '0',
      position,
      color: 0,
      hoist: false,
      mentionable: false
    });
    chatEntities.ingestGuilds([
      {
        id: '1',
        origin_domain: 'guild.example',
        name: 'Guild',
        description: null,
        icon_hash: null,
        owner_id: '9',
        owner_domain: 'guild.example',
        permissions: Permission.ADMINISTRATOR.toString(),
        permission_generation: '1',
        unavailable: false,
        roles: [role('10', 1), role('11', 2)],
        channels: [{ ...channel, type: 13 }]
      } satisfies Guild
    ]);
    chatEntities.ingestMembers(
      ['7', '8', '9'].map((id) => ({
        guild_id: '1',
        guild_domain: 'guild.example',
        user: user(id),
        nickname: null,
        role_ids: [id === '7' ? '11' : '10']
      }))
    );
    const occupants = ['8', '9'].map(
      (id) =>
        ({
          identity: `${id}@guild.example`,
          user_id: id,
          user_domain: 'guild.example',
          suppressed: true,
          can_speak: false
        }) as VoiceOccupant
    );
    await render(Permission.ADMINISTRATOR, occupants);
    const cards = Array.from(document.querySelectorAll('article'));
    expect(
      cards.find((card) => card.textContent?.includes('user-8'))?.querySelector('button')
    ).not.toBeNull();
    expect(
      cards.find((card) => card.textContent?.includes('user-9'))?.querySelector('button')
    ).toBeNull();
    chatEntities.members.remove('1@guild.example:7@guild.example');
    await tick();
    expect(
      Array.from(document.querySelectorAll('article')).every(
        (card) => card.querySelector('button') === null
      )
    ).toBe(true);
  });
});

it('keeps the priority cue visible in participant and video layouts', async () => {
  await render();
  expect(document.querySelector('[aria-label="Priority speaker"]')).toBeNull();
  current().priority.add('7@guild.example');
  current().dispatchEvent(new Event('change'));
  await tick();
  expect(
    document.querySelector('[aria-label="Priority speaker"]')?.closest('article')?.textContent
  ).toContain('Speaker');
  current().videoTiles = [
    {
      key: 'video',
      identity: '7@guild.example',
      name: 'Speaker',
      local: true,
      source: 'camera',
      track: { attach: () => document.createElement('video'), detach: () => [] }
    }
  ];
  current().dispatchEvent(new Event('change'));
  await tick();
  expect(document.querySelector('video')).not.toBeNull();
  expect(
    document.querySelector('[aria-label="Priority speaker"]')?.closest('article')?.textContent
  ).toContain('Speaker');
  expect(document.querySelector('[role="status"]')?.textContent?.replace(/\s+/g, ' ').trim()).toBe(
    'Speaker is speaking with priority'
  );
  current().priority.clear();
  current().dispatchEvent(new Event('change'));
  await tick();
  expect(document.querySelector('[aria-label="Priority speaker"]')).toBeNull();
});
