<script lang="ts">
  import { desktopThemes } from '$lib/ui/desktop-themes';
  import { nativeError, nativeInvoke } from '$lib/platform/native';
  import { t } from '$lib/ui/locale';

  let { selected = $bindable(''), disabled = false }: { selected?: string; disabled?: boolean } =
    $props();

  let folderError = $state('');
  let opening = $state(false);
  const current = $derived($desktopThemes.themes.find((theme) => theme.id === selected));

  async function openFolder() {
    opening = true;
    folderError = '';
    try {
      await nativeInvoke('native_open_themes_folder');
    } catch (caught) {
      folderError = nativeError(caught).message ?? $t('desktop_themes_folder_error');
    } finally {
      opening = false;
    }
  }
</script>

<div class="desktop-theme-settings">
  <label class="form-field">
    <span>{$t('desktop_themes_label')}</span>
    <small>{$t('desktop_themes_description')}</small>
    <select bind:value={selected} {disabled}>
      <option value="">{$t('desktop_themes_default')}</option>
      {#if selected && !current}
        <option value={selected}>{selected}</option>
      {/if}
      {#each $desktopThemes.themes as theme (theme.id)}
        <option value={theme.id}>{theme.name}</option>
      {/each}
    </select>
    {#if current?.description}<small>{current.description}</small>{/if}
  </label>
  <div class="theme-folder-row">
    <button
      type="button"
      class="secondary-button"
      disabled={opening}
      onclick={() => void openFolder()}
    >
      {$t('desktop_themes_open_folder')}
    </button>
    <small>{$t('desktop_themes_auto_reload')}</small>
  </div>
  {#if $desktopThemes.missing}
    <p role="status">{$t('desktop_themes_missing')}</p>
  {/if}
  {#if $desktopThemes.skipped.length}
    <p role="status">
      {$t('desktop_themes_skipped', { files: $desktopThemes.skipped.join(', ') })}
    </p>
  {/if}
  {#if folderError || $desktopThemes.error}
    <p class="error-banner" role="alert">{folderError || $desktopThemes.error}</p>
  {/if}
</div>

<style>
  .desktop-theme-settings {
    display: grid;
    gap: 14px;
    min-width: 0;
  }
  .theme-folder-row {
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
  }
  .theme-folder-row button {
    flex-shrink: 0;
  }
  .theme-folder-row small {
    flex: 1 1 220px;
    color: var(--text-muted);
    line-height: 1.5;
  }
  p {
    margin: 0;
    overflow-wrap: anywhere;
  }
</style>
