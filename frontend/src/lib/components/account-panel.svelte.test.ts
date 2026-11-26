// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { ApiError } from '$lib/api/errors';
import { presenceControl } from '$lib/stores/presence.svelte';
import AccountPanel from './AccountPanel.svelte';
import PresenceSchedulesPanel from './PresenceSchedulesPanel.svelte';

const { api, rememberPresence } = vi.hoisted(() => ({ api: vi.fn(), rememberPresence: vi.fn() }));
vi.mock('$lib/api/client', () => ({
  api,
  userErrorMessage: (_: unknown, fallback: string) => fallback
}));
vi.mock('$lib/gateway/runtime.svelte', () => ({
  authenticatedGateway: { client: { rememberPresence } }
}));
vi.mock('$lib/platform/native', () => ({ isNativeDesktop: () => true }));
const user = {
  id: '42',
  origin_domain: 'chat.example',
  username: 'cookie',
  display_name: 'Cookie',
  avatar_hash: null,
  handle: '@cookie@chat.example',
  custom_status: 'Reading'
};
afterEach(() => {
  vi.clearAllMocks();
  presenceControl.reset();
  document.body.innerHTML = '';
});

it('edits and clears status, persists presence, and exposes profile/account actions', async () => {
  api.mockImplementation(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/settings')) return { presence_preference: 'idle' };
    return init ? { ...user, ...JSON.parse(String(init.body)) } : user;
  });
  const component = mount(AccountPanel, { target: document.body, props: { user } });
  try {
    flushSync();
    const panel = document.querySelector('[popover]')!;
    panel.dispatchEvent(Object.assign(new Event('toggle'), { newState: 'open' }));
    await vi.waitFor(() =>
      expect(document.querySelector('.presence-picker-trigger')?.textContent).toContain('Idle')
    );
    expect(panel.querySelector('a[href="/accounts"]')).not.toBeNull();
    expect(
      panel.querySelector('a[aria-label="Change your profile picture"]')?.getAttribute('href')
    ).toBe('/settings#profile');
    flushSync(() => document.querySelector<HTMLButtonElement>('.status-bubble')!.click());
    const input = document.querySelector<HTMLInputElement>('input')!;
    flushSync(() => {
      input.value = 'Baking';
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    document
      .querySelector('form')!
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await vi.waitFor(() =>
      expect(document.querySelector('.status-bubble')?.textContent).toBe('Baking')
    );
    expect(api).toHaveBeenCalledWith('/users/@me', {
      method: 'PATCH',
      body: JSON.stringify({ custom_status: 'Baking' })
    });
    flushSync(() => document.querySelector<HTMLButtonElement>('.status-bubble')!.click());
    const clear = Array.from(document.querySelectorAll('button')).find(
      (button) => button.textContent === 'Clear status'
    )!;
    clear.click();
    await vi.waitFor(() =>
      expect(document.querySelector('.status-bubble')?.textContent).toBe('Set a custom status')
    );
    flushSync(() => document.querySelector<HTMLButtonElement>('.presence-picker-trigger')!.click());
    const invisible = Array.from(
      document.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]')
    ).find((button) => button.textContent?.includes('Invisible'))!;
    invisible.click();
    await vi.waitFor(() => expect(rememberPresence).toHaveBeenCalledWith('invisible'));
    expect(api).toHaveBeenCalledWith('/users/@me/settings', {
      method: 'PATCH',
      body: JSON.stringify({ presence_preference: 'invisible' })
    });
  } finally {
    await unmount(component);
  }
});

it('keeps the status editor and shows an error when saving fails', async () => {
  api.mockImplementation(async (path: string, init?: RequestInit) => {
    if (init) throw new Error('offline');
    return path.endsWith('/settings') ? { presence_preference: 'online' } : user;
  });
  const component = mount(AccountPanel, { target: document.body, props: { user } });
  try {
    flushSync();
    document
      .querySelector('[popover]')!
      .dispatchEvent(Object.assign(new Event('toggle'), { newState: 'open' }));
    await vi.waitFor(() => {
      flushSync();
      expect(api).toHaveBeenCalledTimes(2);
      expect(document.querySelector<HTMLButtonElement>('.status-bubble')?.disabled).toBe(false);
    });
    flushSync(() => document.querySelector<HTMLButtonElement>('.status-bubble')!.click());
    const save = document.querySelector<HTMLButtonElement>('.status-actions .primary-button')!;
    expect(save.disabled).toBe(true);
    const input = document.querySelector<HTMLInputElement>('input')!;
    flushSync(() => {
      input.value = 'Baking';
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(save.disabled).toBe(false);
    document.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }));
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]')?.textContent).toBe(
        'Could not save your status.'
      )
    );
    expect(document.querySelector('input')).not.toBeNull();
    expect(save.disabled).toBe(false);
  } finally {
    await unmount(component);
  }
});

it('sets a timed presence duration via the Discord-style flyout and opens schedules modal', async () => {
  api.mockImplementation(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/settings')) {
      if (init?.method === 'PATCH') {
        const body = JSON.parse(String(init.body));
        return {
          presence_preference: body.presence_preference ?? 'online',
          presence_state: {
            effective_status: body.presence_preference ?? 'online',
            suppress_notifications: body.presence_preference === 'dnd',
            source: body.presence_duration_minutes ? 'timed' : 'manual',
            base_status: 'online',
            timed_status: body.presence_duration_minutes ? body.presence_preference : null,
            timed_expires_at: body.presence_duration_minutes
              ? new Date(Date.now() + body.presence_duration_minutes * 60_000).toISOString()
              : null,
            active_schedule_id: null,
            active_schedule_name: null,
            active_schedule_status: null,
            active_schedule_ends_at: null,
            skipped_schedule_name: null,
            skipped_schedule_until: null,
            timezone: body.presence_timezone ?? 'UTC',
            schedules: body.presence_schedules ?? []
          }
        };
      }
      return {
        presence_preference: 'online',
        presence_state: {
          effective_status: 'online',
          suppress_notifications: false,
          source: 'manual',
          base_status: 'online',
          timed_status: null,
          timed_expires_at: null,
          active_schedule_id: null,
          active_schedule_name: null,
          active_schedule_status: null,
          active_schedule_ends_at: null,
          skipped_schedule_name: null,
          skipped_schedule_until: null,
          timezone: 'UTC',
          schedules: []
        }
      };
    }
    return user;
  });

  const component = mount(AccountPanel, { target: document.body, props: { user } });
  try {
    flushSync();
    const panel = document.querySelector('[popover]')!;
    panel.dispatchEvent(Object.assign(new Event('toggle'), { newState: 'open' }));
    await vi.waitFor(() => {
      flushSync();
      expect(api).toHaveBeenCalledTimes(2);
      expect(document.querySelector<HTMLButtonElement>('.presence-picker-trigger')?.disabled).toBe(
        false
      );
    });

    // Open status picker and expand Do Not Disturb duration submenu
    flushSync(() => document.querySelector<HTMLButtonElement>('.presence-picker-trigger')!.click());
    const dndChevron = document.querySelector<HTMLButtonElement>(
      'button[aria-label="Set Do not disturb duration"]'
    )!;
    flushSync(() => dndChevron.click());
    const oneHour = Array.from(
      document.querySelectorAll<HTMLButtonElement>('.duration-option')
    ).find((button) => button.textContent?.includes('For 1 Hour'))!;
    oneHour.click();

    await vi.waitFor(() => expect(rememberPresence).toHaveBeenCalledWith('dnd'));
    expect(api).toHaveBeenCalledWith('/users/@me/settings', {
      method: 'PATCH',
      body: JSON.stringify({ presence_preference: 'dnd', presence_duration_minutes: 60 })
    });

    // Open Schedules modal from status picker
    flushSync(() => document.querySelector<HTMLButtonElement>('.presence-picker-trigger')!.click());
    const schedulesBtn = document.querySelector<HTMLButtonElement>('.schedules-entry')!;
    flushSync(() => schedulesBtn.click());
    const dialog = document.querySelector<HTMLDialogElement>(
      'dialog[aria-label="Presence schedules"]'
    )!;
    expect(dialog.open).toBe(true);

    // Add a Sleep schedule via preset
    const sleepPreset = Array.from(
      document.querySelectorAll<HTMLButtonElement>('.preset-chip')
    ).find((button) => button.textContent?.trim() === 'Sleep')!;
    flushSync(() => sleepPreset.click());
    document
      .querySelector('.schedule-editor')!
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

    await vi.waitFor(() => {
      const [path, init] = api.mock.lastCall ?? [];
      expect(path).toBe('/users/@me/settings');
      expect(init?.method).toBe('PATCH');
      const saved = JSON.parse(String(init.body));
      expect(saved.presence_schedules).toEqual([expect.objectContaining({ name: 'Sleep' })]);
      expect(saved.expected_presence_schedules).toEqual([]);
      expect(saved).not.toHaveProperty('presence_timezone');
    });
  } finally {
    await unmount(component);
  }
});

it('uses the list from when editing began and reloads schedules after a concurrent change', async () => {
  presenceControl.applyFallbackPreference('online');
  const original = {
    id: 'sleep',
    name: 'Sleep',
    status: 'dnd' as const,
    days: [0],
    start_minute: 1380,
    end_minute: 420,
    enabled: true
  };
  const state = { ...presenceControl.state!, schedules: [original] };
  const latest = { ...state, schedules: [{ ...original, name: 'Updated elsewhere' }] };
  presenceControl.apply(state);
  api
    .mockRejectedValueOnce(new ApiError('PRESENCE_SCHEDULES_CHANGED', 'Conflict', 409))
    .mockResolvedValueOnce({ presence_state: latest });
  const component = mount(PresenceSchedulesPanel, { target: document.body });
  try {
    flushSync();
    const editButton = () =>
      Array.from(document.querySelectorAll('button')).find(
        (button) => button.textContent.trim() === 'Edit'
      )!;
    flushSync(() => editButton().click());
    flushSync(() =>
      presenceControl.apply({ ...state, schedules: [{ ...original, enabled: false }] })
    );
    document.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }));
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]')?.textContent).toContain('another device')
    );
    const [path, init] = api.mock.calls[0];
    expect(path).toBe('/users/@me/settings');
    expect(JSON.parse(init.body).expected_presence_schedules).toEqual([original]);
    expect(api.mock.calls[1]).toEqual(['/users/@me/settings']);
    expect(document.querySelector('.schedule-card strong')?.textContent).toBe('Updated elsewhere');

    api.mockResolvedValueOnce({ presence_preference: 'online', presence_state: latest });
    flushSync(() => editButton().click());
    document.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }));
    await vi.waitFor(() =>
      expect(document.querySelector('.feedback')?.textContent).toBe('Schedule updated.')
    );
    expect(JSON.parse(api.mock.calls[2][1].body).expected_presence_schedules).toEqual(
      latest.schedules
    );
  } finally {
    await unmount(component);
  }
});

it('tracks midnight and the preview day in the saved schedule timezone', async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-11-26T14:59:30Z'));
  presenceControl.applyFallbackPreference('idle');
  presenceControl.apply({
    ...presenceControl.state!,
    source: 'timed',
    timed_status: 'idle',
    timed_expires_at: '2026-11-26T15:01:00Z',
    timezone: 'Asia/Tokyo'
  });
  const component = mount(PresenceSchedulesPanel, { target: document.body });
  try {
    flushSync();
    const selected = () =>
      document
        .querySelector('[aria-label="Preview day of week"] [aria-pressed="true"]')
        ?.getAttribute('title');
    expect(selected()).toBe('Thu');
    const marker = () => document.querySelector<HTMLElement>('.timeline-now')!.style.left;
    const countdown = () => document.querySelector('.live-banner strong')!.textContent;
    expect(countdown()).toContain('2m left');
    expect(parseFloat(marker())).toBeGreaterThan(99);
    await vi.advanceTimersByTimeAsync(30_000);
    flushSync();
    expect(selected()).toBe('Fri');
    expect(countdown()).toContain('1m left');
    expect(parseFloat(marker())).toBe(0);
  } finally {
    await unmount(component);
    vi.useRealTimers();
  }
});
