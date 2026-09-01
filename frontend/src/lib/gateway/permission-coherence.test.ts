import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const runtime = readFileSync(new URL('./runtime.svelte.ts', import.meta.url), 'utf8');

describe('gateway permission projection coherence', () => {
  it('updates both normalized and nested channel projections', () => {
    expect(runtime).toContain('chatEntities.updateChannelPermissions(');
  });

  it('fails closed and refreshes effective guild permissions after role/member changes', () => {
    expect(runtime).toContain('chatEntities.invalidateGuildPermissionProjection(guildRef)');
    expect(runtime).toContain('void api<Guild>(`/guilds/${encodeURIComponent(guildRef)}`)');
    expect(runtime.match(/refreshGuildPermissionProjection\(/gu)?.length).toBeGreaterThanOrEqual(4);
  });

  it('cancels stale permission refreshes and purges the guild on access loss', () => {
    expect(runtime).toContain('cancelGuildProjectionRefresh(target.id, target.origin_domain);');
    expect(runtime).toContain('chatEntities.removeGuild(target);');
  });
});
