<script lang="ts">
  import { isNativeDesktop, setNativeInstance, storedNativeInstance } from '$lib/platform/native';

  let {
    disabled = false,
    onready,
    suggestedInstance = null
  }: {
    disabled?: boolean;
    onready?: () => void;
    suggestedInstance?: string | null;
  } = $props();
  let instance = $state(storedNativeInstance());
  let error = $state('');

  export async function apply(): Promise<boolean> {
    if (!isNativeDesktop()) return true;
    error = '';
    try {
      instance = await setNativeInstance(instance);
      onready?.();
      return true;
    } catch {
      error = 'Enter a valid Kaede server domain, such as chat.example.com.';
      return false;
    }
  }

  async function useSuggestedInstance() {
    if (!suggestedInstance || disabled) return;
    instance = suggestedInstance;
    await apply();
  }
</script>

{#if isNativeDesktop()}
  <label class="native-instance-field">
    Your Kaede server
    <input
      bind:value={instance}
      placeholder="chat.example.com"
      autocomplete="url"
      inputmode="url"
      spellcheck="false"
      aria-describedby="home-instance-help"
      required
      {disabled}
      onblur={() => void apply()}
    />
  </label>
  <p class="field-note" id="home-instance-help">
    This is the server where you created your account—also called your home instance. For
    <strong>@alex@chat.example.com</strong>, enter <strong>chat.example.com</strong>.
    {#if suggestedInstance}
      Don’t have a server yet?
      <button
        class="native-instance-suggestion"
        type="button"
        {disabled}
        onclick={() => void useSuggestedInstance()}>Use {suggestedInstance}</button
      >.
    {/if}
  </p>
  {#if error}<p class="form-error" role="alert">{error}</p>{/if}
{/if}
