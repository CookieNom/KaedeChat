<script lang="ts">
  import type { UserSummary } from '$lib/chat/types';
  import { chatEntities } from '$lib/stores/entities.svelte';
  import { t } from '$lib/ui/locale';

  let { user }: { user: UserSummary } = $props();
  const status = $derived(chatEntities.presenceFor(user));
  const label = $derived(
    status === 'online'
      ? $t('ui_online_0d21bd52')
      : status === 'idle'
        ? $t('ui_idle_ab0171ca')
        : status === 'dnd'
          ? $t('ui_do_not_disturb_fe39f3cd')
          : $t('presence_offline')
  );
</script>

<span class={`presence-dot presence-${status}`} role="img" aria-label={label} title={label}></span>

<style>
  span {
    position: absolute;
    right: -2px;
    bottom: -2px;
    width: 12px;
    height: 12px;
  }
</style>
