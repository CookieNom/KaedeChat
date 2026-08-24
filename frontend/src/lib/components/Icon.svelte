<script lang="ts">
  export type IconName =
    | 'arrow-left'
    | 'bell'
    | 'check'
    | 'chevron-down'
    | 'chevron-right'
    | 'clock'
    | 'copy'
    | 'edit'
    | 'forum'
    | 'globe'
    | 'hash'
    | 'headphones-off'
    | 'home'
    | 'image'
    | 'key'
    | 'lock'
    | 'logout'
    | 'mail'
    | 'message'
    | 'microphone'
    | 'microphone-off'
    | 'more'
    | 'palette'
    | 'phone-off'
    | 'plus'
    | 'search'
    | 'server'
    | 'settings'
    | 'shield'
    | 'screen'
    | 'sparkles'
    | 'trash'
    | 'threads'
    | 'user'
    | 'users'
    | 'video'
    | 'video-off'
    | 'volume';

  let {
    name,
    size = 20,
    strokeWidth = 1.8
  }: { name: IconName; size?: number; strokeWidth?: number } = $props();
</script>

<svg
  class="ui-icon"
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width={strokeWidth}
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"
>
  {#if name === 'arrow-left'}
    <path d="m15 18-6-6 6-6M9 12h11" />
  {:else if name === 'bell'}
    <path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" />
  {:else if name === 'check'}
    <path d="m5 12 4 4L19 6" />
  {:else if name === 'chevron-down'}
    <path d="m6 9 6 6 6-6" />
  {:else if name === 'chevron-right'}
    <path d="m9 18 6-6-6-6" />
  {:else if name === 'clock'}
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3 2" />
  {:else if name === 'copy'}
    <rect x="8" y="8" width="12" height="12" rx="2" />
    <path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
  {:else if name === 'edit'}
    <path d="M12 20h9" />
    <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" />
  {:else if name === 'forum'}
    <path d="M7 17H5l-3 3V7a3 3 0 0 1 3-3h8a3 3 0 0 1 3 3v7a3 3 0 0 1-3 3Z" />
    <path d="M8 4V3h11a3 3 0 0 1 3 3v11l-3-3h-3" />
  {:else if name === 'globe'}
    <circle cx="12" cy="12" r="9" />
    <path
      d="M3 12h18M12 3c2.5 2.5 3.8 5.5 3.8 9S14.5 18.5 12 21c-2.5-2.5-3.8-5.5-3.8-9S9.5 5.5 12 3Z"
    />
  {:else if name === 'hash'}
    <path d="M5 9h14M4 15h14M10 3 8 21M16 3l-2 18" />
  {:else if name === 'headphones-off'}
    <path
      d="m3 3 18 18M5.2 8.2A8 8 0 0 1 18.8 7M4 12v5a2 2 0 0 0 2 2h2v-7H4Zm16 2.5V12h-4v3.5M12 4a8 8 0 0 1 8 8v5a2 2 0 0 1-2 2h-1"
    />
  {:else if name === 'home'}
    <path d="m3 11 9-8 9 8v9a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z" />
  {:else if name === 'image'}
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <circle cx="8.5" cy="9" r="1.5" />
    <path d="m21 15-5-5L5 20" />
  {:else if name === 'key'}
    <circle cx="8" cy="15" r="4" />
    <path d="m11 12 9-9M15 8l3 3M17 6l2 2" />
  {:else if name === 'lock'}
    <rect x="4" y="10" width="16" height="11" rx="2" />
    <path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3" />
  {:else if name === 'logout'}
    <path d="M10 17l5-5-5-5M15 12H3M14 3h5a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-5" />
  {:else if name === 'mail'}
    <rect x="3" y="5" width="18" height="14" rx="2" />
    <path d="m3 7 9 6 9-6" />
  {:else if name === 'message'}
    <path d="M21 15a4 4 0 0 1-4 4H8l-5 2V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4Z" />
  {:else if name === 'microphone'}
    <rect x="9" y="3" width="6" height="12" rx="3" />
    <path d="M5 11a7 7 0 0 0 14 0M12 18v3M8 21h8" />
  {:else if name === 'microphone-off'}
    <path
      d="m4 4 16 16M9 9v2a3 3 0 0 0 4.6 2.5M15 9V6a3 3 0 0 0-5.8-1M5 11a7 7 0 0 0 11.7 5.2M19 11a7 7 0 0 1-.5 2.6M12 18v3M8 21h8"
    />
  {:else if name === 'more'}
    <circle cx="5" cy="12" r="1.2" fill="currentColor" stroke="none" />
    <circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none" />
    <circle cx="19" cy="12" r="1.2" fill="currentColor" stroke="none" />
  {:else if name === 'palette'}
    <path
      d="M12 3a9 9 0 0 0 0 18h1.5a1.5 1.5 0 0 0 0-3H12a2 2 0 0 1 0-4h4a5 5 0 0 0 5-5c0-3.3-3.7-6-9-6Z"
    />
    <circle cx="7.5" cy="10.5" r=".8" fill="currentColor" stroke="none" />
    <circle cx="10" cy="7" r=".8" fill="currentColor" stroke="none" />
    <circle cx="14" cy="7" r=".8" fill="currentColor" stroke="none" />
    <circle cx="17" cy="10" r=".8" fill="currentColor" stroke="none" />
  {:else if name === 'phone-off'}
    <path
      d="M5.2 10.2a15.5 15.5 0 0 1 13.6 0l1.3.7a2 2 0 0 1 .8 2.6l-1.2 2.2a2 2 0 0 1-2.5.9l-2.4-1.1a2 2 0 0 1-1.1-1.8v-1.2a9 9 0 0 0-3.4 0v1.2a2 2 0 0 1-1.1 1.8l-2.4 1.1a2 2 0 0 1-2.5-.9l-1.2-2.2a2 2 0 0 1 .8-2.6Z"
    />
  {:else if name === 'plus'}
    <path d="M12 5v14M5 12h14" />
  {:else if name === 'search'}
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-4-4" />
  {:else if name === 'server'}
    <rect x="4" y="4" width="16" height="6" rx="2" />
    <rect x="4" y="14" width="16" height="6" rx="2" />
    <path d="M8 7h.01M8 17h.01M12 7h5M12 17h5" />
  {:else if name === 'settings'}
    <circle cx="12" cy="12" r="3" />
    <path
      d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.6v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"
    />
  {:else if name === 'shield'}
    <path d="M12 3 4 6v5c0 5.2 3.4 8.4 8 10 4.6-1.6 8-4.8 8-10V6Z" />
    <path d="m9 12 2 2 4-4" />
  {:else if name === 'screen'}
    <rect x="3" y="4" width="18" height="14" rx="2" />
    <path d="M8 21h8M12 18v3M12 14V8M9 11l3-3 3 3" />
  {:else if name === 'sparkles'}
    <path
      d="m12 3 1.4 4.1L17.5 8.5l-4.1 1.4L12 14l-1.4-4.1-4.1-1.4 4.1-1.4ZM19 15l.7 2.3L22 18l-2.3.7L19 21l-.7-2.3L16 18l2.3-.7Z"
    />
  {:else if name === 'trash'}
    <path d="M4 7h16M9 7V4h6v3M18 7l-1 14H7L6 7M10 11v6M14 11v6" />
  {:else if name === 'threads'}
    <path d="M7 3v12a4 4 0 0 0 4 4h8M3 7h8M15 15l4 4-4 4" />
  {:else if name === 'user'}
    <circle cx="12" cy="8" r="4" />
    <path d="M4 21a8 8 0 0 1 16 0" />
  {:else if name === 'users'}
    <circle cx="9" cy="7" r="4" />
    <path d="M2 21v-2a6 6 0 0 1 6-6h2a6 6 0 0 1 6 6v2M16 3.5a4 4 0 0 1 0 7M18 14a5 5 0 0 1 4 5v2" />
  {:else if name === 'video'}
    <rect x="3" y="6" width="13" height="12" rx="2" />
    <path d="m16 10 5-3v10l-5-3Z" />
  {:else if name === 'video-off'}
    <path
      d="m3 3 18 18M10.5 6H5a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2v-1.5M16 9.5 21 7v10l-2.2-1.1"
    />
  {:else if name === 'volume'}
    <path d="M11 5 6 9H3v6h3l5 4ZM15 9a4 4 0 0 1 0 6M18 6a8 8 0 0 1 0 12" />
  {/if}
</svg>
