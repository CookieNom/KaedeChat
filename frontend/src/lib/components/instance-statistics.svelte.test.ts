// @vitest-environment happy-dom
import { expect, it } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import InstanceStatistics, { type InstanceStats } from './InstanceStatistics.svelte';

it('shows totals, storage categories, search and empty states', async () => {
  const peer: InstanceStats = {
    domain: 'peer.test',
    display_name: 'Garden',
    software_version: '1.0',
    last_seen_at: null,
    users: 2,
    messages: 12,
    guilds: 1,
    dm_conversations: 2,
    cached_files: 3,
    retained_events: 4,
    guild_storage_bytes: 1024,
    dm_storage_bytes: 1024,
    media_storage_bytes: 2048,
    event_storage_bytes: 0,
    pending_deliveries: 1,
    failed_deliveries: 0
  };
  const component = mount(InstanceStatistics, {
    target: document.body,
    props: {
      instances: [peer, { ...peer, domain: 'large.test', display_name: 'Larger', messages: 30 }]
    }
  });
  try {
    flushSync();
    await tick();
    expect(document.querySelector('.summary')?.textContent).toContain('42');
    expect(document.querySelector('.summary')?.textContent).toContain('8 KiB');
    expect(document.querySelector('.storage-grid')?.textContent).toContain('2 KiB');
    expect(document.querySelector('.metadata-grid')?.textContent).toContain('Not recorded');
    const search = document.querySelector('input')!;
    search.value = 'GARDEN';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(document.querySelectorAll('details')).toHaveLength(1);
    expect(document.querySelector('summary')?.textContent).toContain('peer.test');
    search.value = 'missing';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(document.body.textContent).toContain('No matching instances');
  } finally {
    await unmount(component);
  }
  const empty = mount(InstanceStatistics, { target: document.body, props: { instances: [] } });
  flushSync();
  expect(document.body.textContent).toContain('No federation peers yet');
  await unmount(empty);
});
