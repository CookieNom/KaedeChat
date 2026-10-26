// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from 'vitest';
import { mount, unmount } from 'svelte';
import ChildSafetyQueue from './ChildSafetyQueue.svelte';
const network = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock('$lib/api/client', async (original) => ({
  ...(await original<typeof import('$lib/api/client')>()),
  api: network.api
}));
let component: ReturnType<typeof mount>;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.replaceChildren();
  vi.clearAllMocks();
});
const caseData = {
  id: '1',
  report_id: '2',
  status: 'pending',
  source: 'photodna',
  reason: '',
  created_at: '2026-10-26T00:00:00Z',
  message_ref: '3@home.example',
  attachment_refs: ['4@home.example'],
  subject: null,
  evidence: [
    {
      id: '5',
      filename: 'evidence.bin',
      content_type: 'application/octet-stream',
      size: 10,
      sha256: 'digest',
      preserved_at: null
    }
  ],
  submissions: [],
  holds: [],
  retain_until: null,
  closed_at: null,
  can_delete_evidence: false
};

it('separates evidence permission and requires a reason before confirming removal', async () => {
  network.api.mockImplementation(async (path: string) =>
    path.endsWith('/child-safety')
      ? { cases: [caseData] }
      : { ...caseData, status: 'confirmed', removal_pending: true, removal_completed_at: null }
  );
  component = mount(ChildSafetyQueue, {
    target: document.body,
    props: { capabilities: ['safety.read', 'safety.manage'] }
  });
  await vi.waitFor(() => expect(document.querySelector('.case-list button')).not.toBeNull());
  (document.querySelector('.case-list button') as HTMLButtonElement).click();
  await vi.waitFor(() => expect(document.body.textContent).toContain('Confirm and remove content'));
  expect(document.body.textContent).not.toContain('Download evidence');
  expect(document.querySelector('img, video, audio')).toBeNull();
  const confirm = [...document.querySelectorAll('button')].find(
    (button) => button.textContent === 'Confirm and remove content'
  )!;
  expect(confirm.disabled).toBe(true);
  const reason = document.querySelector('textarea')!;
  reason.value = 'Confirmed by authorized review';
  reason.dispatchEvent(new Event('input', { bubbles: true }));
  await vi.waitFor(() => expect(confirm.disabled).toBe(true));
  const verify = [...document.querySelectorAll('label')]
    .find((label) => label.textContent?.replace(/\s+/g, ' ').includes('I verified'))!
    .querySelector('input')!;
  verify.click();
  await vi.waitFor(() => expect(confirm.disabled).toBe(false));
  confirm.click();
  await vi.waitFor(() =>
    expect(network.api).toHaveBeenCalledWith('/administration/child-safety/1/review', {
      method: 'POST',
      body: JSON.stringify({
        decision: 'confirmed',
        reason: 'Confirmed by authorized review',
        source_verified: true
      })
    })
  );
  await vi.waitFor(() =>
    expect(document.body.textContent).toContain('This case has already been reviewed.')
  );
  expect(document.body.textContent).not.toContain('Confirm and remove content');
  expect(document.body.textContent).toContain('Removal pending');
  expect(document.body.textContent).not.toContain('Storage removal verified');
});

it('only shows storage verification once the backend records completion', async () => {
  network.api.mockResolvedValue({
    cases: [{ ...caseData, status: 'confirmed', removal_completed_at: '2026-10-26T01:00:00Z' }]
  });
  component = mount(ChildSafetyQueue, {
    target: document.body,
    props: { capabilities: ['safety.read'] }
  });
  await vi.waitFor(() => expect(document.querySelector('.case-list button')).not.toBeNull());
  (document.querySelector('.case-list button') as HTMLButtonElement).click();
  await vi.waitFor(() =>
    expect(document.body.textContent).toContain('Storage removal verified for this instance')
  );
  expect(document.body.textContent).not.toContain('Removal pending');
});
