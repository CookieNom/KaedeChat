<script lang="ts">
  import { t } from '$lib/ui/locale';

  import Icon from './Icon.svelte';

  let {
    id,
    file = null,
    disabled = false,
    required = false,
    onSelect
  }: {
    id: string;
    file?: File | null;
    disabled?: boolean;
    required?: boolean;
    onSelect: (file: File | null, input: HTMLInputElement) => void;
  } = $props();
  let input = $state<HTMLInputElement | null>(null);
</script>

<div class="image-upload-field">
  <input
    bind:this={input}
    {id}
    class="visually-hidden"
    type="file"
    accept="image/png,image/jpeg,image/gif,image/webp"
    {required}
    {disabled}
    onchange={(event) => onSelect(event.currentTarget.files?.[0] ?? null, event.currentTarget)}
  />
  <button type="button" {disabled} onclick={() => input?.click()}>
    <span class="upload-icon"><Icon name="image" size={22} /><b>+</b></span>
    <span
      ><strong>{file ? $t('ui_change_image_87b8b008') : $t('ui_choose_image_f7e6f67f')}</strong
      ><small>{file?.name ?? $t('ui_png_jpeg_gif_or_webp_f15287a9')}</small></span
    >
  </button>
</div>

<style>
  .image-upload-field button {
    display: flex;
    width: 100%;
    min-width: 220px;
    align-items: center;
    gap: 11px;
    padding: 10px 12px;
    border: 1px dashed var(--line);
    border-radius: 12px;
    background: var(--surface-soft);
    color: var(--text);
    text-align: left;
  }
  .image-upload-field button:hover:not(:disabled) {
    border-color: var(--accent);
    background: var(--surface-hover);
  }
  .upload-icon {
    position: relative;
    display: grid;
    width: 38px;
    height: 38px;
    flex: 0 0 auto;
    place-items: center;
    border-radius: 10px;
    background: color-mix(in srgb, var(--accent) 15%, transparent);
    color: var(--accent);
  }
  .upload-icon b {
    position: absolute;
    right: 3px;
    bottom: 0;
    font-size: 0.85rem;
  }
  button > span:last-child {
    display: grid;
    min-width: 0;
    gap: 2px;
  }
  small {
    overflow: hidden;
    color: var(--text-muted);
    font-size: 0.73rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
