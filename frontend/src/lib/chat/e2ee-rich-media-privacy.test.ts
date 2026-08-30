import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const richEmbed = readFileSync(new URL('../components/RichEmbed.svelte', import.meta.url), 'utf8');
const v2Layout = readFileSync(
  new URL('../components/V2MessageLayout.svelte', import.meta.url),
  'utf8'
);
const messageRow = readFileSync(
  new URL('../components/MessageRow.svelte', import.meta.url),
  'utf8'
);
const forwarded = readFileSync(
  new URL('../components/ForwardedMessage.svelte', import.meta.url),
  'utf8'
);
const ephemeral = readFileSync(
  new URL('../components/EphemeralInteractionTray.svelte', import.meta.url),
  'utf8'
);

describe('encrypted rich media privacy', () => {
  it('gates every automatic embed preview behind the external-media policy', () => {
    expect(richEmbed.match(/<LinkPreview\b/gu)).toHaveLength(4);
    expect(richEmbed.match(/(?:#if|:else if) allowExternalMedia/gu)).toHaveLength(4);
    expect(richEmbed).toContain('Open external embed image');
  });

  it('never auto-loads authored V2 media while the policy is disabled', () => {
    expect(v2Layout).toContain('{:else if remote && allowExternalMedia}');
    expect(v2Layout).toContain('Open external component media');
  });

  it('passes a fail-closed policy through messages, forwards, and private responses', () => {
    expect(messageRow).toContain('allowExternalMedia={!presentedMessage.e2ee}');
    expect(messageRow).toContain('allowEncryptedManifests={Boolean(');
    expect(messageRow).toContain('reference.e2ee && reference.e2ee_verified !== true');
    expect(messageRow).toContain('(!message.e2ee || message.e2ee_verified === true)');
    expect(messageRow).toContain(
      'presentedMessage.e2ee && presentedMessage.e2ee_verified === true && presentedMessage.decrypted_attachments?.length'
    );
    expect(forwarded).toContain('{allowExternalMedia}');
    expect(forwarded).toContain('allowEncryptedManifests && attachment.encrypted_manifest');
    expect(ephemeral).toContain('allowExternalMedia={!request?.e2ee}');
  });
});
