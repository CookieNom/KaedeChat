// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import TaskTrackerSettingsDialog from './TaskTrackerSettingsDialog.svelte';
import type { CreateTrackerLaneRequest, TrackerBoard, TrackerLane } from '$lib/task-tracker/types';

let component: ReturnType<typeof mount>;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.replaceChildren();
});

it('preserves the selected color when a newly created status appears and is saved', () => {
  const state = new SvelteMap<string, TrackerLane[]>([['lanes', []]]);
  const board: TrackerBoard = {
    channel_id: '1',
    channel_domain: 'example.test',
    key_prefix: 'TASK',
    next_task_number: '1',
    version: '1',
    permissions: '0',
    lanes: [],
    tasks: []
  };
  const onUpdateLane = vi.fn((lane: TrackerLane, patch) => {
    state.set('lanes', [{ ...lane, ...patch }]);
  });
  const onCreateLane = vi.fn((request: CreateTrackerLaneRequest) => {
    state.set('lanes', [
      {
        ...request,
        completed: request.completed ?? false,
        id: '2',
        origin_domain: 'example.test',
        channel_id: '1',
        channel_domain: 'example.test',
        kind: 'custom',
        position: 0,
        task_count: 0,
        version: '1'
      }
    ]);
  });
  component = mount(TaskTrackerSettingsDialog, {
    target: document.body,
    props: {
      board,
      get lanes() {
        return state.get('lanes')!;
      },
      onCreateLane,
      onUpdateLane,
      onPrefix: vi.fn(),
      onMoveLane: vi.fn(),
      onDeleteLane: vi.fn(),
      onClose: vi.fn()
    }
  });
  flushSync();
  const form = document.querySelector<HTMLFormElement>('.new-lane-form')!;
  const hex = form.querySelector<HTMLInputElement>('input[type=text]')!;
  hex.value = '#ff00ff';
  hex.dispatchEvent(new Event('input', { bubbles: true }));
  hex.dispatchEvent(new Event('blur'));
  const name = form.querySelector<HTMLInputElement>('input[maxlength="100"]')!;
  name.value = 'Testing';
  name.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  flushSync();
  expect(onCreateLane).toHaveBeenCalledWith({ name: 'Testing', color: 0xff00ff, completed: false });
  const row = document.querySelector('article')!;
  expect(row.querySelector<HTMLInputElement>('input[type=text]')!.value).toBe('#ff00ff');
  const save = [...row.querySelectorAll('button')].find(
    (button) => button.textContent?.trim() === 'Save'
  )!;
  expect(save.disabled).toBe(true);
  save.click();
  expect(onUpdateLane).not.toHaveBeenCalled();
  const laneName = row.querySelector<HTMLInputElement>('input[maxlength="100"]')!;
  laneName.value = 'Updated';
  laneName.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  expect(save.disabled).toBe(false);
  const previous = state.get('lanes')![0];
  save.click();
  flushSync();
  expect(save.disabled).toBe(true);
  expect(onUpdateLane).toHaveBeenCalledWith(previous, {
    name: 'Updated',
    color: 0xff00ff,
    completed: false
  });
});

it('adds a dropdown, preserves its choices, and requires confirmation before removing values', async () => {
  const state = new SvelteMap<string, TrackerBoard>([
    [
      'board',
      {
        channel_id: '1',
        channel_domain: 'example.test',
        key_prefix: 'TASK',
        next_task_number: '1',
        version: '1',
        permissions: '0',
        lanes: [],
        tasks: [],
        custom_fields: []
      }
    ]
  ]);
  const onFields = vi.fn((fields) => {
    state.set('board', { ...state.get('board')!, custom_fields: fields });
  });
  component = mount(TaskTrackerSettingsDialog, {
    target: document.body,
    props: {
      get board() {
        return state.get('board')!;
      },
      lanes: [],
      onFields,
      onCreateLane: vi.fn(),
      onUpdateLane: vi.fn(),
      onPrefix: vi.fn(),
      onMoveLane: vi.fn(),
      onDeleteLane: vi.fn(),
      onClose: vi.fn()
    }
  });
  flushSync();
  const click = (text: string) => {
    [...document.querySelectorAll('button')].find((b) => b.textContent?.trim() === text)!.click();
    flushSync();
  };
  click('Add custom field');
  await tick();
  const section = document.querySelector('section[aria-labelledby="custom-fields-heading"]')!;
  const name = section.querySelector('input')!;
  name.value = 'Complexity';
  name.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  const type = section.querySelector('select')!;
  type.value = 'select';
  type.dispatchEvent(new Event('change', { bubbles: true }));
  await tick();
  flushSync();
  const choices = section.querySelector('textarea')!;
  choices.value = 'Small\nLarge';
  choices.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  section
    .querySelector('form')!
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  await Promise.resolve();
  flushSync();
  expect(onFields).toHaveBeenCalledWith([
    expect.objectContaining({ name: 'Complexity', type: 'select', options: ['Small', 'Large'] })
  ]);
  click('Remove');
  expect(onFields).toHaveBeenCalledTimes(1);
  expect(section.textContent).toContain('and its values from every task');
  click('Keep field');
  expect(onFields).toHaveBeenCalledTimes(1);
  click('Remove');
  click('Remove field and values');
  expect(onFields).toHaveBeenLastCalledWith([]);
});
