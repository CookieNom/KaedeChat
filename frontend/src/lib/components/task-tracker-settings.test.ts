// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
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
  const onUpdateLane = vi.fn();
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
  save.click();
  expect(onUpdateLane).toHaveBeenCalledWith(state.get('lanes')![0], {
    name: 'Testing',
    color: 0xff00ff,
    completed: false
  });
});
