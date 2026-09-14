// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import TaskTrackerTaskDialog from './TaskTrackerTaskDialog.svelte';
import type { TrackerField, TrackerLane } from '$lib/task-tracker/types';

let component: ReturnType<typeof mount>;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.replaceChildren();
});
const lane: TrackerLane = {
  id: '2',
  origin_domain: 'example.test',
  channel_id: '1',
  channel_domain: 'example.test',
  name: 'Planned',
  kind: 'planned',
  color: 0,
  completed: false,
  position: 0,
  task_count: 0,
  version: '1'
};
const fields: TrackerField[] = [
  { id: 'size', name: 'Size', type: 'select', options: ['Small', 'Large'] },
  { id: 'estimate', name: 'Estimate', type: 'number', options: [] },
  { id: 'notes', name: 'Notes', type: 'textarea', options: [] },
  { id: 'labels', name: 'Labels', type: 'multiselect', options: ['Design', 'Engineering'] },
  { id: 'files', name: 'Files', type: 'attachments', options: [] }
];
it('saves typed custom values with a new task and keeps multiline text', () => {
  const onSave = vi.fn();
  component = mount(TaskTrackerTaskDialog, {
    target: document.body,
    props: { fields, initialLane: lane, lanes: [lane], members: [], onSave, onClose: vi.fn() }
  });
  flushSync();
  function enter(selector: string, value: string, event = 'input') {
    const input = document.querySelector<HTMLInputElement>(selector)!;
    input.value = value;
    input.dispatchEvent(new Event(event, { bubbles: true }));
    flushSync();
  }
  enter('input[maxlength="200"]', 'Ship it');
  enter('#field-size', 'Large', 'change');
  enter('#field-estimate', '2.5');
  enter('#field-notes', 'First line\nSecond line');
  (document.querySelector('fieldset input') as HTMLInputElement).click();
  flushSync();
  document
    .querySelector('form')!
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  expect(onSave).toHaveBeenCalledWith(
    expect.objectContaining({
      title: 'Ship it',
      custom_values: {
        size: 'Large',
        estimate: 2.5,
        notes: 'First line\nSecond line',
        labels: ['Design']
      }
    }),
    lane
  );
});

it('keeps custom values uneditable in read-only details', () => {
  component = mount(TaskTrackerTaskDialog, {
    target: document.body,
    props: {
      fields,
      initialLane: lane,
      lanes: [lane],
      members: [],
      readOnly: true,
      onSave: vi.fn(),
      onClose: vi.fn()
    }
  });
  flushSync();
  expect((document.querySelector('#field-size') as HTMLSelectElement).disabled).toBe(true);
  expect((document.querySelector('#field-notes') as HTMLTextAreaElement).disabled).toBe(true);
  expect(document.querySelector('input[type=file]')).toBeNull();
});
