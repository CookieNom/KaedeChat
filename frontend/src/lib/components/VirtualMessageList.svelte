<script lang="ts" generics="T extends { key: string }">
  import { bottomVirtualWindow } from '$lib/chat/virtualization';
  import { onMount, tick, untrack, type Snippet } from 'svelte';

  let {
    items,
    renderItem,
    empty,
    hasEarlier = false,
    loadingEarlier = false,
    hasLater = false,
    loadingLater = false,
    onLoadEarlier,
    onLoadLater,
    onBottomChange,
    targetKey,
    label = 'Messages'
  }: {
    items: T[];
    renderItem: Snippet<[item: T, index: number]>;
    empty?: Snippet;
    hasEarlier?: boolean;
    loadingEarlier?: boolean;
    hasLater?: boolean;
    loadingLater?: boolean;
    onLoadEarlier?: () => Promise<void> | void;
    onLoadLater?: () => Promise<void> | void;
    onBottomChange?: (atBottom: boolean) => void;
    targetKey?: string | null;
    label?: string;
  } = $props();

  let viewport = $state<HTMLDivElement | null>(null);
  let atBottom = $state(false);
  let unseen = $state(0);
  let previousLastKey = '';
  let initialized = false;
  let windowStart = $state(0);
  let windowEnd = $state(100);
  let estimatedHeight = $state(76);
  let contentElement = $state<HTMLDivElement | null>(null);
  let topSpacerElement = $state<HTMLDivElement | null>(null);
  let bottomSpacerElement = $state<HTMLDivElement | null>(null);
  let initialPinTimers: number[] = [];
  let initialPinActive = false;
  let resizeFrame = 0;
  const overscan = 24;
  const visibleItems = $derived(items.slice(windowStart, windowEnd));
  const topSpacer = $derived(windowStart * estimatedHeight);
  const bottomSpacer = $derived(Math.max(0, items.length - windowEnd) * estimatedHeight);

  function updateWindow(itemCount = items.length) {
    if (!viewport) return;
    const visible = Math.ceil(viewport.clientHeight / estimatedHeight);
    const start = Math.max(0, Math.floor(viewport.scrollTop / estimatedHeight) - overscan);
    windowStart = start;
    windowEnd = Math.min(itemCount, start + visible + overscan * 2);
  }

  function messageActionButtons(): HTMLButtonElement[] {
    return Array.from(
      viewport?.querySelectorAll<HTMLButtonElement>(
        'button[data-message-action]:not([disabled])'
      ) ?? []
    );
  }

  function makeMessageActionCurrent(target: HTMLButtonElement, focus = false) {
    const buttons = messageActionButtons();
    if (!buttons.includes(target)) return;
    for (const button of buttons) button.tabIndex = button === target ? 0 : -1;
    if (focus) {
      target.focus({ preventScroll: true });
      target.scrollIntoView({ block: 'nearest' });
    }
  }

  function syncMessageActionTabStop() {
    const buttons = messageActionButtons();
    if (!buttons.length) return;
    const focused =
      document.activeElement instanceof HTMLButtonElement &&
      buttons.includes(document.activeElement)
        ? document.activeElement
        : null;
    const current = focused ?? buttons.find((button) => button.tabIndex === 0) ?? buttons.at(-1);
    if (current) makeMessageActionCurrent(current);
  }

  function messageActionFocused(event: FocusEvent) {
    const target =
      event.target instanceof Element
        ? event.target.closest<HTMLButtonElement>('button[data-message-action]')
        : null;
    if (target) makeMessageActionCurrent(target);
  }

  function viewportKeydown(event: KeyboardEvent) {
    if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)) cancelInitialBottomPin();
    const target =
      event.target instanceof Element
        ? event.target.closest<HTMLButtonElement>('button[data-message-action]')
        : null;
    if (!target) return;
    const buttons = messageActionButtons();
    const current = buttons.indexOf(target);
    if (current < 0) return;
    let next = current;
    if (event.key === 'ArrowDown') next = Math.min(buttons.length - 1, current + 1);
    else if (event.key === 'ArrowUp') next = Math.max(0, current - 1);
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = buttons.length - 1;
    else return;
    event.preventDefault();
    makeMessageActionCurrent(buttons[next], true);
  }

  function prefersReducedMotion(): boolean {
    return (
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    );
  }

  function updateBottom(next: boolean) {
    if (next === atBottom) return;
    atBottom = next;
    onBottomChange?.(next);
  }

  function cancelInitialBottomPin() {
    initialPinActive = false;
    for (const timer of initialPinTimers) window.clearTimeout(timer);
    initialPinTimers = [];
  }

  function pinViewportToBottom() {
    if (!viewport || !items.length) return;
    viewport.scrollTop = viewport.scrollHeight;
    updateWindow();
    updateBottom(true);
    unseen = 0;
  }

  function scheduleInitialBottomPin() {
    cancelInitialBottomPin();
    initialPinActive = true;
    const delays = [0, 50, 150, 300, 600, 1000];
    initialPinTimers = delays.map((delay, index) =>
      window.setTimeout(() => {
        if (!initialPinActive) return;
        pinViewportToBottom();
        if (index === delays.length - 1) {
          initialPinActive = false;
          initialPinTimers = [];
        }
      }, delay)
    );
  }

  function viewportScrolled() {
    if (!viewport) return;
    const nextAtBottom = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight <= 24;
    updateBottom(nextAtBottom);
    if (nextAtBottom) unseen = 0;
    if (initialized && viewport.scrollTop <= 24 && hasEarlier && !loadingEarlier) {
      void loadEarlierAnchored();
    }
    if (initialized && nextAtBottom && hasLater && !loadingLater && onLoadLater) {
      void onLoadLater();
    }
    updateWindow();
  }

  async function loadEarlierAnchored() {
    if (!onLoadEarlier || !viewport || loadingEarlier || !items.length) return;
    const previousHeight = viewport.scrollHeight;
    const previousTop = viewport.scrollTop;
    await onLoadEarlier();
    await tick();
    if (viewport) viewport.scrollTop = previousTop + viewport.scrollHeight - previousHeight;
  }

  async function scrollToBottom(): Promise<boolean> {
    if (!viewport || !items.length) return false;
    const wasInitialized = initialized;
    if (!wasInitialized) {
      const initialWindow = bottomVirtualWindow(
        items.length,
        viewport.clientHeight,
        estimatedHeight,
        overscan
      );
      windowStart = initialWindow.start;
      windowEnd = initialWindow.end;
      await tick();
      if (!viewport) return false;
    }
    viewport.scrollTo({
      top: viewport.scrollHeight,
      behavior: wasInitialized && !prefersReducedMotion() ? 'smooth' : 'auto'
    });
    if (!wasInitialized) {
      await tick();
      if (!viewport) return false;
      viewport.scrollTop = viewport.scrollHeight;
      updateWindow();
    }
    updateBottom(true);
    initialized = true;
    unseen = 0;
    if (!wasInitialized) scheduleInitialBottomPin();
    return true;
  }

  async function scrollToTarget(key: string) {
    const index = items.findIndex((item) => item.key === key);
    if (index < 0 || !viewport) return false;
    windowStart = Math.max(0, index - overscan);
    windowEnd = Math.min(items.length, index + overscan);
    await tick();
    viewport.querySelector<HTMLElement>(`[data-virtual-key="${CSS.escape(key)}"]`)?.scrollIntoView({
      block: 'center'
    });
    initialized = true;
    return true;
  }

  onMount(() => {
    if (!viewport) return;
    const currentViewport = viewport;
    const observer = new MutationObserver(syncMessageActionTabStop);
    const resizeObserver = new ResizeObserver(() => {
      if (!initialized || !atBottom || !viewport) return;
      window.cancelAnimationFrame(resizeFrame);
      resizeFrame = window.requestAnimationFrame(pinViewportToBottom);
    });
    observer.observe(currentViewport, { childList: true, subtree: true });
    if (contentElement) resizeObserver.observe(contentElement);
    currentViewport.addEventListener('scroll', viewportScrolled);
    currentViewport.addEventListener('wheel', cancelInitialBottomPin, { passive: true });
    currentViewport.addEventListener('pointerdown', cancelInitialBottomPin, { passive: true });
    currentViewport.addEventListener('focusin', messageActionFocused);
    currentViewport.addEventListener('keydown', viewportKeydown);
    syncMessageActionTabStop();
    updateWindow();
    if (items.length && !initialized) {
      if (targetKey) void tick().then(() => scrollToTarget(targetKey));
      else void tick().then(scrollToBottom);
    }
    return () => {
      observer.disconnect();
      resizeObserver.disconnect();
      window.cancelAnimationFrame(resizeFrame);
      cancelInitialBottomPin();
      currentViewport.removeEventListener('scroll', viewportScrolled);
      currentViewport.removeEventListener('wheel', cancelInitialBottomPin);
      currentViewport.removeEventListener('pointerdown', cancelInitialBottomPin);
      currentViewport.removeEventListener('focusin', messageActionFocused);
      currentViewport.removeEventListener('keydown', viewportKeydown);
    };
  });

  $effect(() => {
    const currentLastKey = items.at(-1)?.key ?? '';
    untrack(() => {
      if (!currentLastKey) {
        previousLastKey = '';
        initialized = false;
        unseen = 0;
        updateBottom(false);
        return;
      }
      if (!previousLastKey) {
        if (targetKey) void tick().then(() => scrollToTarget(targetKey));
        else void tick().then(scrollToBottom);
      } else if (currentLastKey !== previousLastKey) {
        if (atBottom) void tick().then(scrollToBottom);
        else unseen += 1;
      }
      previousLastKey = currentLastKey;
    });
  });

  $effect(() => {
    const itemCount = items.length;
    untrack(() => updateWindow(itemCount));
  });

  $effect(() => {
    if (topSpacerElement) topSpacerElement.style.setProperty('height', `${topSpacer}px`);
    if (bottomSpacerElement) bottomSpacerElement.style.setProperty('height', `${bottomSpacer}px`);
  });
</script>

<div class="virtual-message-shell">
  <span id="message-history-keyboard-help" class="visually-hidden">
    Scroll this region with Page Up and Page Down. When a message actions button is focused, use Up
    and Down Arrow to move between messages.
  </span>
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (the scrollable message region needs a keyboard entry point) -->
  <div
    bind:this={viewport}
    class="virtual-message-viewport"
    role="region"
    tabindex="0"
    aria-label={`${label} history`}
    aria-describedby="message-history-keyboard-help"
    aria-busy={loadingEarlier}
  >
    <div bind:this={contentElement} class="virtual-message-content">
      {#if hasEarlier}
        <button class="history-button" disabled={loadingEarlier} onclick={loadEarlierAnchored}>
          {loadingEarlier ? 'Loading…' : 'Load earlier messages'}
        </button>
      {/if}
      {#if !items.length && empty}{@render empty()}{/if}
      <div bind:this={topSpacerElement} class="virtual-message-spacer"></div>
      {#each visibleItems as item, index (item.key)}
        <div class="virtual-message-item" data-virtual-key={item.key}>
          {@render renderItem(item, windowStart + index)}
        </div>
      {/each}
      <div bind:this={bottomSpacerElement} class="virtual-message-spacer"></div>
      {#if hasLater}
        <button class="history-button" disabled={loadingLater} onclick={onLoadLater}>
          {loadingLater ? 'Loading…' : 'Load newer messages'}
        </button>
      {/if}
    </div>
  </div>
  {#if unseen > 0}
    <button class="new-message-pill" onclick={scrollToBottom} aria-label={`${unseen} new messages`}>
      {unseen} new {unseen === 1 ? 'message' : 'messages'} ↓
    </button>
  {/if}
</div>
