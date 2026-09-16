// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest';
import { draftKey, readDraft, writeDraft } from './local-drafts';

describe('device-local drafts', () => {
  it('isolates accounts and conversations, preserves whitespace, and clears sent text', () => {
    const key = draftKey('alice@one.test', '123@two.test');
    writeDraft(key, 'Unfinished\n  ');
    expect(readDraft(key)).toBe('Unfinished\n  ');
    expect(readDraft(draftKey('bob@one.test', '123@two.test'))).toBe('');
    expect(readDraft(draftKey('alice@one.test', '124@two.test'))).toBe('');
    writeDraft(key, '');
    expect(readDraft(key)).toBe('');
  });
});

it('restores device storage after a reload and cannot resurrect cleared text', async () => {
  const key = draftKey('reload@one.test', '125@two.test');
  writeDraft(key, 'Saved only here');
  expect(localStorage.getItem(key)).toBe('Saved only here');
  vi.resetModules();
  const reloaded = await import('./local-drafts');
  expect(reloaded.readDraft(key)).toBe('Saved only here');
  reloaded.writeDraft(key, '');
  vi.resetModules();
  expect((await import('./local-drafts')).readDraft(key)).toBe('');
});
