<script lang="ts">
  import { t } from '$lib/ui/locale';

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
      mediaError = $t('ui_choose_an_uploaded_store_asset_17841268');
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
      mediaError = $t('ui_enter_a_valid_youtube_video_id_or_https_youtu_1d77e6c7');
      return;
    }
    if (media.length >= DIRECTORY_MEDIA_LIMIT) {
      mediaError = `You can show at most ${DIRECTORY_MEDIA_LIMIT} images or videos.`;
      return;
    }
    if (media.some((item) => item.type === 'youtube' && item.video_id === videoId)) {
      mediaError = $t('ui_that_youtube_video_is_already_included_76c8182a');
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
  <legend>{$t('ui_product_page_content_28db4a96')}</legend>
  <div class="editor-block">
    <div class="heading">
      <div>
        <h3>{$t('ui_media_d357175c')}</h3>
        <p>{$t('ui_arrange_up_to_five_uploaded_store_images_or_y_bcc75114')}</p>
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
                  ? (asset?.name ?? $t('ui_missing_store_asset_d3d0edb8'))
                  : $t('ui_youtube_video_4d78938a')}</strong
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
                >{$t('ui_remove_c3812fc4')}</button
              >
            </div>
          </li>
        {/each}
      </ol>
    {:else}<p class="empty">{$t('ui_no_product_page_media_selected_83ab4f7b')}</p>{/if}
    <div class="add-grid">
      <label
        >{$t('ui_uploaded_store_image_8df213ef')}<select bind:value={assetDraft}>
          <option value="">{$t('ui_choose_an_asset_f8ea137b')}</option>
          {#each availableAssets as asset (asset.id)}<option value={asset.id}>{asset.name}</option
            >{/each}
        </select></label
      ><button
        type="button"
        disabled={disabled || !assetDraft || media.length >= DIRECTORY_MEDIA_LIMIT}
        onclick={addImage}>{$t('ui_add_image_04136060')}</button
      >
      <label
        >{$t('ui_youtube_id_or_url_510ed94a')}<input
          bind:value={youtubeDraft}
          maxlength="2048"
          placeholder="https://youtu.be/…"
        /></label
      ><button
        type="button"
        disabled={disabled || !youtubeDraft.trim() || media.length >= DIRECTORY_MEDIA_LIMIT}
        onclick={addYouTube}>{$t('ui_add_video_36aeaefe')}</button
      >
    </div>
    {#if !availableAssets.length}<small
        >{$t('ui_upload_an_asset_with_the_ef8f054d')} <strong>store</strong>
        {$t('ui_kind_in_assets_emoji_to_add_another_image_b46d3acd')}</small
      >{/if}
    {#if mediaError}<p class="inline-error" role="alert">{mediaError}</p>{/if}
  </div>

  <div class="editor-block">
    <div class="heading">
      <div>
        <h3>{$t('ui_external_links_2cedb069')}</h3>
        <p>{$t('ui_add_up_to_five_named_https_links_such_as_docu_6f5e133a')}</p>
      </div>
      <span>{externalLinks.length}/{DIRECTORY_EXTERNAL_LINK_LIMIT}</span>
    </div>
    <div class="link-list">
      {#each externalLinks as link, index (index)}
        <div class="link-row">
          <label
            >{$t('ui_name_dcd1d522')}<input
              value={link.name}
              maxlength="100"
              oninput={(event) =>
                updateExternalLink(index, 'name', (event.currentTarget as HTMLInputElement).value)}
            /></label
          >
          <label
            >{$t('ui_https_url_c3cf95f4')}<input
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
              >{$t('ui_remove_c3812fc4')}</button
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
      >{$t('ui_add_external_link_18a39758')}</button
    >
  </div>

  <div class="editor-block">
    <div class="heading">
      <div>
        <h3>{$t('ui_languages_318655ce')}</h3>
        <p>{$t('ui_select_supported_languages_and_optionally_tra_731becd1')}</p>
      </div>
      <span>{supportedLocales.length}/{DIRECTORY_LOCALES.length}</span>
    </div>
    <div class="locale-add">
      <label
        >{$t('ui_language_a4fe6526')}<select bind:value={localeDraft}>
          <option value="">{$t('ui_choose_a_language_f8416d79')}</option>
          {#each availableLocales as locale (locale[0])}<option value={locale[0]}
              >{locale[1]} ({locale[0]})</option
            >{/each}
        </select></label
      ><button type="button" disabled={disabled || !localeDraft} onclick={addLocale}
        >{$t('ui_add_language_7ef7c441')}</button
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
              onclick={() => removeLocale(locale)}>{$t('ui_remove_c3812fc4')}</button
            >
          </div>
          <label
            >{$t('ui_localized_description_optional_e1efef01')}<textarea
              rows="3"
              maxlength="1000"
              value={descriptionLocalizations[locale] ?? ''}
              placeholder={$t('ui_leave_blank_to_use_the_default_description_129521fb')}
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
