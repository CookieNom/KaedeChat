<script lang="ts">
  import type {
    DirectoryExternalLink,
    DirectoryLocale,
    DirectoryMediaInput
  } from '$lib/chat/application-directory';
  import {
    DIRECTORY_EXTERNAL_LINK_LIMIT,
    DIRECTORY_LOCALES,
    DIRECTORY_MEDIA_LIMIT,
    moveDirectoryItem,
    parseYouTubeVideoId,
    type ApplicationAsset
  } from '$lib/chat/application-directory-editor';
  import { assetUrl } from '$lib/media/assets';

  interface Props {
    originDomain: string;
    media: DirectoryMediaInput[];
    externalLinks: DirectoryExternalLink[];
    supportedLocales: DirectoryLocale[];
    descriptionLocalizations: Partial<Record<DirectoryLocale, string>>;
    assets: ApplicationAsset[];
    disabled?: boolean;
    onMediaChange: (value: DirectoryMediaInput[]) => void;
    onExternalLinksChange: (value: DirectoryExternalLink[]) => void;
    onSupportedLocalesChange: (value: DirectoryLocale[]) => void;
    onDescriptionLocalizationsChange: (value: Partial<Record<DirectoryLocale, string>>) => void;
  }

  let {
    originDomain,
    media,
    externalLinks,
    supportedLocales,
    descriptionLocalizations,
    assets,
    disabled = false,
    onMediaChange,
    onExternalLinksChange,
    onSupportedLocalesChange,
    onDescriptionLocalizationsChange
  }: Props = $props();

  let assetDraft = $state('');
  let youtubeDraft = $state('');
  let localeDraft = $state<DirectoryLocale | ''>('');
  let mediaError = $state('');

  const assetsById = $derived(new Map(assets.map((asset) => [asset.id, asset])));
  const usedImageIds = $derived(
    new Set(media.filter((item) => item.type === 'image').map((item) => item.asset_id))
  );
  const availableAssets = $derived(
    assets.filter((asset) => asset.kind === 'store' && !usedImageIds.has(asset.id))
  );
  const availableLocales = $derived(
    DIRECTORY_LOCALES.filter(([locale]) => !supportedLocales.includes(locale))
  );

  function addImage(): void {
    mediaError = '';
    const asset = assetsById.get(assetDraft);
    if (!asset || asset.kind !== 'store') {
      mediaError = 'Choose an uploaded store asset.';
      return;
    }
    if (media.length >= DIRECTORY_MEDIA_LIMIT) {
      mediaError = `You can show at most ${DIRECTORY_MEDIA_LIMIT} images or videos.`;
      return;
    }
    if (!usedImageIds.has(asset.id))
      onMediaChange([...media, { type: 'image', asset_id: asset.id }]);
    assetDraft = '';
  }

  function addYouTube(): void {
    mediaError = '';
    const videoId = parseYouTubeVideoId(youtubeDraft);
    if (!videoId) {
      mediaError = 'Enter a valid YouTube video ID or HTTPS YouTube URL.';
      return;
    }
    if (media.length >= DIRECTORY_MEDIA_LIMIT) {
      mediaError = `You can show at most ${DIRECTORY_MEDIA_LIMIT} images or videos.`;
      return;
    }
    if (media.some((item) => item.type === 'youtube' && item.video_id === videoId)) {
      mediaError = 'That YouTube video is already included.';
      return;
    }
    onMediaChange([...media, { type: 'youtube', video_id: videoId }]);
    youtubeDraft = '';
  }

  function updateExternalLink(
    index: number,
    field: keyof DirectoryExternalLink,
    value: string
  ): void {
    onExternalLinksChange(
      externalLinks.map((link, linkIndex) =>
        linkIndex === index ? { ...link, [field]: value } : link
      )
    );
  }

  function addLocale(): void {
    if (!localeDraft || supportedLocales.includes(localeDraft)) return;
    onSupportedLocalesChange([...supportedLocales, localeDraft].sort());
    localeDraft = '';
  }

  function removeLocale(locale: DirectoryLocale): void {
    onSupportedLocalesChange(supportedLocales.filter((item) => item !== locale));
    const next = { ...descriptionLocalizations };
    delete next[locale];
    onDescriptionLocalizationsChange(next);
  }

  function localeName(locale: DirectoryLocale): string {
    return DIRECTORY_LOCALES.find(([value]) => value === locale)?.[1] ?? locale;
  }
</script>

<fieldset {disabled}>
  <legend>Product page content</legend>
  <div class="editor-block">
    <div class="heading">
      <div>
        <h3>Media</h3>
        <p>Arrange up to five uploaded store images or YouTube videos.</p>
      </div>
      <span>{media.length}/{DIRECTORY_MEDIA_LIMIT}</span>
    </div>
    {#if media.length}
      <ol class="media-list">
        {#each media as item, index (`${item.type}:${item.type === 'image' ? item.asset_id : item.video_id}`)}
          {@const asset = item.type === 'image' ? assetsById.get(item.asset_id) : null}
          <li>
            <div class="media-thumb">
              {#if item.type === 'image' && asset}
                <img src={assetUrl(asset.media_hash, 'thumbnail_128', originDomain)} alt="" />
              {:else}<span aria-hidden="true">▶</span>{/if}
            </div>
            <div>
              <strong
                >{item.type === 'image'
                  ? (asset?.name ?? 'Missing store asset')
                  : 'YouTube video'}</strong
              >
              <small>{item.type === 'image' ? `Asset ${item.asset_id}` : item.video_id}</small>
            </div>
            <div class="row-actions">
              <button
                type="button"
                aria-label={`Move ${item.type === 'image' ? (asset?.name ?? 'image') : 'video'} up`}
                disabled={disabled || index === 0}
                onclick={() => onMediaChange(moveDirectoryItem(media, index, -1))}>↑</button
              >
              <button
                type="button"
                aria-label={`Move ${item.type === 'image' ? (asset?.name ?? 'image') : 'video'} down`}
                disabled={disabled || index === media.length - 1}
                onclick={() => onMediaChange(moveDirectoryItem(media, index, 1))}>↓</button
              >
              <button
                class="remove"
                type="button"
                aria-label={`Remove ${item.type === 'image' ? (asset?.name ?? 'image') : 'video'}`}
                onclick={() => onMediaChange(media.filter((_, itemIndex) => itemIndex !== index))}
                >Remove</button
              >
            </div>
          </li>
        {/each}
      </ol>
    {:else}<p class="empty">No product-page media selected.</p>{/if}
    <div class="add-grid">
      <label
        >Uploaded store image<select bind:value={assetDraft}>
          <option value="">Choose an asset</option>
          {#each availableAssets as asset (asset.id)}<option value={asset.id}>{asset.name}</option
            >{/each}
        </select></label
      ><button
        type="button"
        disabled={disabled || !assetDraft || media.length >= DIRECTORY_MEDIA_LIMIT}
        onclick={addImage}>Add image</button
      >
      <label
        >YouTube ID or URL<input
          bind:value={youtubeDraft}
          maxlength="2048"
          placeholder="https://youtu.be/…"
        /></label
      ><button
        type="button"
        disabled={disabled || !youtubeDraft.trim() || media.length >= DIRECTORY_MEDIA_LIMIT}
        onclick={addYouTube}>Add video</button
      >
    </div>
    {#if !availableAssets.length}<small
        >Upload an asset with the <strong>store</strong> kind in Assets & emoji to add another image.</small
      >{/if}
    {#if mediaError}<p class="inline-error" role="alert">{mediaError}</p>{/if}
  </div>

  <div class="editor-block">
    <div class="heading">
      <div>
        <h3>External links</h3>
        <p>Add up to five named HTTPS links, such as documentation or a community.</p>
      </div>
      <span>{externalLinks.length}/{DIRECTORY_EXTERNAL_LINK_LIMIT}</span>
    </div>
    <div class="link-list">
      {#each externalLinks as link, index (index)}
        <div class="link-row">
          <label
            >Name<input
              value={link.name}
              maxlength="100"
              oninput={(event) =>
                updateExternalLink(index, 'name', (event.currentTarget as HTMLInputElement).value)}
            /></label
          >
          <label
            >HTTPS URL<input
              type="url"
              value={link.url}
              maxlength="2048"
              placeholder="https://docs.example"
              oninput={(event) =>
                updateExternalLink(index, 'url', (event.currentTarget as HTMLInputElement).value)}
            /></label
          >
          <div class="row-actions">
            <button
              type="button"
              aria-label={`Move ${link.name || `link ${index + 1}`} up`}
              disabled={disabled || index === 0}
              onclick={() => onExternalLinksChange(moveDirectoryItem(externalLinks, index, -1))}
              >↑</button
            >
            <button
              type="button"
              aria-label={`Move ${link.name || `link ${index + 1}`} down`}
              disabled={disabled || index === externalLinks.length - 1}
              onclick={() => onExternalLinksChange(moveDirectoryItem(externalLinks, index, 1))}
              >↓</button
            >
            <button
              class="remove"
              type="button"
              aria-label={`Remove ${link.name || `link ${index + 1}`}`}
              onclick={() =>
                onExternalLinksChange(externalLinks.filter((_, linkIndex) => linkIndex !== index))}
              >Remove</button
            >
          </div>
        </div>
      {/each}
    </div>
    <button
      class="secondary"
      type="button"
      disabled={disabled || externalLinks.length >= DIRECTORY_EXTERNAL_LINK_LIMIT}
      onclick={() => onExternalLinksChange([...externalLinks, { name: '', url: '' }])}
      >Add external link</button
    >
  </div>

  <div class="editor-block">
    <div class="heading">
      <div>
        <h3>Languages</h3>
        <p>Select supported languages and optionally translate the full description.</p>
      </div>
      <span>{supportedLocales.length}/{DIRECTORY_LOCALES.length}</span>
    </div>
    <div class="locale-add">
      <label
        >Language<select bind:value={localeDraft}>
          <option value="">Choose a language</option>
          {#each availableLocales as locale (locale[0])}<option value={locale[0]}
              >{locale[1]} ({locale[0]})</option
            >{/each}
        </select></label
      ><button type="button" disabled={disabled || !localeDraft} onclick={addLocale}
        >Add language</button
      >
    </div>
    <div class="locale-list">
      {#each supportedLocales as locale (locale)}
        <div class="locale-row">
          <div class="locale-heading">
            <strong>{localeName(locale)} <small>{locale}</small></strong>
            <button
              class="remove"
              type="button"
              aria-label={`Remove ${localeName(locale)}`}
              onclick={() => removeLocale(locale)}>Remove</button
            >
          </div>
          <label
            >Localized description (optional)<textarea
              rows="3"
              maxlength="1000"
              value={descriptionLocalizations[locale] ?? ''}
              placeholder="Leave blank to use the default description."
              oninput={(event) =>
                onDescriptionLocalizationsChange({
                  ...descriptionLocalizations,
                  [locale]: (event.currentTarget as HTMLTextAreaElement).value
                })}
            ></textarea></label
          >
        </div>
      {/each}
    </div>
  </div>
</fieldset>

<style>
  fieldset {
    display: grid;
    gap: 1rem;
    min-width: 0;
    margin: 1.3rem 0 0;
    border: 0;
    padding: 0;
  }
  legend {
    padding: 0;
    font-size: 1.05rem;
    font-weight: 800;
  }
  .editor-block {
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 1rem;
    background: var(--bg);
  }
  .heading,
  .locale-heading {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    align-items: start;
  }
  h3,
  p {
    margin: 0;
  }
  .heading p,
  small,
  .empty {
    color: var(--text-muted);
  }
  .heading > span {
    white-space: nowrap;
    color: var(--text-muted);
    font-size: 0.78rem;
  }
  .media-list,
  .link-list,
  .locale-list {
    display: grid;
    gap: 0.65rem;
    margin: 0.85rem 0;
    padding: 0;
    list-style: none;
  }
  .media-list li {
    display: grid;
    grid-template-columns: 64px minmax(0, 1fr) auto;
    gap: 0.7rem;
    align-items: center;
    border-top: 1px solid var(--line);
    padding-top: 0.65rem;
  }
  .media-list li > div:nth-child(2) {
    display: grid;
  }
  .media-thumb {
    display: grid;
    place-items: center;
    width: 64px;
    height: 44px;
    overflow: hidden;
    border-radius: 6px;
    background: var(--surface-hover);
  }
  .media-thumb img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .row-actions {
    display: flex;
    gap: 0.35rem;
    align-items: center;
  }
  button {
    border: 0;
    border-radius: 7px;
    padding: 0.55rem 0.7rem;
    color: var(--on-accent, white);
    background: var(--accent);
    font: inherit;
    font-weight: 750;
    cursor: pointer;
  }
  button:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }
  button.remove,
  button.secondary,
  .row-actions button {
    color: var(--text);
    background: var(--surface-hover);
  }
  button.remove {
    color: var(--danger);
  }
  .add-grid,
  .locale-add {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 0.55rem;
    align-items: end;
    margin: 0.75rem 0;
  }
  label {
    display: grid;
    gap: 0.35rem;
    color: var(--text);
    font-size: 0.78rem;
    font-weight: 700;
  }
  input,
  textarea,
  select {
    box-sizing: border-box;
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: 0.6rem;
    color: var(--text);
    background: var(--input-bg, var(--surface));
    font: inherit;
  }
  .link-row {
    display: grid;
    grid-template-columns: minmax(120px, 0.6fr) minmax(180px, 1.4fr) auto;
    gap: 0.55rem;
    align-items: end;
  }
  .locale-row {
    border-top: 1px solid var(--line);
    padding-top: 0.75rem;
  }
  .locale-heading strong {
    display: flex;
    gap: 0.45rem;
    align-items: baseline;
  }
  .inline-error {
    margin-top: 0.65rem;
    color: var(--danger);
  }
  @media (max-width: 720px) {
    .media-list li,
    .link-row {
      grid-template-columns: 1fr;
    }
    .media-thumb {
      width: 100%;
      height: 100px;
    }
    .row-actions {
      flex-wrap: wrap;
    }
  }
</style>
