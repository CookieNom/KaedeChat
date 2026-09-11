// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';
import GuildMemberManagement from './GuildMemberManagement.svelte';
import type { GuildMemberSummary, Role } from '$lib/chat/types';

let component: ReturnType<typeof mount>;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.replaceChildren();
});
const role: Role = {
  id: '20',
  origin_domain: 'home.example',
  guild_id: '1',
  guild_domain: 'home.example',
  name: 'Moderator',
  color: 0xffaa00,
  permissions: '0',
  position: 1,
  hoist: false,
  mentionable: false,
  icon_hash: null
};
const members: GuildMemberSummary[] = Array.from({ length: 15 }, (_, i) => ({
  guild_id: '1',
  guild_domain: 'home.example',
  nickname: null,
  role_ids: i === 0 ? ['20'] : [],
  joined_at: new Date(Date.UTC(2026, 0, i + 1)).toISOString(),
  timeout_until: i === 1 ? '2099-01-01T00:00:00Z' : i === 2 ? '2000-01-01T00:00:00Z' : null,
  user: {
    id: String(i + 100),
    origin_domain: 'home.example',
    username: `user${i}`,
    display_name: `Member ${i}`,
    avatar_hash: null,
    handle: `user${i}@home.example`
  }
}));
function render(overrides: Record<string, unknown> = {}) {
  const toggleRole = vi.fn().mockResolvedValue(true);
  component = mount(GuildMemberManagement, {
    target: document.body,
    props: {
      members,
      roles: [role],
      hasMore: false,
      loading: false,
      busy: false,
      loadMore: vi.fn().mockResolvedValue(false),
      canManageMember: (member) => member.user.id !== '114',
      canManageRole: () => true,
      toggleRole,
      actions: createRawSnippet(() => ({ render: () => '<span>Moderation</span>' })),
      ...overrides
    }
  });
  flushSync();
  return { toggleRole };
}
function change(label: string, value: string) {
  const input = document.querySelector<HTMLInputElement | HTMLSelectElement>(
    `[aria-label="${label}"]`
  )!;
  flushSync(() => {
    input.value = value;
    if (input instanceof HTMLSelectElement) {
      // happy-dom does not match selected options with :checked, which Svelte uses.
      const querySelector = input.querySelector.bind(input);
      vi.spyOn(input, 'querySelector').mockImplementation((selector) =>
        selector === ':checked'
          ? (Array.from(input.options).find((option) => option.value === value) ?? null)
          : querySelector(selector)
      );
    }
    input.dispatchEvent(
      new Event(input.tagName === 'INPUT' ? 'input' : 'change', { bubbles: true })
    );
  });
}
const rows = () => Array.from(document.querySelectorAll('tbody tr'));
const button = (text: string) =>
  Array.from(document.querySelectorAll('button')).find((button) =>
    button.textContent?.includes(text)
  )!;

it('sorts by membership date, paginates, and searches by qualified user ID', () => {
  render();
  expect(rows()).toHaveLength(12);
  expect(rows()[0].textContent).toContain('Member 14');
  flushSync(() => button('Next').click());
  expect(rows()).toHaveLength(3);
  change('Search members', '100@home.example');
  expect(rows()).toHaveLength(1);
  expect(rows()[0].textContent).toContain('Member 0');
  change('Search members', '');
  change('Sort members', 'oldest');
  expect(rows()[0].textContent).toContain('Member 0');
  change('Members per page', '25');
  expect(rows()).toHaveLength(15);
});

it('filters assigned roles and active timeouts, excluding expired timeouts', () => {
  render();
  change('Filter by role', '20');
  expect(rows()).toHaveLength(1);
  expect(rows()[0].textContent).toContain('Moderator');
  change('Filter by role', '');
  change('Filter by signal', 'timeout');
  expect(rows()).toHaveLength(1);
  expect(rows()[0].textContent).toContain('Member 1');
});

it('selects only manageable members and stops bulk assignment on failure', async () => {
  const toggleRole = vi.fn().mockResolvedValueOnce(true).mockResolvedValueOnce(false);
  render({ toggleRole });
  expect(
    document.querySelector<HTMLInputElement>('[aria-label="Select Member 14"]')!.disabled
  ).toBe(true);
  flushSync(() =>
    document
      .querySelector<HTMLInputElement>('[aria-label="Select all manageable members on this page"]')!
      .click()
  );
  expect(document.body.textContent).toContain('11 selected');
  change('Role to assign to selected members', '20');
  flushSync(() => button('Add role').click());
  await vi.waitFor(() =>
    expect(document.body.textContent).toContain('Assigned Moderator to 1 of 11 members.')
  );
  expect(toggleRole).toHaveBeenCalledTimes(2);
  expect(toggleRole.mock.calls[0][0].user.id).toBe('103');
  change('Filter by role', '20');
  expect(document.body.textContent).not.toContain('11 selected');
});

it('loads successive cursor pages and leaves failed loads retryable without a request loop', async () => {
  const loadMore = vi.fn().mockResolvedValueOnce(true).mockResolvedValue(false);
  render({ hasMore: true, loadMore });
  await vi.waitFor(() => expect(loadMore).toHaveBeenCalledTimes(2));
  flushSync();
  expect(document.body.textContent).toContain('Directory incomplete');
  flushSync(() => button('Retry loading').click());
  await vi.waitFor(() => expect(loadMore).toHaveBeenCalledTimes(3));
});
