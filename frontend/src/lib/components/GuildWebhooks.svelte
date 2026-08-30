<script lang="ts">
  import { userErrorMessage } from '$lib/api/client';
  import { entityRef } from '$lib/chat/refs';
  import type { Guild } from '$lib/chat/types';
  import {
    commitGuildWebhookAvatar,
    createGuildWebhook,
    createGuildWebhookAvatarTicket,
    deleteGuildWebhook,
    deleteGuildWebhookAvatar,
    isChannelFollowerWebhook,
    listGuildWebhooks,
    manageableWebhookChannels,
    rotateGuildWebhook,
    updateGuildWebhook,
    type WebhookSummary
  } from '$lib/chat/webhooks';
  import { assetUrl } from '$lib/media/assets';
  import { uploadObject } from '$lib/media/uploads';
  import Icon from './Icon.svelte';

  let { guild, canManage }: { guild: Guild; canManage: boolean } = $props();

  let webhooks = $state<WebhookSummary[]>([]);
  let nameDrafts = $state<Record<string, string>>({});
  let channelDrafts = $state<Record<string, string>>({});
  let newName = $state('');
  let newChannelRef = $state('');
  let revealedExecutionUrl = $state('');
  let loading = $state(false);
  let busyRef = $state('');
  let error = $state('');
  let notice = $state('');
  let requestGeneration = 0;

  const guildRef = $derived(entityRef(guild));
  const channels = $derived(manageableWebhookChannels(guild));
  const ordinaryWebhooks = $derived(webhooks.filter((item) => !isChannelFollowerWebhook(item)));
  const followedChannels = $derived(webhooks.filter(isChannelFollowerWebhook));

  function installDrafts(items: WebhookSummary[]) {
    nameDrafts = Object.fromEntries(items.map((item) => [item.id, item.name]));
    channelDrafts = Object.fromEntries(
      items.map((item) => [item.id, `${item.channel_id}@${item.channel_domain}`])
    );
  }

  async function load(reference: string, generation: number, signal: AbortSignal) {
    loading = true;
    error = '';
    try {
      const items = await listGuildWebhooks(reference, signal);
      if (signal.aborted || generation !== requestGeneration) return;
      webhooks = items.filter((item) => !item.revoked);
      installDrafts(webhooks);
    } catch (caught) {
      if (signal.aborted || generation !== requestGeneration) return;
      error = userErrorMessage(caught, 'Could not load webhooks for this guild.');
    } finally {
      if (generation === requestGeneration) loading = false;
    }
  }

  $effect(() => {
    const reference = guildRef;
    const allowed = canManage;
    const generation = ++requestGeneration;
    webhooks = [];
    nameDrafts = {};
    channelDrafts = {};
    error = '';
    notice = '';
    revealedExecutionUrl = '';
    if (!allowed) {
      loading = false;
      return;
    }
    const controller = new AbortController();
    void load(reference, generation, controller.signal);
    return () => controller.abort();
  });

  $effect(() => {
    const references = channels.map((channel) => entityRef(channel));
    if (!references.includes(newChannelRef)) newChannelRef = references[0] ?? '';
  });

  function replaceWebhook(updated: WebhookSummary) {
    webhooks = webhooks.map((item) => (item.id === updated.id ? updated : item));
    nameDrafts = { ...nameDrafts, [updated.id]: updated.name };
    channelDrafts = {
      ...channelDrafts,
      [updated.id]: `${updated.channel_id}@${updated.channel_domain}`
    };
  }

  async function createWebhook() {
    const name = newName.trim();
    if (!canManage || busyRef || !name || !newChannelRef) return;
    busyRef = 'create';
    error = '';
    notice = '';
    revealedExecutionUrl = '';
    try {
      const created = await createGuildWebhook(guildRef, newChannelRef, name);
      webhooks = [...webhooks, created];
      nameDrafts = { ...nameDrafts, [created.id]: created.name };
      channelDrafts = {
        ...channelDrafts,
        [created.id]: `${created.channel_id}@${created.channel_domain}`
      };
      newName = '';
      revealedExecutionUrl = created.execution_url ?? '';
      notice = 'Webhook created. Its URL remains available to server managers.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not create the webhook.');
    } finally {
      busyRef = '';
    }
  }

  async function saveWebhook(webhook: WebhookSummary) {
    const name = (nameDrafts[webhook.id] ?? webhook.name).trim();
    const channelRef =
      channelDrafts[webhook.id] ?? `${webhook.channel_id}@${webhook.channel_domain}`;
    if (!canManage || busyRef) return;
    if (!name) {
      error = 'Webhook names cannot be blank.';
      return;
    }
    if (!channels.some((channel) => entityRef(channel) === channelRef)) {
      error = 'Choose a manageable plaintext text, announcement, or forum channel.';
      return;
    }
    busyRef = webhook.id;
    error = '';
    notice = '';
    try {
      replaceWebhook(await updateGuildWebhook(guildRef, webhook, { name, channel_id: channelRef }));
      notice = 'Webhook saved.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not save the webhook.');
    } finally {
      busyRef = '';
    }
  }

  async function rotateWebhook(webhook: WebhookSummary) {
    if (
      !canManage ||
      busyRef ||
      !confirm(`Rotate the token for “${webhook.name}”? The current token will stop working.`)
    )
      return;
    busyRef = webhook.id;
    error = '';
    notice = '';
    revealedExecutionUrl = '';
    try {
      const updated = await rotateGuildWebhook(guildRef, webhook);
      replaceWebhook(updated);
      revealedExecutionUrl = updated.execution_url ?? '';
      notice = 'Webhook token rotated. The previous token no longer works.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not rotate the webhook token.');
    } finally {
      busyRef = '';
    }
  }

  async function removeWebhook(webhook: WebhookSummary) {
    const followed = isChannelFollowerWebhook(webhook);
    const question = followed
      ? `Stop following ${webhook.source_channel?.name ?? webhook.name}?`
      : `Delete the webhook “${webhook.name}”? This cannot be undone.`;
    if (!canManage || busyRef || !confirm(question)) return;
    busyRef = webhook.id;
    error = '';
    notice = '';
    try {
      await deleteGuildWebhook(guildRef, webhook);
      webhooks = webhooks.filter((item) => item.id !== webhook.id);
      notice = followed ? 'Stopped following that announcement channel.' : 'Webhook deleted.';
      revealedExecutionUrl = '';
    } catch (caught) {
      error = userErrorMessage(
        caught,
        followed ? 'Could not stop following that channel.' : 'Could not delete the webhook.'
      );
    } finally {
      busyRef = '';
    }
  }

  async function copyExecutionUrl(url = revealedExecutionUrl) {
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      notice = 'Webhook URL copied.';
    } catch {
      error = 'Could not copy automatically. Select the webhook URL and copy it manually.';
    }
  }

  function waitForScan(signal: AbortSignal): Promise<void> {
    return new Promise((resolve, reject) => {
      const timeout = window.setTimeout(resolve, 1000);
      signal.addEventListener(
        'abort',
        () => {
          window.clearTimeout(timeout);
          reject(new DOMException('Aborted', 'AbortError'));
        },
        { once: true }
      );
    });
  }

  async function uploadAvatar(webhook: WebhookSummary, file: File | null, input: HTMLInputElement) {
    if (!file || !canManage || busyRef) return;
    if (!['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(file.type) || !file.size) {
      error = 'Choose a non-empty PNG, JPEG, GIF, or WebP image.';
      input.value = '';
      return;
    }
    const controller = new AbortController();
    busyRef = webhook.id;
    error = '';
    notice = '';
    try {
      const ticket = await createGuildWebhookAvatarTicket(
        guildRef,
        webhook,
        {
          filename: file.name || 'webhook-avatar',
          content_type: file.type,
          size: file.size
        },
        controller.signal
      );
      await uploadObject(ticket, file, () => undefined, controller.signal);
      let updated: WebhookSummary | null = null;
      for (let attempt = 0; attempt < 45; attempt += 1) {
        const result = await commitGuildWebhookAvatar(
          guildRef,
          webhook,
          ticket.id,
          controller.signal
        );
        if ('guild_id' in result) {
          updated = result;
          break;
        }
        if (['infected', 'rejected', 'failed'].includes(result.attachment.scan_status)) {
          throw new Error('The webhook avatar did not pass media safety processing.');
        }
        await waitForScan(controller.signal);
      }
      if (!updated) throw new Error('Webhook avatar processing is taking longer than expected.');
      replaceWebhook(updated);
      input.value = '';
      notice = 'Webhook avatar updated.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not update the webhook avatar.');
    } finally {
      controller.abort();
      busyRef = '';
    }
  }

  async function removeAvatar(webhook: WebhookSummary) {
    if (
      !canManage ||
      busyRef ||
      !webhook.avatar_hash ||
      !confirm(`Remove the avatar for “${webhook.name}”?`)
    )
      return;
    busyRef = webhook.id;
    error = '';
    notice = '';
    try {
      replaceWebhook(await deleteGuildWebhookAvatar(guildRef, webhook));
      notice = 'Webhook avatar removed.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not remove the webhook avatar.');
    } finally {
      busyRef = '';
    }
  }
</script>

<section id="webhooks" class="integration-section" aria-labelledby="guild-webhooks-title">
  <header>
    <span class="section-icon" aria-hidden="true"><Icon name="globe" size={19} /></span>
    <div>
      <span>Incoming automation</span>
      <h2 id="guild-webhooks-title">Webhooks</h2>
      <p>
        Create and manage webhook identities across this server. Authorized managers can copy a
        webhook URL whenever an external website needs it.
      </p>
    </div>
  </header>

  {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  {#if notice}<p class="form-success" role="status">{notice}</p>{/if}
  {#if revealedExecutionUrl}
    <div class="token-notice" role="status">
      <Icon name="lock" size={18} />
      <span><strong>Webhook URL</strong><code>{revealedExecutionUrl}</code></span>
      <button class="secondary-button" type="button" onclick={() => void copyExecutionUrl()}>
        Copy webhook URL
      </button>
    </div>
  {/if}

  {#if !canManage}
    <div class="empty-state">
      <strong>Manage Webhooks is required</strong>
      <p>A server administrator can grant this permission before webhook details are shown.</p>
    </div>
  {:else if loading}
    <div class="empty-state" role="status">Loading webhooks…</div>
  {:else}
    <form
      class="create-form"
      onsubmit={(event) => {
        event.preventDefault();
        void createWebhook();
      }}
    >
      <label>
        <span>Webhook name</span>
        <input
          bind:value={newName}
          minlength="1"
          maxlength="80"
          required
          disabled={Boolean(busyRef)}
        />
      </label>
      <label>
        <span>Post to channel</span>
        <select
          bind:value={newChannelRef}
          required
          disabled={Boolean(busyRef) || channels.length === 0}
        >
          {#each channels as channel (entityRef(channel))}
            <option value={entityRef(channel)}>#{channel.name ?? 'channel'}</option>
          {/each}
        </select>
      </label>
      <button disabled={Boolean(busyRef) || !newName.trim() || !newChannelRef}>
        {busyRef === 'create' ? 'Creating…' : 'Create webhook'}
      </button>
    </form>
    {#if channels.length === 0}
      <p class="help-copy">
        No manageable plaintext text, announcement, or forum channel is available.
      </p>
    {/if}

    <div class="webhook-list" aria-label="Server webhooks">
      {#each ordinaryWebhooks as webhook (webhook.id)}
        <article class="webhook-row">
          <div class="avatar-editor">
            {#if webhook.avatar_hash}
              <img
                src={assetUrl(webhook.avatar_hash, 'thumbnail_128', webhook.guild_domain)}
                alt=""
              />
            {:else}
              <span class="avatar-placeholder" aria-hidden="true"
                ><Icon name="image" size={20} /></span
              >
            {/if}
            <label class="secondary-button">
              <span>{webhook.avatar_hash ? 'Replace avatar' : 'Add avatar'}</span>
              <input
                type="file"
                accept="image/png,image/jpeg,image/gif,image/webp"
                disabled={Boolean(busyRef)}
                onchange={(event) => {
                  const input = event.currentTarget;
                  void uploadAvatar(webhook, input.files?.[0] ?? null, input);
                }}
              />
            </label>
            {#if webhook.avatar_hash}
              <button
                class="danger-text"
                type="button"
                disabled={Boolean(busyRef)}
                onclick={() => void removeAvatar(webhook)}>Remove avatar</button
              >
            {/if}
          </div>
          <div class="fields">
            <label>
              <span>Name <small>ID {webhook.id}</small></span>
              <input
                value={nameDrafts[webhook.id] ?? webhook.name}
                minlength="1"
                maxlength="80"
                disabled={Boolean(busyRef)}
                oninput={(event) =>
                  (nameDrafts = { ...nameDrafts, [webhook.id]: event.currentTarget.value })}
              />
            </label>
            <label>
              <span>Post to channel</span>
              <select
                value={channelDrafts[webhook.id] ??
                  `${webhook.channel_id}@${webhook.channel_domain}`}
                disabled={Boolean(busyRef)}
                onchange={(event) =>
                  (channelDrafts = {
                    ...channelDrafts,
                    [webhook.id]: event.currentTarget.value
                  })}
              >
                {#each channels as channel (entityRef(channel))}
                  <option value={entityRef(channel)}>#{channel.name ?? 'channel'}</option>
                {/each}
              </select>
            </label>
          </div>
          <div class="actions">
            {#if webhook.execution_url}
              <button
                class="secondary-button"
                type="button"
                disabled={Boolean(busyRef)}
                onclick={() => void copyExecutionUrl(webhook.execution_url)}
                >Copy webhook URL</button
              >
            {/if}
            <button
              class="secondary-button"
              type="button"
              disabled={Boolean(busyRef) || !(nameDrafts[webhook.id] ?? webhook.name).trim()}
              onclick={() => void saveWebhook(webhook)}>Save</button
            >
            <button
              class="secondary-button"
              type="button"
              disabled={Boolean(busyRef)}
              onclick={() => void rotateWebhook(webhook)}>Rotate token</button
            >
            <button
              class="danger-text"
              type="button"
              disabled={Boolean(busyRef)}
              onclick={() => void removeWebhook(webhook)}>Delete</button
            >
          </div>
        </article>
      {:else}
        <div class="empty-state">No ordinary webhooks have been created for this server.</div>
      {/each}
    </div>

    {#if followedChannels.length}
      <div class="followed-webhooks" aria-labelledby="incoming-follows-title">
        <div>
          <span>Following into this server</span>
          <h3 id="incoming-follows-title">Connected announcement channels</h3>
          <p>These system-managed webhooks deliver published posts into this server.</p>
        </div>
        {#each followedChannels as webhook (webhook.id)}
          <div class="follow-row">
            <span class="follow-mark" aria-hidden="true">#</span>
            <span>
              <strong>{webhook.source_channel?.name ?? webhook.name}</strong>
              <small>
                {webhook.source_guild?.name ?? 'Announcement source'} → {webhook.channel_id}@{webhook.channel_domain}{webhook.federated
                  ? ' · Federated'
                  : ''}
              </small>
            </span>
            <button
              class="danger-text"
              type="button"
              disabled={Boolean(busyRef)}
              onclick={() => void removeWebhook(webhook)}>Stop following</button
            >
          </div>
        {/each}
      </div>
    {/if}
  {/if}
</section>

<style>
  .integration-section {
    display: grid;
    gap: 16px;
    margin-top: 22px;
    border: 1px solid var(--line);
    border-radius: 13px;
    padding: 1rem;
    background: var(--surface);
  }
  header,
  .actions,
  .follow-row,
  .avatar-editor {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  header {
    align-items: flex-start;
  }
  header > div,
  .follow-row > span:nth-child(2) {
    min-width: 0;
  }
  header span,
  label > span,
  .followed-webhooks > div:first-child > span {
    color: var(--text-muted);
    font-size: 0.75rem;
    font-weight: 750;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }
  h2,
  h3,
  p {
    margin: 0.2rem 0;
  }
  header p,
  .help-copy,
  .empty-state p,
  .followed-webhooks p,
  small {
    color: var(--text-muted);
    line-height: 1.45;
  }
  .section-icon,
  .follow-mark,
  .avatar-placeholder {
    display: grid;
    flex: 0 0 36px;
    height: 36px;
    place-items: center;
    border-radius: 10px;
    color: var(--accent);
    background: color-mix(in srgb, var(--accent) 14%, transparent);
  }
  .create-form,
  .fields {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 10px;
  }
  .create-form {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto;
    align-items: end;
  }
  label {
    display: grid;
    gap: 6px;
  }
  input,
  select,
  .create-form button {
    min-height: 42px;
  }
  button,
  .secondary-button {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.6rem 0.75rem;
    color: var(--text);
    background: var(--surface-hover);
    font: inherit;
    font-weight: 750;
  }
  .create-form button {
    border-color: transparent;
    color: white;
    background: var(--accent);
  }
  button:disabled,
  input:disabled,
  select:disabled {
    opacity: 0.55;
  }
  .webhook-list {
    display: grid;
    gap: 10px;
  }
  .webhook-row {
    display: grid;
    gap: 13px;
    border: 1px solid var(--line);
    border-radius: 11px;
    padding: 13px;
    background: var(--surface-hover);
  }
  .avatar-editor img,
  .avatar-placeholder {
    width: 44px;
    height: 44px;
    border-radius: 12px;
    object-fit: cover;
  }
  .secondary-button input[type='file'] {
    display: none;
  }
  .danger-text {
    border-color: color-mix(in srgb, var(--danger, #ef6767) 42%, var(--line));
    color: var(--danger, #ef6767);
    background: transparent;
  }
  .token-notice,
  .empty-state,
  .form-error,
  .form-success {
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 12px;
  }
  .token-notice {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    border-color: color-mix(in srgb, #f3b85b 45%, var(--line));
  }
  .token-notice span,
  .token-notice strong,
  .token-notice code,
  .follow-row span,
  .follow-row strong,
  .follow-row small {
    display: block;
  }
  .token-notice code {
    overflow-wrap: anywhere;
    margin-top: 5px;
  }
  .form-error {
    color: var(--danger, #ef6767);
  }
  .form-success {
    color: var(--success, #49c98a);
  }
  .followed-webhooks {
    display: grid;
    gap: 10px;
    border-top: 1px solid var(--line);
    padding-top: 16px;
  }
  .follow-row {
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 11px;
  }
  .follow-row > span:nth-child(2) {
    flex: 1 1 240px;
  }
  @media (max-width: 720px) {
    .create-form,
    .fields {
      grid-template-columns: 1fr;
    }
  }
</style>
