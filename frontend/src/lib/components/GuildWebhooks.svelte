<script lang="ts">
  import { t } from '$lib/ui/locale';

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
      error = userErrorMessage(caught, $t('ui_could_not_load_webhooks_for_this_guild_95453ac7'));
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
      notice = $t('ui_webhook_created_its_url_remains_available_to__15821689');
    } catch (caught) {
      error = userErrorMessage(caught, $t('ui_could_not_create_the_webhook_4c1e6b2a'));
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
      error = $t('ui_webhook_names_cannot_be_blank_7a077d9b');
      return;
    }
    if (!channels.some((channel) => entityRef(channel) === channelRef)) {
      error = $t('ui_choose_a_manageable_plaintext_text_announceme_4b4d7c74');
      return;
    }
    busyRef = webhook.id;
    error = '';
    notice = '';
    try {
      replaceWebhook(await updateGuildWebhook(guildRef, webhook, { name, channel_id: channelRef }));
      notice = $t('ui_webhook_saved_11589616');
    } catch (caught) {
      error = userErrorMessage(caught, $t('ui_could_not_save_the_webhook_e3317c53'));
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
      notice = $t('ui_webhook_token_rotated_the_previous_token_no_l_b93ec265');
    } catch (caught) {
      error = userErrorMessage(caught, $t('ui_could_not_rotate_the_webhook_token_37a682e8'));
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
        followed
          ? $t('ui_could_not_stop_following_that_channel_e9de7de5')
          : $t('ui_could_not_delete_the_webhook_49321159')
      );
    } finally {
      busyRef = '';
    }
  }

  async function copyExecutionUrl(url = revealedExecutionUrl) {
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      notice = $t('ui_webhook_url_copied_d08595e0');
    } catch {
      error = $t('ui_could_not_copy_automatically_select_the_webho_529aa9dc');
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
      error = $t('ui_choose_a_non_empty_png_jpeg_gif_or_webp_image_bd72768e');
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
          throw new Error($t('ui_the_webhook_avatar_did_not_pass_media_safety__f45f1a3f'));
        }
        await waitForScan(controller.signal);
      }
      if (!updated)
        throw new Error($t('ui_webhook_avatar_processing_is_taking_longer_th_8cb9d57e'));
      replaceWebhook(updated);
      input.value = '';
      notice = $t('ui_webhook_avatar_updated_a4a0a1f5');
    } catch (caught) {
      error = userErrorMessage(caught, $t('ui_could_not_update_the_webhook_avatar_5257094d'));
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
      notice = $t('ui_webhook_avatar_removed_607e6eae');
    } catch (caught) {
      error = userErrorMessage(caught, $t('ui_could_not_remove_the_webhook_avatar_dc0cfbf1'));
    } finally {
      busyRef = '';
    }
  }
</script>

<section id="webhooks" class="integration-section" aria-labelledby="guild-webhooks-title">
  <header>
    <span class="section-icon" aria-hidden="true"><Icon name="globe" size={19} /></span>
    <div>
      <span>{$t('ui_incoming_automation_4c9a1317')}</span>
      <h2 id="guild-webhooks-title">{$t('ui_webhooks_45808d75')}</h2>
      <p>{$t('ui_create_and_manage_webhook_identities_across_t_580bad51')}</p>
    </div>
  </header>

  {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  {#if notice}<p class="form-success" role="status">{notice}</p>{/if}
  {#if revealedExecutionUrl}
    <div class="token-notice" role="status">
      <Icon name="lock" size={18} />
      <span
        ><strong>{$t('ui_webhook_url_84805a75')}</strong><code>{revealedExecutionUrl}</code></span
      >
      <button class="secondary-button" type="button" onclick={() => void copyExecutionUrl()}>
        {$t('ui_copy_webhook_url_69179edf')}
      </button>
    </div>
  {/if}

  {#if !canManage}
    <div class="empty-state">
      <strong>{$t('ui_manage_webhooks_is_required_df6097a8')}</strong>
      <p>{$t('ui_a_server_administrator_can_grant_this_permiss_7ccbb219')}</p>
    </div>
  {:else if loading}
    <div class="empty-state" role="status">{$t('ui_loading_webhooks_bb656a75')}</div>
  {:else}
    <form
      class="create-form"
      onsubmit={(event) => {
        event.preventDefault();
        void createWebhook();
      }}
    >
      <label>
        <span>{$t('ui_webhook_name_d28bddc1')}</span>
        <input
          bind:value={newName}
          minlength="1"
          maxlength="80"
          required
          disabled={Boolean(busyRef)}
        />
      </label>
      <label>
        <span>{$t('ui_post_to_channel_6bcc7da9')}</span>
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
        {busyRef === 'create' ? $t('ui_creating_c79ed949') : $t('ui_create_webhook_4a2b33ad')}
      </button>
    </form>
    {#if channels.length === 0}
      <p class="help-copy">{$t('ui_no_manageable_plaintext_text_announcement_or__1072f0d8')}</p>
    {/if}

    <div class="webhook-list" aria-label={$t('ui_server_webhooks_58d34859')}>
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
              <span
                >{webhook.avatar_hash
                  ? $t('ui_replace_avatar_ec965b00')
                  : $t('ui_add_avatar_97bc36ba')}</span
              >
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
                onclick={() => void removeAvatar(webhook)}>{$t('ui_remove_avatar_5ae2a862')}</button
              >
            {/if}
          </div>
          <div class="fields">
            <label>
              <span
                >{$t('ui_name_dcd1d522')}
                <small>{$t('ui_id_value0_19b3c40a', { value0: String(webhook.id) })}</small></span
              >
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
              <span>{$t('ui_post_to_channel_6bcc7da9')}</span>
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
                >{$t('ui_copy_webhook_url_69179edf')}</button
              >
            {/if}
            <button
              class="secondary-button"
              type="button"
              disabled={Boolean(busyRef) || !(nameDrafts[webhook.id] ?? webhook.name).trim()}
              onclick={() => void saveWebhook(webhook)}>{$t('ui_save_1509f561')}</button
            >
            <button
              class="secondary-button"
              type="button"
              disabled={Boolean(busyRef)}
              onclick={() => void rotateWebhook(webhook)}>{$t('ui_rotate_token_4ade7882')}</button
            >
            <button
              class="danger-text"
              type="button"
              disabled={Boolean(busyRef)}
              onclick={() => void removeWebhook(webhook)}>{$t('ui_delete_e2d0a549')}</button
            >
          </div>
        </article>
      {:else}
        <div class="empty-state">
          {$t('ui_no_ordinary_webhooks_have_been_created_for_th_ea33ab73')}
        </div>
      {/each}
    </div>

    {#if followedChannels.length}
      <div class="followed-webhooks" aria-labelledby="incoming-follows-title">
        <div>
          <span>{$t('ui_following_into_this_server_bfb9e7c4')}</span>
          <h3 id="incoming-follows-title">{$t('ui_connected_announcement_channels_65ee3071')}</h3>
          <p>{$t('ui_these_system_managed_webhooks_deliver_publish_7589b5c0')}</p>
        </div>
        {#each followedChannels as webhook (webhook.id)}
          <div class="follow-row">
            <span class="follow-mark" aria-hidden="true">#</span>
            <span>
              <strong>{webhook.source_channel?.name ?? webhook.name}</strong>
              <small>
                {webhook.source_guild?.name ?? $t('ui_announcement_source_f5d80a6d')} → {webhook.channel_id}@{webhook.channel_domain}{webhook.federated
                  ? $t('ui_federated_bfc21067')
                  : ''}
              </small>
            </span>
            <button
              class="danger-text"
              type="button"
              disabled={Boolean(busyRef)}
              onclick={() => void removeWebhook(webhook)}>{$t('ui_stop_following_fcc32e2a')}</button
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
