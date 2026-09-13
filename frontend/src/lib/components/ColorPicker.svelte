<script lang="ts">
  import { untrack } from 'svelte';
  import { t } from '$lib/ui/locale';
  import { hexToHsv, hsvToHex } from '$lib/ui/color';

  let {
    value = $bindable('#000000'),
    label,
    caption = '',
    selected = false,
    disabled = false
  }: {
    value?: string;
    label: string;
    caption?: string;
    selected?: boolean;
    disabled?: boolean;
  } = $props();
  const id = $props.id();
  let trigger: HTMLButtonElement;
  let panel: HTMLDivElement;
  let area: HTMLDivElement;
  let open = $state(false);
  let hue = $state(0);
  let saturation = $state(0);
  let brightness = $state(0);
  let hex = $state('');

  $effect(() => {
    const current = value;
    untrack(() => {
      // Keep the chosen hue when moving through black or gray.
      if (/^#[\da-f]{6}$/i.test(current)) {
        if (hsvToHex(hue, saturation, brightness) !== current.toLowerCase()) {
          const [h, s, v] = hexToHsv(current);
          if (s) hue = h;
          saturation = s;
          brightness = v;
        }
        hex = current;
      }
    });
  });

  $effect(() => {
    if (!open) return;
    window.addEventListener('scroll', position, true);
    return () => window.removeEventListener('scroll', position, true);
  });

  function publish() {
    value = hsvToHex(hue, saturation, brightness);
    hex = value;
  }

  function position() {
    if (!open) return;
    const rect = trigger.getBoundingClientRect();
    const width = panel.offsetWidth;
    const height = panel.offsetHeight;
    panel.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`;
    const top =
      rect.bottom + 8 + height <= window.innerHeight ? rect.bottom + 8 : rect.top - height - 8;
    panel.style.top = `${Math.max(8, Math.min(top, window.innerHeight - height - 8))}px`;
  }

  function toggle() {
    panel.togglePopover();
    open = panel.matches(':popover-open');
    if (open) {
      position();
      area.focus({ preventScroll: true });
    }
  }

  function pick(event: PointerEvent) {
    const element = event.currentTarget as HTMLDivElement;
    if (event.type === 'pointerdown') {
      if (event.button !== 0) return;
      element.setPointerCapture(event.pointerId);
      element.focus({ preventScroll: true });
    } else if (!element.hasPointerCapture(event.pointerId)) return;
    const rect = element.getBoundingClientRect();
    saturation = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    brightness = 1 - Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
    publish();
  }

  function adjust(event: KeyboardEvent) {
    const step = event.shiftKey ? 0.1 : 0.01;
    if (event.key === 'ArrowLeft') saturation -= step;
    else if (event.key === 'ArrowRight') saturation += step;
    else if (event.key === 'ArrowUp') brightness += step;
    else if (event.key === 'ArrowDown') brightness -= step;
    else return;
    event.preventDefault();
    saturation = Math.max(0, Math.min(1, saturation));
    brightness = Math.max(0, Math.min(1, brightness));
    publish();
  }

  function commitHex() {
    const input = hex.trim().replace(/^#/, '');
    if (/^[\da-f]{3}([\da-f]{3})?$/i.test(input)) {
      value =
        '#' + (input.length === 3 ? [...input].map((c) => c + c).join('') : input).toLowerCase();
    }
    hex = value;
  }
</script>

<svelte:window onresize={position} />

<button
  bind:this={trigger}
  type="button"
  class="color-trigger"
  class:with-caption={!!caption}
  class:selected
  aria-label={label}
  aria-expanded={open}
  aria-controls={id}
  aria-haspopup="dialog"
  {disabled}
  onclick={toggle}
>
  <span class="preview" style:background={value}>
    {#if caption}<svg viewBox="0 0 24 24" aria-hidden="true"
        ><path d="m16 3 5 5-12 12-6 1 1-6Z" fill="currentColor" /></svg
      >{/if}
  </span>
  {#if caption}<small>{caption}</small>{/if}
</button>
<div
  bind:this={panel}
  {id}
  class="color-popover"
  popover="auto"
  role="dialog"
  tabindex="-1"
  aria-label={label}
  ontoggle={(event) => {
    open = event.newState === 'open';
  }}
  onkeydown={(event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      panel.hidePopover();
      trigger.focus();
    }
  }}
>
  <div
    bind:this={area}
    class="color-area"
    style:background-color={`hsl(${hue} 100% 50%)`}
    role="slider"
    tabindex="0"
    aria-label={$t('ui_color_saturation_brightness')}
    aria-valuemin={0}
    aria-valuemax={100}
    aria-valuenow={Math.round(saturation * 100)}
    aria-valuetext={$t('ui_color_values', {
      saturation: Math.round(saturation * 100),
      brightness: Math.round(brightness * 100)
    })}
    onpointerdown={pick}
    onpointermove={pick}
    onkeydown={adjust}
  >
    <span
      class="color-cursor"
      style:left={`${saturation * 100}%`}
      style:top={`${(1 - brightness) * 100}%`}
    ></span>
  </div>
  <input
    class="hue"
    type="range"
    min="0"
    max="360"
    step="1"
    bind:value={hue}
    aria-label={$t('ui_color_hue')}
    oninput={(event) => {
      hue = Number(event.currentTarget.value);
      publish();
    }}
  />
  <label class="hex-field">
    <span>{$t('ui_hex_69493e6f')}</span>
    <input
      type="text"
      bind:value={hex}
      maxlength="7"
      spellcheck="false"
      autocapitalize="off"
      onblur={commitHex}
      onkeydown={(event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          commitHex();
        }
      }}
    />
    <span class="hex-preview" style:background={value}></span>
  </label>
</div>

<style>
  .color-trigger {
    display: grid;
    place-items: center;
    width: 38px;
    height: 38px;
    flex: 0 0 auto;
    padding: 3px;
    border: 1px solid var(--line);
    border-radius: 9px;
    color: var(--text);
    background: var(--surface-subtle);
    cursor: pointer;
  }
  .color-trigger.with-caption {
    width: 100%;
    height: 70px;
    border-radius: 12px;
  }
  .color-trigger:disabled {
    cursor: default;
    opacity: 0.58;
  }
  .preview {
    display: grid;
    place-items: center;
    width: 100%;
    height: 100%;
    border-radius: 6px;
    box-shadow: inset 0 0 0 1px rgb(255 255 255 / 20%);
  }
  .with-caption .preview {
    width: 34px;
    height: 34px;
    border-radius: 9px;
  }
  .preview svg {
    width: 18px;
    height: 18px;
    color: white;
    filter: drop-shadow(0 1px 2px black);
  }
  small {
    font-size: 0.6rem;
    font-weight: 700;
  }
  .selected,
  .color-trigger:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }
  .color-popover {
    position: fixed;
    inset: auto;
    margin: 0;
    box-sizing: border-box;
    width: min(300px, calc(100vw - 16px));
    max-height: calc(100dvh - 16px);
    overflow: auto;
    padding: 18px;
    border: 1px solid var(--line);
    border-radius: 12px;
    color: var(--text);
    background: var(--surface-raised);
    box-shadow: 0 12px 36px rgb(0 0 0 / 35%);
  }
  .color-area {
    position: relative;
    height: 180px;
    border-radius: 7px;
    background-image:
      linear-gradient(to top, #000, transparent), linear-gradient(to right, #fff, transparent);
    touch-action: none;
    cursor: crosshair;
  }
  .color-area:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 4px;
  }
  .color-cursor {
    position: absolute;
    width: 12px;
    height: 12px;
    box-sizing: border-box;
    border: 2px solid white;
    border-radius: 50%;
    box-shadow: 0 0 2px 1px #0008;
    transform: translate(-50%, -50%);
    pointer-events: none;
  }
  .hue {
    display: block;
    appearance: none;
    width: 100%;
    min-height: 0;
    height: 14px;
    margin: 20px 0;
    padding: 0;
    border: 0;
    border-radius: 8px;
    background: linear-gradient(to right, #f00, #ff0, #0f0, #0ff, #00f, #f0f, #f00);
    cursor: pointer;
  }
  .hue::-webkit-slider-thumb {
    appearance: none;
    width: 10px;
    height: 24px;
    border: 2px solid white;
    border-radius: 5px;
    background: white;
    box-shadow: 0 1px 4px #0008;
  }
  .hue::-moz-range-thumb {
    width: 8px;
    height: 22px;
    border: 2px solid white;
    border-radius: 5px;
    background: white;
    box-shadow: 0 1px 4px #0008;
  }
  .hex-field {
    display: flex;
    align-items: center;
    gap: 8px;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0 10px;
    background: var(--surface-subtle);
  }
  .hex-field:focus-within {
    outline: 2px solid var(--accent);
  }
  .hex-field > span:first-child {
    font-size: 0.7rem;
    color: var(--text-muted);
  }
  .hex-field input {
    width: 100%;
    min-width: 0;
    height: 40px;
    padding: 0;
    border: 0;
    outline: none;
    color: var(--text);
    background: transparent;
    box-shadow: none;
    font-family: var(--font-mono);
    text-transform: uppercase;
  }
  .hex-preview {
    flex: 0 0 22px;
    height: 22px;
    border: 1px solid var(--line);
    border-radius: 5px;
  }
</style>
