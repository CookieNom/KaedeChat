<script lang="ts">
  let {
    message,
    onDismiss,
    duration = 3_500
  }: {
    message: string;
    onDismiss: () => void;
    duration?: number;
  } = $props();

  $effect(() => {
    if (!message) return;
    const timer = window.setTimeout(onDismiss, duration);
    return () => window.clearTimeout(timer);
  });
</script>

{#if message}
  <div class="settings-toast" role="status" aria-live="polite" aria-atomic="true">
    <span class="settings-toast-icon" aria-hidden="true">
      <svg viewBox="0 0 20 20">
        <path d="m5 10.25 3.1 3.1L15.5 6" />
      </svg>
    </span>
    <span>{message}</span>
    <button type="button" aria-label="Dismiss notification" onclick={onDismiss}>×</button>
  </div>
{/if}

<style>
  .settings-toast {
    position: fixed;
    z-index: 1200;
    right: 24px;
    bottom: 24px;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 12px;
    width: min(390px, calc(100vw - 32px));
    min-height: 58px;
    padding: 10px 10px 10px 13px;
    color: var(--text);
    font-size: 0.92rem;
    font-weight: 680;
    background: color-mix(in srgb, var(--surface-raised) 96%, var(--pine-soft));
    border: 1px solid color-mix(in srgb, var(--pine) 55%, var(--line));
    border-radius: 14px;
    box-shadow: var(--shadow-lg);
    animation: toast-enter 160ms ease-out;
  }

  .settings-toast-icon {
    display: grid;
    width: 30px;
    height: 30px;
    color: var(--on-pine);
    background: var(--pine);
    border-radius: 50%;
    place-items: center;
  }

  .settings-toast-icon svg {
    width: 18px;
    height: 18px;
    fill: none;
    stroke: currentColor;
    stroke-width: 2.2;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  button {
    display: grid;
    width: 34px;
    height: 34px;
    padding: 0;
    color: var(--text-muted);
    font: inherit;
    font-size: 1.25rem;
    line-height: 1;
    background: transparent;
    border: 0;
    border-radius: 9px;
    cursor: pointer;
    place-items: center;
  }

  button:hover {
    color: var(--text);
    background: var(--surface-hover);
  }

  button:focus-visible {
    outline: 2px solid var(--focus);
    outline-offset: 1px;
  }

  @keyframes toast-enter {
    from {
      opacity: 0;
      transform: translateY(8px) scale(0.98);
    }
  }

  @media (max-width: 640px) {
    .settings-toast {
      right: 16px;
      bottom: 16px;
      left: 16px;
      width: auto;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .settings-toast {
      animation: none;
    }
  }
</style>
