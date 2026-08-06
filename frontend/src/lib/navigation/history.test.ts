import { describe, expect, it } from 'vitest';
import { lastVisitedChannel, readNavigationHistory, recordNavigation } from './history';

function storage() {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value)
  };
}

describe('channel navigation persistence', () => {
  it('deduplicates recent routes and rejects unrelated paths', () => {
    const target = storage();
    recordNavigation(target, '/g/1/2');
    recordNavigation(target, '/settings');
    recordNavigation(target, '/home/3');
    recordNavigation(target, '/g/1/2');
    expect(readNavigationHistory(target)).toEqual(['/home/3', '/g/1/2']);
    expect(lastVisitedChannel(target)).toBe('/g/1/2');
  });
});
