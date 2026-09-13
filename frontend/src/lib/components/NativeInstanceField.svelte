<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { userErrorMessage } from '$lib/api/client';
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
    } catch (caught) {
      error = userErrorMessage(
        caught,
        $t('ui_could_not_connect_to_that_kaede_server_check__0e3f69e1')
      );
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
    {$t('ui_your_kaede_server_f45cfa9f')}
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
    {$t('ui_this_is_the_server_where_you_created_your_acc_4557e0bb')}
    <strong>@alex@chat.example.com</strong>{$t('ui_enter_df392f7d')}
    <strong>chat.example.com</strong>.
    {#if suggestedInstance}{$t('ui_don_t_have_a_server_yet_7fd67bfd')}
      <button
        class="native-instance-suggestion"
        type="button"
        {disabled}
        onclick={() => void useSuggestedInstance()}
        >{$t('ui_use_value0_89497461', { value0: String(suggestedInstance) })}</button
      >.
    {/if}
  </p>
  {#if error}<p class="form-error" role="alert">{error}</p>{/if}
{/if}
