import { describe, expect, it } from 'vitest';
import { developerModeFromSettings } from './developer-mode.svelte';

describe('developer mode preferences', () => {
  it('enables developer mode only for an explicit boolean true', () => {
    expect(developerModeFromSettings({ developer_mode: true })).toBe(true);
    expect(developerModeFromSettings({ developer_mode: false })).toBe(false);
    expect(developerModeFromSettings({ developer_mode: 'true' })).toBe(false);
    expect(developerModeFromSettings(undefined)).toBe(false);
  });
});
