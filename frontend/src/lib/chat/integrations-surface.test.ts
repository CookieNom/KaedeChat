import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const route = readFileSync(
  new URL('../../routes/(app)/g/[guildId]/integrations/+page.svelte', import.meta.url),
  'utf8'
);
const webhooks = readFileSync(
  new URL('../components/GuildWebhooks.svelte', import.meta.url),
  'utf8'
);

describe('guild Integrations placement', () => {
  it('keeps bots, webhooks, and channel following reachable from one server route', () => {
    expect(route).toContain('href="#bots-apps"');
    expect(route).toContain('href="#webhooks"');
    expect(route).toContain('href="#channels-followed"');
    expect(route).toContain('<GuildWebhooks');
    expect(route).toContain('<GuildAnnouncementFollows');
  });

  it('keeps target-owned bot channel ceilings in Server Settings Integrations', () => {
    expect(route).toContain('Channel access');
    expect(route).toContain('All role-permitted channels');
    expect(route).toContain("method: 'PATCH'");
    expect(route).toContain('channel_restrictions: installation.channel_restrictions');
    expect(route).toContain("channel.type === 4 ? 'Category' : 'Channel'");
  });

  it('offers full webhook CRUD and reload-safe copyable execution URLs', () => {
    expect(webhooks).toContain('void createWebhook()');
    expect(webhooks).toContain('void saveWebhook(webhook)');
    expect(webhooks).toContain('void rotateWebhook(webhook)');
    expect(webhooks).toContain('void removeWebhook(webhook)');
    expect(webhooks).toContain('Copy webhook URL');
    expect(webhooks).toContain('{revealedExecutionUrl}');
    expect(webhooks).toContain('copyExecutionUrl(webhook.execution_url)');
    expect(webhooks).toContain('remains available to server managers');
  });
});
