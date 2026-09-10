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
      onSubmit: vi.fn(),
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

it('focuses a command without arguments and sends on Enter, respecting composition and modifiers', () => {
  const onSubmit = vi.fn();
  component = mount(CommandOptionComposer, {
    target: document.body,
    props: {
      commandName: 'bridge-pair',
      options: [],
      values: {},
      onValueChange: vi.fn(),
      onSubmit,
      onCancel: vi.fn()
    }
  });
  flushSync();
  const fields = document.querySelector<HTMLElement>('[role="group"]')!;
  expect(document.activeElement).toBe(fields);
  for (const extra of [{ shiftKey: true }, { isComposing: true }]) {
    fields.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true, ...extra })
    );
  }
  expect(onSubmit).not.toHaveBeenCalled();
  fields.dispatchEvent(
    new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
  );
  expect(onSubmit).toHaveBeenCalledOnce();
});

it('tabs between arguments in both directions and selects picker results before sending', () => {
  const onSubmit = vi.fn();
  const onValueChange = vi.fn();
  component = mount(CommandOptionComposer, {
    target: document.body,
    props: {
      commandName: 'configure',
      options: [
        { name: 'channel', type: 'channel' },
        { name: 'reason', type: 'string' }
      ],
      channels: [{ id: '1', origin_domain: 'chat.example', name: 'general', type: 0 }] as Channel[],
      values: {},
      onValueChange,
      onSubmit,
      onCancel: vi.fn()
    }
  });
  flushSync();
  const [channel, reason] = document.querySelectorAll<HTMLInputElement>('input');
  expect(document.activeElement).toBe(channel);
  const key = (target: HTMLElement, key: string, shiftKey = false) => {
    target.dispatchEvent(
      new KeyboardEvent('keydown', { key, shiftKey, bubbles: true, cancelable: true })
    );
    flushSync();
  };
  key(channel, 'Tab');
  expect(document.activeElement).toBe(reason);
  key(reason, 'Tab', true);
  expect(document.activeElement).toBe(channel);
  key(channel, 'Enter');
  expect(onValueChange).toHaveBeenCalledWith('channel', '1@chat.example');
  expect(onSubmit).not.toHaveBeenCalled();
  key(channel, 'Tab');
  key(reason, 'Enter');
  expect(onSubmit).toHaveBeenCalledOnce();
});
