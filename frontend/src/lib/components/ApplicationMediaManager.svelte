<script lang="ts">
  import { onDestroy } from 'svelte';

  import { api, userErrorMessage } from '$lib/api/client';
  import type {
    ApplicationAsset,
    ApplicationAssetKind
  } from '$lib/chat/application-directory-editor';
  import { assetUrl } from '$lib/media/assets';
  import { completeScannedMediaResource } from '$lib/media/scanned';
  import { uploadObject, type UploadTicket } from '$lib/media/uploads';

  interface ApplicationEmoji {
    id: string;
    application_ref: string;
    name: string;
    media_hash: string;
    animated: boolean;
    available: boolean;
    version: number;
  }

  interface AttachmentStatus {
    id: string;
    scan_status: string;
  }

  let {
    applicationRef,
    onAssetsChange = () => undefined
  }: {
    applicationRef: string;
    onAssetsChange?: (assets: ApplicationAsset[]) => void;
  } = $props();

  const acceptedTypes = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);
  const assetKinds: ApplicationAssetKind[] = [
    'icon',
    'cover',
    'store',
    'achievement',
    'activity',
    'other'
  ];

  let assets = $state<ApplicationAsset[]>([]);
  let emojis = $state<ApplicationEmoji[]>([]);
  let loading = $state(true);
  let busy = $state('');
  let error = $state('');
  let notice = $state('');
  let uploadProgress = $state(0);
  let assetName = $state('');
  let assetKind = $state<ApplicationAssetKind>('other');
  let assetFile = $state<File | null>(null);
  let emojiName = $state('');
  let emojiFile = $state<File | null>(null);
  let assetInput = $state<HTMLInputElement>();
  let emojiInput = $state<HTMLInputElement>();
  let loadedRef = $state('');
  let controller = new AbortController();
  let requestGeneration = 0;

  const mediaDomain = $derived(loadedRef.slice(loadedRef.lastIndexOf('@') + 1));

  function requestIsCurrent(
    targetApplicationRef: string,
    signal: AbortSignal,
    generation: number
  ): boolean {
    return (
      !signal.aborted &&
      loadedRef === targetApplicationRef &&
      applicationRef === targetApplicationRef &&
      requestGeneration === generation
    );
  }

  function replaceAssets(nextAssets: ApplicationAsset[]): void {
    assets = nextAssets;
    onAssetsChange(nextAssets.map((asset) => ({ ...asset })));
  }

  async function load(
    applicationRef: string,
    signal: AbortSignal,
    generation: number
  ): Promise<void> {
    loading = true;
    error = '';
    const targetRef = encodeURIComponent(applicationRef);
    try {
      const [loadedAssets, loadedEmojis] = await Promise.all([
        api<ApplicationAsset[]>(`/applications/${targetRef}/assets`, { signal }),
        api<ApplicationEmoji[]>(`/applications/${targetRef}/emojis`, { signal })
      ]);
      if (!requestIsCurrent(applicationRef, signal, generation)) return;
      replaceAssets(loadedAssets);
      emojis = loadedEmojis;
    } catch (caught) {
      if (requestIsCurrent(applicationRef, signal, generation)) {
        error = userErrorMessage(caught, 'Could not load application assets and emoji.');
      }
    } finally {
      if (requestIsCurrent(applicationRef, signal, generation)) loading = false;
    }
  }

  function reload(applicationRef = loadedRef): Promise<void> {
    controller.abort();
    controller = new AbortController();
    const generation = ++requestGeneration;
    return load(applicationRef, controller.signal, generation);
  }

  function operationIsCurrent(targetApplicationRef: string, signal: AbortSignal): boolean {
    return (
      !signal.aborted &&
      loadedRef === targetApplicationRef &&
      applicationRef === targetApplicationRef
    );
  }

  function selectedFile(event: Event, kind: 'asset' | 'emoji'): void {
    const file = (event.currentTarget as HTMLInputElement).files?.[0] ?? null;
    if (kind === 'asset') assetFile = file;
    else emojiFile = file;
  }

  function validateImage(file: File): void {
    if (!acceptedTypes.has(file.type)) {
      throw new Error('Choose a PNG, JPEG, GIF, or WebP image.');
    }
    if (!file.size) throw new Error('The selected image is empty.');
  }

  async function createAsset(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!assetFile || !assetName.trim() || busy) return;
    const file = assetFile;
    const applicationRef = loadedRef;
    const targetRef = encodeURIComponent(applicationRef);
    const targetName = assetName.trim();
    const targetKind = assetKind;
    const signal = controller.signal;
    busy = 'asset-create';
    error = '';
    notice = '';
    uploadProgress = 0;
    try {
      validateImage(file);
      const ticket = await api<UploadTicket>(`/applications/${targetRef}/assets/tickets`, {
        method: 'POST',
        signal,
        body: JSON.stringify({
          filename: file.name || 'application-asset',
          content_type: file.type,
          size: file.size
        })
      });
      await uploadObject(
        ticket,
        file,
        (progress) => {
          if (operationIsCurrent(applicationRef, signal)) uploadProgress = progress;
        },
        signal
      );
      const commit = () =>
        api<ApplicationAsset | AttachmentStatus>(`/applications/${targetRef}/assets`, {
          method: 'POST',
          signal,
          body: JSON.stringify({
            attachment_id: ticket.id,
            kind: targetKind,
            name: targetName
          })
        });
      const created = await completeScannedMediaResource(
        commit,
        (value): value is ApplicationAsset => 'application_ref' in value,
        { signal }
      );
      if (!operationIsCurrent(applicationRef, signal)) return;
      replaceAssets(
        [...assets.filter((item) => item.id !== created.id), created].sort((a, b) =>
          `${a.kind}:${a.name}`.localeCompare(`${b.kind}:${b.name}`)
        )
      );
      assetName = '';
      assetFile = null;
      if (assetInput) assetInput.value = '';
      notice = `${created.name} is ready.`;
    } catch (caught) {
      if (operationIsCurrent(applicationRef, signal)) {
        error = userErrorMessage(caught, 'Could not create the application asset.');
      }
    } finally {
      if (operationIsCurrent(applicationRef, signal) && busy === 'asset-create') {
        busy = '';
        uploadProgress = 0;
      }
    }
  }

  async function createEmoji(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!emojiFile || !emojiName.trim() || busy) return;
    const file = emojiFile;
    const applicationRef = loadedRef;
    const targetRef = encodeURIComponent(applicationRef);
    const targetName = emojiName.trim();
    const signal = controller.signal;
    busy = 'emoji-create';
    error = '';
    notice = '';
    uploadProgress = 0;
    try {
      validateImage(file);
      if (!/^[A-Za-z0-9_]{2,32}$/.test(targetName)) {
        throw new Error('Emoji names use 2–32 letters, numbers, or underscores.');
      }
      const ticket = await api<UploadTicket>(`/applications/${targetRef}/emojis/tickets`, {
        method: 'POST',
        signal,
        body: JSON.stringify({
          filename: file.name || 'application-emoji',
          content_type: file.type,
          size: file.size
        })
      });
      await uploadObject(
        ticket,
        file,
        (progress) => {
          if (operationIsCurrent(applicationRef, signal)) uploadProgress = progress;
        },
        signal
      );
      const commit = () =>
        api<ApplicationEmoji | AttachmentStatus>(`/applications/${targetRef}/emojis`, {
          method: 'POST',
          signal,
          body: JSON.stringify({ attachment_id: ticket.id, name: targetName })
        });
      const created = await completeScannedMediaResource(
        commit,
        (value): value is ApplicationEmoji => 'application_ref' in value,
        { signal }
      );
      if (!operationIsCurrent(applicationRef, signal)) return;
      emojis = [...emojis.filter((item) => item.id !== created.id), created].sort((a, b) =>
        a.name.localeCompare(b.name)
      );
      emojiName = '';
      emojiFile = null;
      if (emojiInput) emojiInput.value = '';
      notice = `:${created.name}: is ready.`;
    } catch (caught) {
      if (operationIsCurrent(applicationRef, signal)) {
        error = userErrorMessage(caught, 'Could not create the application emoji.');
      }
    } finally {
      if (operationIsCurrent(applicationRef, signal) && busy === 'emoji-create') {
        busy = '';
        uploadProgress = 0;
      }
    }
  }

  async function saveAsset(asset: ApplicationAsset): Promise<void> {
    if (busy) return;
    const applicationRef = loadedRef;
    const targetRef = encodeURIComponent(applicationRef);
    const operation = `asset-${asset.id}`;
    const signal = controller.signal;
    const name = asset.name.trim();
    const kind = asset.kind;
    busy = operation;
    error = '';
    try {
      const updated = await api<ApplicationAsset>(`/applications/${targetRef}/assets/${asset.id}`, {
        method: 'PATCH',
        signal,
        body: JSON.stringify({ name, kind })
      });
      if (!operationIsCurrent(applicationRef, signal)) return;
      replaceAssets(assets.map((item) => (item.id === updated.id ? updated : item)));
      notice = `${updated.name} was updated.`;
    } catch (caught) {
      if (operationIsCurrent(applicationRef, signal)) {
        error = userErrorMessage(caught, 'Could not update the application asset.');
        busy = '';
        await reload(applicationRef);
      }
    } finally {
      if (operationIsCurrent(applicationRef, signal) && busy === operation) busy = '';
    }
  }

  async function deleteAsset(asset: ApplicationAsset): Promise<void> {
    if (busy || !confirm(`Delete ${asset.name}? Existing references will stop resolving.`)) return;
    const applicationRef = loadedRef;
    const targetRef = encodeURIComponent(applicationRef);
    const operation = `asset-${asset.id}`;
    const signal = controller.signal;
    busy = operation;
    error = '';
    try {
      await api(`/applications/${targetRef}/assets/${asset.id}`, { method: 'DELETE', signal });
      if (!operationIsCurrent(applicationRef, signal)) return;
      replaceAssets(assets.filter((item) => item.id !== asset.id));
      notice = `${asset.name} was deleted.`;
    } catch (caught) {
      if (operationIsCurrent(applicationRef, signal)) {
        error = userErrorMessage(caught, 'Could not delete the application asset.');
      }
    } finally {
      if (operationIsCurrent(applicationRef, signal) && busy === operation) busy = '';
    }
  }

  async function saveEmoji(emoji: ApplicationEmoji): Promise<void> {
    if (busy) return;
    const applicationRef = loadedRef;
    const targetRef = encodeURIComponent(applicationRef);
    const operation = `emoji-${emoji.id}`;
    const signal = controller.signal;
    const name = emoji.name.trim();
    busy = operation;
    error = '';
    try {
      const updated = await api<ApplicationEmoji>(`/applications/${targetRef}/emojis/${emoji.id}`, {
        method: 'PATCH',
        signal,
        body: JSON.stringify({ name })
      });
      if (!operationIsCurrent(applicationRef, signal)) return;
      emojis = emojis.map((item) => (item.id === updated.id ? updated : item));
      notice = `:${updated.name}: was updated.`;
    } catch (caught) {
      if (operationIsCurrent(applicationRef, signal)) {
        error = userErrorMessage(caught, 'Could not update the application emoji.');
        busy = '';
        await reload(applicationRef);
      }
    } finally {
      if (operationIsCurrent(applicationRef, signal) && busy === operation) busy = '';
    }
  }

  async function deleteEmoji(emoji: ApplicationEmoji): Promise<void> {
    if (busy || !confirm(`Delete :${emoji.name}:? Existing uses may stop rendering.`)) return;
    const applicationRef = loadedRef;
    const targetRef = encodeURIComponent(applicationRef);
    const operation = `emoji-${emoji.id}`;
    const signal = controller.signal;
    busy = operation;
    error = '';
    try {
      await api(`/applications/${targetRef}/emojis/${emoji.id}`, { method: 'DELETE', signal });
      if (!operationIsCurrent(applicationRef, signal)) return;
      emojis = emojis.filter((item) => item.id !== emoji.id);
      notice = `:${emoji.name}: was deleted.`;
    } catch (caught) {
      if (operationIsCurrent(applicationRef, signal)) {
        error = userErrorMessage(caught, 'Could not delete the application emoji.');
      }
    } finally {
      if (operationIsCurrent(applicationRef, signal) && busy === operation) busy = '';
    }
  }

  $effect(() => {
    if (applicationRef !== loadedRef) {
      loadedRef = applicationRef;
      assets = [];
      emojis = [];
      busy = '';
      notice = '';
      void reload(applicationRef);
    }
  });

  onDestroy(() => {
    requestGeneration += 1;
    controller.abort();
  });
</script>

{#if error}<div class="media-notice error" role="alert">{error}</div>{/if}
{#if notice}<div class="media-notice success" role="status">{notice}</div>{/if}

{#if loading}
  <p class="state">Loading application media…</p>
{:else}
  <div class="media-grid">
    <div>
      <h3>Application assets</h3>
      <p>Manage icons, covers, store art, achievements, and activity artwork.</p>
      <form class="create" onsubmit={createAsset}>
        <label
          >Asset name<input bind:value={assetName} minlength="1" maxlength="100" required /></label
        >
        <label
          >Kind<select bind:value={assetKind}>
            {#each assetKinds as kind (kind)}<option value={kind}>{kind}</option>{/each}
          </select></label
        >
        <label
          >Image<input
            bind:this={assetInput}
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp"
            required
            onchange={(event) => selectedFile(event, 'asset')}
          /></label
        >
        <button disabled={Boolean(busy) || !assetFile || !assetName.trim()}>
          {busy === 'asset-create' ? `Uploading ${uploadProgress}%` : 'Add asset'}
        </button>
      </form>
      <div class="items">
        {#each assets as asset (asset.id)}
          <article>
            <img src={assetUrl(asset.media_hash, 'thumbnail_512', mediaDomain)} alt="" />
            <div class="fields">
              <label>Name<input bind:value={asset.name} maxlength="100" /></label>
              <label
                >Kind<select bind:value={asset.kind}>
                  {#each assetKinds as kind (kind)}<option value={kind}>{kind}</option>{/each}
                </select></label
              >
              <small>v{asset.version}{asset.width ? ` · ${asset.width}×${asset.height}` : ''}</small
              >
            </div>
            <div class="actions">
              <button
                disabled={Boolean(busy) || !asset.name.trim()}
                onclick={() => saveAsset(asset)}>Save</button
              >
              <button class="danger" disabled={Boolean(busy)} onclick={() => deleteAsset(asset)}
                >Delete</button
              >
            </div>
          </article>
        {/each}
        {#if !assets.length}<p class="state">No application assets yet.</p>{/if}
      </div>
    </div>

    <div>
      <h3>Application emoji</h3>
      <p>Application emoji are portable and do not consume a guild’s emoji slots.</p>
      <form class="create" onsubmit={createEmoji}>
        <label
          >Emoji name<input
            bind:value={emojiName}
            minlength="2"
            maxlength="32"
            pattern="[A-Za-z0-9_]+"
            required
          /></label
        >
        <label
          >Image<input
            bind:this={emojiInput}
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp"
            required
            onchange={(event) => selectedFile(event, 'emoji')}
          /></label
        >
        <button disabled={Boolean(busy) || !emojiFile || !emojiName.trim()}>
          {busy === 'emoji-create' ? `Uploading ${uploadProgress}%` : 'Add emoji'}
        </button>
      </form>
      <div class="items">
        {#each emojis as emoji (emoji.id)}
          <article>
            <img src={assetUrl(emoji.media_hash, 'thumbnail_128', mediaDomain)} alt="" />
            <div class="fields">
              <label>Name<input bind:value={emoji.name} minlength="2" maxlength="32" /></label>
              <small
                >{emoji.animated ? 'Animated' : 'Static'} ·
                {emoji.available ? 'Available' : 'Unavailable'} · v{emoji.version}</small
              >
            </div>
            <div class="actions">
              <button
                disabled={Boolean(busy) || !emoji.name.trim()}
                onclick={() => saveEmoji(emoji)}>Save</button
              >
              <button class="danger" disabled={Boolean(busy)} onclick={() => deleteEmoji(emoji)}
                >Delete</button
              >
            </div>
          </article>
        {/each}
        {#if !emojis.length}<p class="state">No application emoji yet.</p>{/if}
      </div>
    </div>
  </div>
{/if}

<style>
  .media-notice {
    margin: 0.75rem 0;
    border-radius: 8px;
    padding: 0.75rem;
  }
  .media-notice.error {
    color: var(--danger);
    background: color-mix(in srgb, var(--danger) 12%, transparent);
  }
  .media-notice.success {
    color: var(--success, #70c58f);
    background: color-mix(in srgb, var(--success, #70c58f) 12%, transparent);
  }
  .media-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1.25rem;
  }
  h3 {
    margin-bottom: 0.25rem;
  }
  h3 + p,
  .state,
  small {
    color: var(--text-muted);
  }
  .create,
  .fields {
    display: grid;
    gap: 0.55rem;
  }
  .create {
    margin: 1rem 0;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 0.85rem;
    background: var(--bg);
  }
  label {
    display: grid;
    gap: 0.3rem;
    font-size: 0.78rem;
    font-weight: 700;
  }
  input,
  select {
    box-sizing: border-box;
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: 0.55rem;
    color: var(--text);
    background: var(--input-bg, var(--bg));
  }
  .items {
    display: grid;
    gap: 0.65rem;
  }
  article {
    display: grid;
    grid-template-columns: 72px minmax(0, 1fr) auto;
    gap: 0.75rem;
    align-items: center;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 0.75rem;
  }
  article > img {
    width: 72px;
    height: 72px;
    border-radius: 9px;
    object-fit: contain;
    background: var(--bg);
  }
  .actions {
    display: grid;
    gap: 0.4rem;
  }
  button {
    border: 0;
    border-radius: 7px;
    padding: 0.55rem 0.75rem;
    color: var(--on-accent, white);
    background: var(--accent);
    font: inherit;
    font-weight: 800;
    cursor: pointer;
  }
  button.danger {
    background: var(--danger);
  }
  button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }
  @media (max-width: 850px) {
    .media-grid {
      grid-template-columns: 1fr;
    }
  }
  @media (max-width: 560px) {
    article {
      grid-template-columns: 56px minmax(0, 1fr);
    }
    article > img {
      width: 56px;
      height: 56px;
    }
    .actions {
      grid-column: 1 / -1;
      grid-template-columns: 1fr 1fr;
    }
  }
</style>
