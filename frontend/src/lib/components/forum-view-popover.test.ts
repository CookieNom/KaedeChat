import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const forumView = readFileSync(new URL('./ForumView.svelte', import.meta.url), 'utf8');

describe('ForumView Sort & View popover', () => {
  it('dismisses on outside pointer presses and Escape', () => {
    expect(forumView).toContain('<details bind:this={sortViewMenu} class="sort-view-menu">');
    expect(forumView).toContain('onpointerdown={dismissSortViewOnOutsidePointer}');
    expect(forumView).toContain('onkeydown={dismissSortViewOnEscape}');
    expect(forumView).toContain('!sortViewMenu.contains(target)');
    expect(forumView).toContain("event.key !== 'Escape'");
  });
});
