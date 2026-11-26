<script lang="ts">
  import { shopping } from './shopping.svelte';
  let { domain }: { domain: string } = $props();
  let dialog: HTMLDialogElement;
  $effect(() => {
    if (shopping.state.prompt) dialog?.showModal();
    else dialog?.close();
  });
</script>

<dialog
  bind:this={dialog}
  aria-labelledby="shopping-title"
  oncancel={() => {
    shopping.state.prompt = false;
  }}
>
  <h2 id="shopping-title">Use affiliate links?</h2>
  <p>
    When you open an eligible shopping link, Kaede can add a shared affiliate code. The retailer may
    pay a commission at no extra cost to you. The code identifies this instance and contains no
    personal identifier.
  </p>
  <a
    href={'https://' + domain + '/privacy#shopping-links'}
    target="_blank"
    rel="noopener noreferrer">Read the privacy policy</a
  >
  <div class="choices">
    <button class="yes" disabled={shopping.busy} onclick={() => shopping.choose(true)}
      >Yes, use affiliate links</button
    >
    <button class="no" disabled={shopping.busy} onclick={() => shopping.choose(false)}
      >No, keep original links</button
    >
  </div>
  {#if shopping.error}<p role="alert">{shopping.error}</p>{/if}
</dialog>

<style>
  dialog {
    margin: auto;
    box-sizing: border-box;
    width: min(26rem, calc(100vw - 2rem));
    padding: clamp(1.25rem, 4vw, 2rem);
    border: 1px solid var(--line);
    border-radius: 16px;
    background: var(--surface);
    color: var(--text);
    box-shadow: var(--shadow-lg);
  }
  dialog::backdrop {
    background: rgb(0 0 0 / 55%);
  }
  h2 {
    margin: 0;
  }
  p {
    line-height: 1.55;
    margin: 0.85rem 0;
  }
  a {
    text-decoration: underline;
  }
  .choices {
    display: grid;
    gap: 0.55rem;
    margin-top: 1.25rem;
  }
  button {
    min-height: 44px;
    border: 0;
    border-radius: 8px;
    font-weight: 650;
    color: white;
    cursor: pointer;
  }
  button:disabled {
    opacity: 0.6;
    cursor: wait;
  }
  .yes {
    background: #247447;
  }
  .no {
    background: #525b65;
  }
</style>
