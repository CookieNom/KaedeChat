<script lang="ts">
  import { tick } from 'svelte';

  export interface Completion {
    value: string;
    label: string;
    detail?: string;
  }

  let {
    query,
    options,
    onSelect,
    listboxId = 'message-suggestions',
    onActiveIndexChange,
    onOpenChange
  }: {
    query: string;
    options: Completion[];
    onSelect: (completion: Completion) => void;
    listboxId?: string;
    onActiveIndexChange?: (index: number) => void;
    onOpenChange?: (open: boolean) => void;
  } = $props();
  let active = $state(0);
  let listbox = $state<HTMLElement | null>(null);
  let dismissed = $state(false);
  let observedSignature: string | undefined;
  const signature = $derived(
    JSON.stringify([
      query,
      options.map((option) => [option.value, option.label, option.detail ?? ''])
    ])
  );
  const shown = $derived(dismissed ? [] : options.slice(0, 8));

  export function handleKeydown(event: KeyboardEvent): boolean {
    if (!shown.length) return false;
    if (event.key === 'ArrowDown') setActive((active + 1) % shown.length);
    else if (event.key === 'ArrowUp') setActive((active - 1 + shown.length) % shown.length);
    else if (event.key === 'Home') setActive(0);
    else if (event.key === 'End') setActive(shown.length - 1);
    else if (event.key === 'Enter' || event.key === 'Tab') {
      const option = shown[active] ?? shown[0];
      if (option) onSelect(option);
    } else if (event.key === 'Escape') {
      dismissed = true;
    } else return false;
    event.preventDefault();
    return true;
  }

  function setActive(index: number) {
    const next = shown.length ? Math.max(0, Math.min(index, shown.length - 1)) : 0;
    active = next;
    onActiveIndexChange?.(next);
    if (shown.length) void revealActiveOption(next);
  }

  async function revealActiveOption(index: number) {
    await tick();
    const option = listbox?.querySelector<HTMLElement>(`[data-option-index="${index}"]`);
    option?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  }

  $effect(() => {
    const currentSignature = signature;
    if (currentSignature !== observedSignature) {
      observedSignature = currentSignature;
      dismissed = false;
      setActive(0);
    }
  });

  $effect(() => {
    onOpenChange?.(shown.length > 0);
  });
</script>

{#if shown.length}
  <div
    bind:this={listbox}
    id={listboxId}
    class="composer-autocomplete"
    role="listbox"
    aria-label="Message suggestions"
  >
    {#each shown as option, index (option.value)}
      <button
        type="button"
        id={`${listboxId}-option-${index}`}
        data-option-index={index}
        role="option"
        tabindex="-1"
        aria-selected={index === active}
        class:active={index === active}
        onmouseenter={() => setActive(index)}
        onmousedown={(event) => event.preventDefault()}
        onclick={() => onSelect(option)}
      >
        <strong>{option.label}</strong>{#if option.detail}<small>{option.detail}</small>{/if}
      </button>
    {/each}
  </div>
{/if}
