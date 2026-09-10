// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import CommandOptionComposer from './CommandOptionComposer.svelte';
import type { Channel, Role } from '$lib/chat/types';

let component: ReturnType<typeof mount>;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.replaceChildren();
});

it('browses and searches channel and role options, preserving type restrictions and entity values', () => {
  const onValueChange = vi.fn();
  component = mount(CommandOptionComposer, {
    target: document.body,
    props: {
      commandName: 'configure',
      options: [
        { name: 'channel', type: 'channel', channel_types: [0] },
        { name: 'role', type: 'role' }
      ],
      values: {},
      channels: [
        { id: '1', origin_domain: 'chat.example', name: 'general', type: 0 },
        { id: '2', origin_domain: 'chat.example', name: 'support', type: 0 },
        { id: '3', origin_domain: 'chat.example', name: 'voice', type: 2 }
      ] as Channel[],
      roles: [
        { id: '4', origin_domain: 'chat.example', name: 'Admin' },
        { id: '5', origin_domain: 'chat.example', name: 'Moderator' }
      ] as Role[],
      onValueChange,
      onCancel: vi.fn()
    }
  });
  flushSync();
  const channel = document.querySelector<HTMLInputElement>('[aria-label="Search channels"]')!;
  channel.focus();
  flushSync();
  const labels = () =>
    [...document.querySelectorAll('[role="option"]')].map((el) => el.textContent);
  expect(labels()).toEqual(['#general', '#support']);
  channel.value = 'SUP';
  channel.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  expect(labels()).toEqual(['#support']);
  channel.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  flushSync();
  expect(onValueChange).toHaveBeenLastCalledWith('channel', '2@chat.example');
  expect(channel.getAttribute('aria-expanded')).toBe('false');
  channel.click();
  flushSync();
  expect(labels()).toEqual(['#general', '#support']);
  channel.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  flushSync();
  expect(channel.getAttribute('aria-expanded')).toBe('false');

  const role = document.querySelector<HTMLInputElement>('[aria-label="Search roles"]')!;
  role.focus();
  flushSync();
  expect(labels()).toEqual(['@Admin', '@Moderator']);
  role.value = 'missing';
  role.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  expect(labels()).toEqual([]);
  expect(document.body.textContent).toContain('No matching roles.');
  role.value = 'mod';
  role.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  expect(labels()).toEqual(['@Moderator']);
  document.querySelector<HTMLButtonElement>('[role="option"]')!.click();
  expect(onValueChange).toHaveBeenLastCalledWith('role', '5@chat.example');
});
