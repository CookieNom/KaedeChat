// @vitest-environment happy-dom
import { afterEach, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import ColorPicker from './ColorPicker.svelte';
import { hexToHsv, hsvToHex } from '$lib/ui/color';

let component: ReturnType<typeof mount>;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.replaceChildren();
});

it('round trips primaries, grays and arbitrary colors through HSV', () => {
  for (const hex of [
    '#000000',
    '#ffffff',
    '#808080',
    '#ff0000',
    '#00ff00',
    '#0000ff',
    '#abcdef',
    '#e67e22'
  ]) {
    expect(hsvToHex(...hexToHsv(hex))).toBe(hex);
  }
});

it('updates hue immediately, preserves it through black, and validates hex input', () => {
  component = mount(ColorPicker, {
    target: document.body,
    props: { value: '#ff0000', label: 'Color' }
  });
  flushSync();
  const hue = document.querySelector<HTMLInputElement>('input[type=range]')!;
  const hex = document.querySelector<HTMLInputElement>('input[type=text]')!;
  const area = document.querySelector<HTMLElement>('[role=slider]')!;
  const key = (element: HTMLElement, key: string, shiftKey = false) => {
    element.dispatchEvent(new KeyboardEvent('keydown', { key, shiftKey, bubbles: true }));
    flushSync();
  };
  hue.value = '120';
  hue.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  expect(hex.value).toBe('#00ff00');
  for (let i = 0; i < 11; i++) key(area, 'ArrowDown', true);
  expect(hex.value).toBe('#000000');
  key(area, 'ArrowUp', true);
  expect(hex.value).toBe('#001a00');
  const enterHex = (value: string) => {
    hex.value = value;
    hex.dispatchEvent(new Event('input', { bubbles: true }));
    key(hex, 'Enter');
  };
  enterHex('ABC');
  expect(hex.value).toBe('#aabbcc');
  enterHex('#nope');
  expect(hex.value).toBe('#aabbcc');
  enterHex('#000000');
  key(area, 'ArrowLeft');
  key(area, 'ArrowDown');
  expect(hex.value).toBe('#000000');
  expect(document.querySelector('input[type=color]')).toBeNull();
});
