<script lang="ts">
  let {
    label,
    value = 1,
    onChange
  }: {
    label: string;
    value?: number;
    onChange: (value: number) => Promise<void>;
  } = $props();
  let error = $state('');
  let level = $derived(value);
  async function change(event: Event) {
    level = Number((event.currentTarget as HTMLInputElement).value);
    error = '';
    try {
      await onChange(level);
    } catch {
      error = 'Could not change volume. Try again.';
    }
  }
</script>

<label class="listening-volume">
  <span>{label}<output>{Math.round(level * 100)}%</output></span>
  <input
    type="range"
    min="0"
    max="1"
    step="0.01"
    value={level}
    oninput={change}
    style={`--volume-fill: ${level * 100}%`}
    aria-label={label}
    aria-valuetext={`${Math.round(level * 100)}%`}
  />
</label>
{#if error}<small role="alert">{error}</small>{/if}

<style>
  .listening-volume {
    display: grid;
    gap: 4px;
    min-width: 120px;
    width: 100%;
    padding: 8px 0;
    box-sizing: border-box;
    color: var(--text);
    background: var(--surface-raised);
    border-radius: 8px;
  }
  span {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    font-size: 12px;
  }
  output {
    color: var(--text-muted);
    font-variant-numeric: tabular-nums;
  }
  input {
    width: 100%;
    margin: 0;
    height: 28px;
    min-height: 28px;
    padding: 0;
    border: 0;
    border-radius: 0;
    box-shadow: none;
    appearance: none;
    background: transparent;
    cursor: pointer;
  }
  input::-webkit-slider-runnable-track {
    height: 5px;
    border-radius: 8px;
    background: linear-gradient(
      to right,
      var(--accent) 0 var(--volume-fill),
      var(--line) var(--volume-fill) 100%
    );
  }
  input::-moz-range-track {
    height: 5px;
    border-radius: 8px;
    background: var(--line);
  }
  input::-moz-range-progress {
    height: 5px;
    border-radius: 8px;
    background: var(--accent);
  }
  input::-webkit-slider-thumb {
    appearance: none;
    width: 12px;
    height: 18px;
    margin-top: -6.5px;
    border-radius: 4px;
    background: var(--text);
    box-shadow: 0 1px 4px #0004;
  }
  input::-moz-range-thumb {
    width: 12px;
    height: 18px;
    border: 0;
    border-radius: 4px;
    background: var(--text);
  }
  input:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 3px;
    border-radius: 5px;
  }
  small {
    color: var(--danger);
  }
</style>
