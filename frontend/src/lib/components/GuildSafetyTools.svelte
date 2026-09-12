<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { api, userErrorMessage } from '$lib/api/client';
  import {
    autoModPayload,
    boundedVolume,
    canAccessGuildExpressionSettings,
    canCreateGuildExpression,
    canEditGuildExpression,
    isQualifiedGuildOwner,
    pruneEstimateQuery,
    soundboardEmojiPayload,
    uniqueNonemptyLines,
    type AutoModDraft,
    type AutoModTrigger,
    type SoundboardEmojiDraft
  } from '$lib/chat/guild-admin';
  import { entityKey, entityRef } from '$lib/chat/refs';
  import type { Guild } from '$lib/chat/types';
  import { Permission } from '$lib/generated/permissions';
  import { hasAllPermissions } from '$lib/chat/permissions';
  import { uploadObject, type UploadTicket } from '$lib/media/uploads';
  import { assetUrl } from '$lib/media/assets';
  import { completeScannedMediaResource } from '$lib/media/scanned';
  import { onDestroy, onMount } from 'svelte';

  interface AutoModAction {
    type: 'block_message' | 'send_alert_message' | 'timeout' | 'block_member_interaction';
    metadata: {
      custom_message?: string;
      channel_id?: string;
      duration_seconds?: number;
    };
  }

  interface AutoModRule {
    id: string;
    name: string;
    event_type: 'message_send' | 'member_update';
    trigger_type: AutoModTrigger;
    trigger_metadata: {
      keyword_filter?: string[];
      regex_patterns?: string[];
      presets?: Array<'profanity' | 'sexual_content' | 'slurs'>;
      allow_list?: string[];
      mention_total_limit?: number;
      mention_raid_protection_enabled?: boolean;
    };
    actions: AutoModAction[];
    enabled: boolean;
    exempt_roles: string[];
    exempt_channels: string[];
    version: number;
  }

  interface SoundboardSound {
    id: string;
    origin_domain: string;
    guild_id: string;
    guild_domain: string;
    name: string;
    volume: number;
    emoji_id: string | null;
    emoji_domain: string | null;
    emoji_name: string | null;
    available: boolean;
    duration_ms: number;
    created_by_id: string;
    created_by_domain: string;
    version: string;
  }

  interface SoundboardDraft {
    name: string;
    emojiSelection: string;
    emojiName: string;
    volume: number;
  }

  interface BulkFailure {
    user_id: string;
    code: string;
    message: string;
  }

  interface PruneResult {
    pruned: number | null;
    pruned_user_ids?: string[];
    failed_users?: BulkFailure[];
  }

  interface BulkBanResult {
    banned_users: string[];
    failed_users: string[];
    failed_user_details: BulkFailure[];
  }

  let { guild, currentUserRef }: { guild: Guild; currentUserRef: string } = $props();
  const controller = new AbortController();
  let rules = $state<AutoModRule[]>([]);
  let sounds = $state<SoundboardSound[]>([]);
  let selectedRuleId = $state('');
  let draft = $state<AutoModDraft>(blankRule());
  let automodBusy = $state(false);
  let automodError = $state('');
  let automodNotice = $state('');
  let pruneDays = $state(7);
  let pruneRoles = $state<string[]>([]);
  let pruneEstimate = $state<number | null>(null);
  let pruneBusy = $state(false);
  let pruneError = $state('');
  let pruneResult = $state<PruneResult | null>(null);
  let bulkUsers = $state('');
  let bulkReason = $state('');
  let bulkDeleteSeconds = $state(0);
  let bulkBusy = $state(false);
  let bulkError = $state('');
  let bulkResult = $state<BulkBanResult | null>(null);
  let soundFile = $state<File | null>(null);
  let soundName = $state('');
  let soundEmoji = $state('');
  let soundEmojiSelection = $state('none');
  let soundVolume = $state(1);
  let soundUploadProgress = $state(0);
  let soundBusy = $state(false);
  let soundError = $state('');
  let soundNotice = $state('');
  let soundDrafts = $state<Record<string, SoundboardDraft>>({});
  let playbackChannel = $state('');

  const permissionBits = $derived.by(() => {
    try {
      return BigInt(guild.permissions ?? '0');
    } catch {
      return 0n;
    }
  });
  const isGuildOwner = $derived(isQualifiedGuildOwner(guild, currentUserRef));
  const canManageAutoMod = $derived(
    isGuildOwner || hasAllPermissions(permissionBits, Permission.MANAGE_AUTO_MODERATION)
  );
  const canPrune = $derived(
    isGuildOwner ||
      hasAllPermissions(permissionBits, Permission.MANAGE_GUILD | Permission.KICK_MEMBERS)
  );
  const canBulkBan = $derived(
    isGuildOwner ||
      hasAllPermissions(permissionBits, Permission.MANAGE_GUILD | Permission.BAN_MEMBERS)
  );
  const canCreateSounds = $derived(isGuildOwner || canCreateGuildExpression(permissionBits));
  const canUseSounds = $derived(
    isGuildOwner || hasAllPermissions(permissionBits, Permission.USE_SOUNDBOARD)
  );
  const canAccessSounds = $derived(canAccessGuildExpressionSettings(permissionBits, isGuildOwner));
  const textChannels = $derived(
    (guild.channels ?? []).filter(
      (channel) => (channel.type === 0 || channel.type === 5) && channel.encryption_mode !== 'e2ee'
    )
  );
  const voiceChannels = $derived((guild.channels ?? []).filter((channel) => channel.type === 2));
  const manageableRoles = $derived((guild.roles ?? []).filter((role) => role.id !== guild.id));
  const soundCustomEmojis = $derived(
    (guild.emojis ?? []).filter((emoji) => emoji.available !== false)
  );

  function blankRule(): AutoModDraft {
    return {
      name: '',
      enabled: true,
      triggerType: 'keyword',
      keywords: '',
      regexPatterns: '',
      presets: [],
      allowList: '',
      mentionLimit: 5,
      mentionRaidProtection: false,
      blockMessage: true,
      blockMessageText: '',
      alertMessage: false,
      alertChannelRef: '',
      timeout: false,
      timeoutSeconds: 60,
      blockMemberInteraction: false,
      exemptRoles: [],
      exemptChannels: []
    };
  }

  function selectedValues(event: Event): string[] {
    return Array.from(
      (event.currentTarget as HTMLSelectElement).selectedOptions,
      (item) => item.value
    );
  }

  function action(rule: AutoModRule, type: AutoModAction['type']): AutoModAction | undefined {
    return rule.actions.find((item) => item.type === type);
  }

  function editRule(rule?: AutoModRule) {
    automodError = '';
    automodNotice = '';
    if (!rule) {
      selectedRuleId = '';
      draft = blankRule();
      return;
    }
    const block = action(rule, 'block_message');
    const alert = action(rule, 'send_alert_message');
    const timeout = action(rule, 'timeout');
    selectedRuleId = rule.id;
    draft = {
      name: rule.name,
      enabled: rule.enabled,
      triggerType: rule.trigger_type,
      keywords: (rule.trigger_metadata.keyword_filter ?? []).join('\n'),
      regexPatterns: (rule.trigger_metadata.regex_patterns ?? []).join('\n'),
      presets: [...(rule.trigger_metadata.presets ?? [])],
      allowList: (rule.trigger_metadata.allow_list ?? []).join('\n'),
      mentionLimit: rule.trigger_metadata.mention_total_limit ?? 5,
      mentionRaidProtection: rule.trigger_metadata.mention_raid_protection_enabled ?? false,
      blockMessage: Boolean(block),
      blockMessageText: block?.metadata.custom_message ?? '',
      alertMessage: Boolean(alert),
      alertChannelRef: alert?.metadata.channel_id ?? '',
      timeout: Boolean(timeout),
      timeoutSeconds: timeout?.metadata.duration_seconds ?? 60,
      blockMemberInteraction: Boolean(action(rule, 'block_member_interaction')),
      exemptRoles: [...rule.exempt_roles],
      exemptChannels: [...rule.exempt_channels]
    };
  }

  function togglePreset(value: 'profanity' | 'sexual_content' | 'slurs') {
    draft.presets = draft.presets.includes(value)
      ? draft.presets.filter((item) => item !== value)
      : [...draft.presets, value];
  }

  function normalizeDraftForTrigger(trigger: AutoModTrigger) {
    draft.triggerType = trigger;
    if (trigger === 'member_profile') {
      draft.blockMessage = false;
      draft.timeout = false;
      draft.blockMemberInteraction = true;
    } else {
      draft.blockMemberInteraction = false;
    }
    if (trigger !== 'keyword' && trigger !== 'mention_spam') draft.timeout = false;
  }

  async function loadAutoMod() {
    if (!canManageAutoMod) return;
    try {
      rules = await api<AutoModRule[]>(
        `/guilds/${encodeURIComponent(entityRef(guild))}/auto-moderation/rules`,
        { signal: controller.signal }
      );
    } catch (caught) {
      if (!controller.signal.aborted)
        automodError = userErrorMessage(caught, $t('ui_could_not_load_automod_rules_8918d442'));
    }
  }

  async function saveRule(event: SubmitEvent) {
    event.preventDefault();
    if (automodBusy) return;
    const payload = autoModPayload(draft);
    const actions = payload.actions as unknown[];
    if (!draft.name.trim()) {
      automodError = $t('ui_give_this_automod_rule_a_name_183d4bf3');
      return;
    }
    if (!actions.length) {
      automodError = $t('ui_choose_at_least_one_action_for_this_automod_r_bd13c1f8');
      return;
    }
    if (
      (draft.triggerType === 'keyword' || draft.triggerType === 'member_profile') &&
      !uniqueNonemptyLines(draft.keywords).length &&
      !uniqueNonemptyLines(draft.regexPatterns).length
    ) {
      automodError = $t('ui_add_at_least_one_keyword_wildcard_pattern_or__a79eaca3');
      return;
    }
    if (draft.triggerType === 'keyword_preset' && !draft.presets.length) {
      automodError = $t('ui_choose_at_least_one_built_in_keyword_filter_69d19a8a');
      return;
    }
    if (draft.alertMessage && !draft.alertChannelRef) {
      automodError = $t('ui_choose_a_plaintext_text_channel_for_automod_a_a61ef142');
      return;
    }
    automodBusy = true;
    automodError = '';
    automodNotice = '';
    try {
      const base = `/guilds/${encodeURIComponent(entityRef(guild))}/auto-moderation/rules`;
      const updating = Boolean(selectedRuleId);
      const saved = await api<AutoModRule>(
        selectedRuleId ? `${base}/${encodeURIComponent(selectedRuleId)}` : base,
        {
          method: selectedRuleId ? 'PATCH' : 'POST',
          body: JSON.stringify(payload),
          signal: controller.signal
        }
      );
      rules = [...rules.filter((item) => item.id !== saved.id), saved].sort((left, right) =>
        left.name.localeCompare(right.name)
      );
      editRule(saved);
      automodNotice = updating ? 'AutoMod rule saved.' : 'AutoMod rule created.';
    } catch (caught) {
      if (!controller.signal.aborted)
        automodError = userErrorMessage(caught, $t('ui_could_not_save_the_automod_rule_d2fbd7f2'));
    } finally {
      automodBusy = false;
    }
  }

  async function deleteRule() {
    const rule = rules.find((item) => item.id === selectedRuleId);
    if (!rule || automodBusy) return;
    if (!confirm(`Delete the AutoMod rule “${rule.name}”? This cannot be undone.`)) return;
    automodBusy = true;
    automodError = '';
    try {
      await api(
        `/guilds/${encodeURIComponent(entityRef(guild))}/auto-moderation/rules/${encodeURIComponent(rule.id)}`,
        { method: 'DELETE', signal: controller.signal }
      );
      rules = rules.filter((item) => item.id !== rule.id);
      editRule();
      automodNotice = $t('ui_automod_rule_deleted_e2839077');
    } catch (caught) {
      if (!controller.signal.aborted)
        automodError = userErrorMessage(
          caught,
          $t('ui_could_not_delete_the_automod_rule_b9b6fc7a')
        );
    } finally {
      automodBusy = false;
    }
  }

  async function estimatePrune() {
    if (pruneBusy) return;
    pruneBusy = true;
    pruneError = '';
    pruneResult = null;
    try {
      const query = pruneEstimateQuery(pruneDays, pruneRoles);
      const result = await api<{ pruned: number }>(
        `/guilds/${encodeURIComponent(entityRef(guild))}/prune/estimate?${query}`,
        { signal: controller.signal }
      );
      pruneEstimate = result.pruned;
    } catch (caught) {
      if (!controller.signal.aborted)
        pruneError = userErrorMessage(
          caught,
          $t('ui_could_not_estimate_inactive_members_0a6e509c')
        );
    } finally {
      pruneBusy = false;
    }
  }

  async function executePrune() {
    if (pruneBusy) return;
    const count = pruneEstimate ?? 0;
    if (
      !confirm(
        `Prune ${count} currently eligible member${count === 1 ? '' : 's'} inactive for at least ${pruneDays} days? Active members and bots are never included.`
      )
    )
      return;
    pruneBusy = true;
    pruneError = '';
    try {
      pruneResult = await api<PruneResult>(
        `/guilds/${encodeURIComponent(entityRef(guild))}/prune`,
        {
          method: 'POST',
          body: JSON.stringify({ days: pruneDays, include_roles: pruneRoles }),
          signal: controller.signal
        }
      );
      pruneEstimate = null;
    } catch (caught) {
      if (!controller.signal.aborted)
        pruneError = userErrorMessage(caught, $t('ui_could_not_prune_inactive_members_da18a6c8'));
    } finally {
      pruneBusy = false;
    }
  }

  async function executeBulkBan(event: SubmitEvent) {
    event.preventDefault();
    if (bulkBusy) return;
    const userIds = uniqueNonemptyLines(bulkUsers);
    if (!userIds.length) {
      bulkError = $t('ui_enter_at_least_one_user_reference_one_per_lin_33c8a26b');
      return;
    }
    if (userIds.length > 200) {
      bulkError = $t('ui_bulk_bans_can_include_at_most_200_users_at_a__4e32ef30');
      return;
    }
    if (
      !confirm(
        `Ban ${userIds.length} user${userIds.length === 1 ? '' : 's'}? Each user is checked separately against role hierarchy and existing bans.`
      )
    )
      return;
    bulkBusy = true;
    bulkError = '';
    bulkResult = null;
    try {
      bulkResult = await api<BulkBanResult>(
        `/guilds/${encodeURIComponent(entityRef(guild))}/bulk-bans`,
        {
          method: 'POST',
          body: JSON.stringify({
            user_ids: userIds,
            delete_message_seconds: bulkDeleteSeconds,
            reason: bulkReason.trim() || null
          }),
          signal: controller.signal
        }
      );
    } catch (caught) {
      if (!controller.signal.aborted)
        bulkError = userErrorMessage(caught, $t('ui_could_not_complete_the_bulk_ban_bb10a7bf'));
    } finally {
      bulkBusy = false;
    }
  }

  function setSoundDraft(sound: SoundboardSound) {
    soundDrafts = {
      ...soundDrafts,
      [entityKey(sound)]: {
        name: sound.name,
        emojiSelection: sound.emoji_id
          ? `custom:${sound.emoji_id}`
          : sound.emoji_name
            ? 'unicode'
            : 'none',
        emojiName: sound.emoji_id ? '' : (sound.emoji_name ?? ''),
        volume: sound.volume
      }
    };
  }

  function soundEmojiDraft(selection: string, emojiName: string): SoundboardEmojiDraft {
    if (selection.startsWith('custom:')) {
      return { mode: 'custom', emojiId: selection.slice('custom:'.length), emojiName: '' };
    }
    if (selection === 'unicode') return { mode: 'unicode', emojiId: '', emojiName };
    return { mode: 'none', emojiId: '', emojiName: '' };
  }

  function customEmojiForSound(sound: SoundboardSound) {
    if (!sound.emoji_id) return null;
    return (
      soundCustomEmojis.find(
        (emoji) =>
          emoji.id === sound.emoji_id &&
          (!sound.emoji_domain || emoji.origin_domain === sound.emoji_domain)
      ) ?? null
    );
  }

  function canEditSound(sound: SoundboardSound): boolean {
    return (
      isGuildOwner ||
      canEditGuildExpression(
        permissionBits,
        currentUserRef,
        sound.created_by_id,
        sound.created_by_domain
      )
    );
  }

  async function loadSounds() {
    if (!canAccessSounds) return;
    try {
      const result = await api<{ items: SoundboardSound[] }>(
        `/guilds/${encodeURIComponent(entityRef(guild))}/soundboard-sounds`,
        { signal: controller.signal }
      );
      sounds = result.items;
      for (const sound of sounds) setSoundDraft(sound);
    } catch (caught) {
      if (!controller.signal.aborted)
        soundError = userErrorMessage(caught, $t('ui_could_not_load_guild_sounds_c6603443'));
    }
  }

  async function uploadSound(event: SubmitEvent) {
    event.preventDefault();
    if (!canCreateSounds || !soundFile || soundBusy) return;
    if (!['audio/mpeg', 'audio/ogg'].includes(soundFile.type)) {
      soundError = $t('ui_choose_an_mp3_or_ogg_audio_file_c921fa52');
      return;
    }
    if (soundFile.size > 512 * 1024) {
      soundError = $t('ui_soundboard_audio_can_be_at_most_512_kib_610d3555');
      return;
    }
    soundBusy = true;
    soundError = '';
    soundNotice = '';
    try {
      const ticket = await api<UploadTicket>(
        `/guilds/${encodeURIComponent(entityRef(guild))}/soundboard-sounds/tickets`,
        {
          method: 'POST',
          body: JSON.stringify({
            filename: soundFile.name,
            content_type: soundFile.type,
            size: soundFile.size,
            encryption_mode: 'plaintext'
          }),
          signal: controller.signal
        }
      );
      await uploadObject(
        ticket,
        soundFile,
        (value) => (soundUploadProgress = value),
        controller.signal
      );
      const commit = () =>
        api<SoundboardSound | { scan_status: string }>(
          `/guilds/${encodeURIComponent(entityRef(guild))}/soundboard-sounds`,
          {
            method: 'POST',
            body: JSON.stringify({
              attachment_id: ticket.id,
              name: soundName.trim(),
              volume: boundedVolume(soundVolume),
              ...soundboardEmojiPayload(soundEmojiDraft(soundEmojiSelection, soundEmoji))
            }),
            signal: controller.signal
          }
        );
      const result = await completeScannedMediaResource(
        commit,
        (value): value is SoundboardSound => 'name' in value,
        {
          signal: controller.signal,
          maxAttempts: 30,
          rejectedMessage: 'The audio did not pass media processing.',
          timeoutMessage: 'Audio processing is taking longer than expected. Try again shortly.'
        }
      );
      sounds = [...sounds.filter((item) => entityKey(item) !== entityKey(result)), result];
      setSoundDraft(result);
      soundName = '';
      soundEmoji = '';
      soundEmojiSelection = 'none';
      soundVolume = 1;
      soundFile = null;
      soundUploadProgress = 0;
      soundNotice = `“${result.name}” is ready to play.`;
    } catch (caught) {
      if (!controller.signal.aborted)
        soundError = userErrorMessage(caught, $t('ui_could_not_upload_the_sound_d991315e'));
    } finally {
      soundBusy = false;
    }
  }

  function patchSoundDraft(sound: SoundboardSound, patch: Partial<SoundboardDraft>) {
    const key = entityKey(sound);
    soundDrafts = {
      ...soundDrafts,
      [key]: {
        ...(soundDrafts[key] ?? {
          name: sound.name,
          emojiSelection: 'none',
          emojiName: '',
          volume: sound.volume
        }),
        ...patch
      }
    };
  }

  async function updateSound(sound: SoundboardSound) {
    const draftValue = soundDrafts[entityKey(sound)];
    if (!canEditSound(sound) || !draftValue || soundBusy) return;
    soundBusy = true;
    soundError = '';
    try {
      const updated = await api<SoundboardSound>(
        `/guilds/${encodeURIComponent(entityRef(guild))}/soundboard-sounds/${encodeURIComponent(entityRef(sound))}`,
        {
          method: 'PATCH',
          body: JSON.stringify({
            name: draftValue.name.trim(),
            volume: boundedVolume(draftValue.volume),
            ...soundboardEmojiPayload(
              soundEmojiDraft(draftValue.emojiSelection, draftValue.emojiName)
            )
          }),
          signal: controller.signal
        }
      );
      sounds = sounds.map((item) => (entityKey(item) === entityKey(updated) ? updated : item));
      setSoundDraft(updated);
      soundNotice = `“${updated.name}” was updated.`;
    } catch (caught) {
      if (!controller.signal.aborted)
        soundError = userErrorMessage(caught, $t('ui_could_not_update_the_sound_61a120de'));
    } finally {
      soundBusy = false;
    }
  }

  async function deleteSound(sound: SoundboardSound) {
    if (
      !canEditSound(sound) ||
      soundBusy ||
      !confirm(`Delete the sound “${sound.name}”? This cannot be undone.`)
    )
      return;
    soundBusy = true;
    soundError = '';
    try {
      await api(
        `/guilds/${encodeURIComponent(entityRef(guild))}/soundboard-sounds/${encodeURIComponent(entityRef(sound))}`,
        { method: 'DELETE', signal: controller.signal }
      );
      sounds = sounds.filter((item) => entityKey(item) !== entityKey(sound));
      soundNotice = `“${sound.name}” was deleted.`;
    } catch (caught) {
      if (!controller.signal.aborted)
        soundError = userErrorMessage(caught, $t('ui_could_not_delete_the_sound_e0716052'));
    } finally {
      soundBusy = false;
    }
  }

  async function playSound(sound: SoundboardSound) {
    if (!playbackChannel || soundBusy) {
      soundError = $t('ui_choose_the_voice_channel_you_are_currently_co_cf79b4f1');
      return;
    }
    soundBusy = true;
    soundError = '';
    try {
      await api(`/channels/${encodeURIComponent(playbackChannel)}/send-soundboard-sound`, {
        method: 'POST',
        body: JSON.stringify({
          sound_id: entityRef(sound),
          sound_version: sound.version,
          source_guild_id: `${sound.guild_id}@${sound.guild_domain}`
        }),
        signal: controller.signal
      });
      soundNotice = `Playing “${sound.name}” for everyone in the voice channel.`;
    } catch (caught) {
      if (!controller.signal.aborted)
        soundError = userErrorMessage(caught, $t('ui_could_not_play_the_sound_in_voice_141c3d38'));
    } finally {
      soundBusy = false;
    }
  }

  onMount(() => {
    if (voiceChannels[0]) playbackChannel = entityRef(voiceChannels[0]);
    void Promise.all([loadAutoMod(), loadSounds()]);
  });
  onDestroy(() => controller.abort());
</script>

{#if canManageAutoMod}
  <section id="automod" class="tool-section">
    <header>
      <div>
        <h2>{$t('ui_automod_a3ed9717')}</h2>
        <p>{$t('ui_block_harmful_content_alert_moderators_and_ap_de93c7cd')}</p>
      </div>
      <button type="button" class="secondary" disabled={automodBusy} onclick={() => editRule()}
        >{$t('ui_new_rule_99cd24ef')}</button
      >
    </header>
    {#if automodError}<p class="error" role="alert">{automodError}</p>{/if}
    {#if automodNotice}<p class="notice" role="status">{automodNotice}</p>{/if}
    <div class="split">
      <aside class="rule-list" aria-label={$t('ui_automod_rules_52999507')}>
        {#each rules as rule (rule.id)}
          <button
            class:active={selectedRuleId === rule.id}
            type="button"
            onclick={() => editRule(rule)}
          >
            <span>{rule.name}</span><small
              >{rule.enabled ? $t('ui_enabled_92c1cdfd') : $t('ui_disabled_75081b59')} · {rule.trigger_type.replaceAll(
                '_',
                ' '
              )}</small
            >
          </button>
        {:else}<p>{$t('ui_no_automod_rules_yet_fd6ff20b')}</p>{/each}
      </aside>
      <form class="card editor" onsubmit={saveRule}>
        <div class="form-grid two">
          <label
            ><span>{$t('ui_rule_name_7c9de4e8')}</span><input
              bind:value={draft.name}
              minlength="1"
              maxlength="100"
              required
              disabled={automodBusy}
            /></label
          >
          <label
            ><span>{$t('ui_trigger_8b9c6437')}</span>
            <select
              value={draft.triggerType}
              disabled={automodBusy}
              onchange={(event) =>
                normalizeDraftForTrigger(event.currentTarget.value as AutoModTrigger)}
            >
              <option value="keyword">{$t('ui_keyword_or_regex_07c7da52')}</option><option
                value="spam">{$t('ui_spam_94a9eac4')}</option
              ><option value="keyword_preset">{$t('ui_keyword_preset_23173fae')}</option><option
                value="mention_spam">{$t('ui_mention_spam_5bf48c74')}</option
              ><option value="member_profile">{$t('ui_member_profile_4df32473')}</option>
            </select>
          </label>
        </div>
        <label class="check"
          ><input type="checkbox" bind:checked={draft.enabled} disabled={automodBusy} /><span
            ><strong>{$t('ui_enabled_92c1cdfd')}</strong><small
              >{$t('ui_evaluate_new_matching_events_immediately_afte_88a28e01')}</small
            ></span
          ></label
        >
        {#if draft.triggerType === 'keyword' || draft.triggerType === 'member_profile'}
          <div class="form-grid two">
            <label
              ><span>{$t('ui_keywords_and_wildcard_patterns_e64d2bbd')}</span><small
                >{$t('ui_one_per_line_is_supported_3a7e6f05')}</small
              ><textarea bind:value={draft.keywords} rows="5" maxlength="61000"></textarea></label
            >
            <label
              ><span>{$t('ui_safe_regular_expressions_1bef4879')}</span><small
                >{$t('ui_one_per_line_lookarounds_and_backreferences_a_732f5ac1')}</small
              ><textarea bind:value={draft.regexPatterns} rows="5" maxlength="2700"
              ></textarea></label
            >
          </div>
          <label
            ><span>{$t('ui_allowed_terms_4f8e356f')}</span><small
              >{$t('ui_one_per_line_these_bypass_the_keyword_matches_5048c175')}</small
            ><textarea bind:value={draft.allowList} rows="3"></textarea></label
          >
        {:else if draft.triggerType === 'keyword_preset'}
          <fieldset>
            <legend>{$t('ui_built_in_filters_72f4cc8b')}</legend>
            {#each [['profanity', $t('ui_profanity_ec602e94')], ['sexual_content', $t('ui_sexual_content_cedfe045')], ['slurs', $t('ui_slurs_55ee9fcd')]] as preset (preset[0])}
              <label class="check compact"
                ><input
                  type="checkbox"
                  checked={draft.presets.includes(
                    preset[0] as 'profanity' | 'sexual_content' | 'slurs'
                  )}
                  onchange={() =>
                    togglePreset(preset[0] as 'profanity' | 'sexual_content' | 'slurs')}
                /><span>{preset[1]}</span></label
              >
            {/each}
          </fieldset>
          <label
            ><span>{$t('ui_allowed_terms_4f8e356f')}</span><textarea
              bind:value={draft.allowList}
              rows="3"
            ></textarea></label
          >
        {:else if draft.triggerType === 'mention_spam'}
          <div class="form-grid two">
            <label
              ><span>{$t('ui_mention_limit_707d428c')}</span><input
                type="number"
                min="1"
                max="50"
                bind:value={draft.mentionLimit}
              /></label
            >
            <label class="check"
              ><input type="checkbox" bind:checked={draft.mentionRaidProtection} /><span
                ><strong>{$t('ui_raid_protection_675bdb8c')}</strong><small
                  >{$t('ui_use_coordinated_burst_detection_in_addition_t_da67dd53')}</small
                ></span
              ></label
            >
          </div>
        {/if}
        <fieldset>
          <legend>{$t('ui_actions_ff8059dc')}</legend>
          {#if draft.triggerType !== 'member_profile'}
            <label class="check"
              ><input type="checkbox" bind:checked={draft.blockMessage} /><span
                ><strong>{$t('ui_block_message_002c0cd3')}</strong><small
                  >{$t('ui_stop_the_matching_message_before_delivery_a8f0599e')}</small
                ></span
              ></label
            >
            {#if draft.blockMessage}<label
                ><span
                  >{$t('ui_message_shown_to_the_author_40588dd4')}
                  <small>{$t('ui_optional_59be7133')}</small></span
                ><input
                  bind:value={draft.blockMessageText}
                  maxlength="150"
                  placeholder={$t('ui_explain_what_needs_to_change_0de2a4aa')}
                /></label
              >{/if}
          {/if}
          <label class="check"
            ><input type="checkbox" bind:checked={draft.alertMessage} /><span
              ><strong>{$t('ui_send_moderator_alert_e551fd8f')}</strong><small
                >{$t('ui_post_a_server_authored_alert_in_a_plaintext_c_263a7832')}</small
              ></span
            ></label
          >
          {#if draft.alertMessage}<label
              ><span>{$t('ui_alert_channel_9df96c4d')}</span><select
                bind:value={draft.alertChannelRef}
                required
                ><option value="">{$t('ui_choose_a_channel_86306895')}</option
                >{#each textChannels as channel (entityKey(channel))}<option
                    value={entityRef(channel)}>#{channel.name}</option
                  >{/each}</select
              ></label
            >{/if}
          {#if draft.triggerType === 'keyword' || draft.triggerType === 'mention_spam'}
            <label class="check"
              ><input type="checkbox" bind:checked={draft.timeout} /><span
                ><strong>{$t('ui_timeout_member_794af269')}</strong><small
                  >{$t('ui_requires_moderate_members_and_supports_up_to__86406eff')}</small
                ></span
              ></label
            >
            {#if draft.timeout}<label
                ><span>{$t('ui_timeout_seconds_e7a1bb3c')}</span><input
                  type="number"
                  min="1"
                  max="2419200"
                  bind:value={draft.timeoutSeconds}
                /></label
              >{/if}
          {/if}
          {#if draft.triggerType === 'member_profile'}
            <label class="check"
              ><input type="checkbox" bind:checked={draft.blockMemberInteraction} /><span
                ><strong>{$t('ui_block_member_interaction_f678c417')}</strong><small
                  >{$t('ui_quarantine_the_matching_member_profile_from_g_1562b0bc')}</small
                ></span
              ></label
            >
          {/if}
        </fieldset>
        <div class="form-grid two">
          <label
            ><span>{$t('ui_exempt_roles_fbe743ef')}</span><select
              multiple
              size="5"
              value={draft.exemptRoles}
              onchange={(event) => (draft.exemptRoles = selectedValues(event))}
              >{#each manageableRoles as role (entityKey(role))}<option value={entityRef(role)}
                  >{role.name}</option
                >{/each}</select
            ></label
          >
          <label
            ><span>{$t('ui_exempt_channels_5c9fc254')}</span><select
              multiple
              size="5"
              value={draft.exemptChannels}
              onchange={(event) => (draft.exemptChannels = selectedValues(event))}
              >{#each guild.channels ?? [] as channel (entityKey(channel))}{#if channel.type !== 4}<option
                    value={entityRef(channel)}>#{channel.name}</option
                  >{/if}{/each}</select
            ></label
          >
        </div>
        <footer>
          <button class="primary" disabled={automodBusy}
            >{automodBusy
              ? $t('ui_saving_23e39291')
              : selectedRuleId
                ? $t('ui_save_rule_4e62c225')
                : $t('ui_create_rule_e2077a44')}</button
          >{#if selectedRuleId}<button
              type="button"
              class="danger"
              disabled={automodBusy}
              onclick={() => void deleteRule()}>{$t('ui_delete_rule_075d1139')}</button
            >{/if}
        </footer>
      </form>
    </div>
  </section>
{/if}

{#if canPrune || canBulkBan}
  <section id="bulk-moderation" class="tool-section">
    <header>
      <div>
        <h2>{$t('ui_bulk_moderation_7b53e29a')}</h2>
        <p>{$t('ui_estimate_destructive_actions_first_and_review_2c4cc52a')}</p>
      </div>
    </header>
    <div class="cards">
      {#if canPrune}<article class="card">
          <h3>{$t('ui_prune_inactive_members_408468e5')}</h3>
          <p>{$t('ui_by_default_only_roleless_humans_with_no_guild_3e1eafef')}</p>
          {#if pruneError}<p class="error" role="alert">{pruneError}</p>{/if}
          <div class="form-grid two">
            <label
              ><span>{$t('ui_inactive_for_72acc89e')}</span><select
                value={pruneDays}
                onchange={(event) => {
                  pruneDays = Number(event.currentTarget.value);
                  pruneEstimate = null;
                  pruneResult = null;
                }}
                ><option value={1}>{$t('ui_1_day_fa665d95')}</option><option value={7}
                  >{$t('ui_7_days_7f920bb6')}</option
                ><option value={14}>{$t('ui_14_days_60acc36e')}</option><option value={30}
                  >{$t('ui_30_days_ffd72805')}</option
                ></select
              ></label
            ><label
              ><span>{$t('ui_also_include_these_roles_a04fd499')}</span><select
                multiple
                size="4"
                value={pruneRoles}
                onchange={(event) => {
                  pruneRoles = selectedValues(event);
                  pruneEstimate = null;
                  pruneResult = null;
                }}
                >{#each manageableRoles as role (entityKey(role))}<option value={entityRef(role)}
                    >{role.name}</option
                  >{/each}</select
              ></label
            >
          </div>
          {#if pruneEstimate !== null}<p class="result">
              <strong>{pruneEstimate}</strong>
              {$t('ui_member_value0_currently_eligible_1084e175', {
                value0: String(pruneEstimate === 1 ? '' : 's')
              })}
            </p>{/if}
          {#if pruneResult}<p class="result">
              <strong>{pruneResult.pruned ?? 0}</strong>
              pruned.
            </p>
            {#if pruneResult.failed_users?.length}<ul class="failures">
                {#each pruneResult.failed_users as failure (`${failure.user_id}:${failure.code}`)}<li
                  >
                    <code>{failure.user_id}</code>: {failure.message}
                  </li>{/each}
              </ul>{/if}{/if}
          <footer>
            <button
              type="button"
              class="secondary"
              disabled={pruneBusy}
              onclick={() => void estimatePrune()}
              >{pruneBusy ? $t('ui_checking_ec963ffc') : $t('ui_estimate_558ecb0f')}</button
            ><button
              type="button"
              class="danger"
              disabled={pruneBusy || pruneEstimate === null || pruneEstimate === 0}
              onclick={() => void executePrune()}>{$t('ui_prune_eligible_members_7090e4a1')}</button
            >
          </footer>
        </article>{/if}
      {#if canBulkBan}<form class="card" onsubmit={executeBulkBan}>
          <h3>{$t('ui_bulk_ban_74676f94')}</h3>
          <p>{$t('ui_enter_canonical_user_references_role_hierarch_3033b62c')}</p>
          {#if bulkError}<p class="error" role="alert">{bulkError}</p>{/if}
          <label
            ><span>{$t('ui_user_references_1446248f')}</span><small
              >{$t('ui_one_id_domain_per_line_up_to_200_205f975b')}</small
            ><textarea bind:value={bulkUsers} rows="6" required placeholder="123456789@chat.example"
            ></textarea></label
          >
          <div class="form-grid two">
            <label
              ><span>{$t('ui_reason_f81ab834')} <small>{$t('ui_optional_59be7133')}</small></span
              ><input bind:value={bulkReason} maxlength="512" /></label
            ><label
              ><span>{$t('ui_delete_recent_messages_e6a404bc')}</span><select
                bind:value={bulkDeleteSeconds}
                ><option value={0}>{$t('ui_do_not_delete_71c7190a')}</option><option value={3600}
                  >{$t('ui_previous_hour_e680d771')}</option
                ><option value={86400}>{$t('ui_previous_day_e4a1e89e')}</option><option
                  value={604800}>{$t('ui_previous_7_days_c6327b7b')}</option
                ></select
              ></label
            >
          </div>
          {#if bulkResult}<p class="result">
              <strong>{bulkResult.banned_users.length}</strong>
              {$t('ui_banned_a280f60d')} <strong>{bulkResult.failed_users.length}</strong>
              failed.
            </p>
            {#if bulkResult.failed_user_details.length}<ul class="failures">
                {#each bulkResult.failed_user_details as failure (`${failure.user_id}:${failure.code}`)}<li
                  >
                    <code>{failure.user_id}</code>: {failure.message}
                  </li>{/each}
              </ul>{/if}{/if}
          <footer>
            <button class="danger" disabled={bulkBusy}
              >{bulkBusy ? $t('ui_banning_0a933b8e') : $t('ui_review_and_ban_cd3faa3f')}</button
            >
          </footer>
        </form>{/if}
    </div>
  </section>
{/if}

{#if canAccessSounds}
  <section id="soundboard" class="tool-section">
    <header>
      <div>
        <h2>{$t('ui_soundboard_07ff885c')}</h2>
        <p>{$t('ui_manage_short_guild_sounds_and_play_them_for_e_cb2a723b')}</p>
      </div>
    </header>
    {#if soundError}<p class="error" role="alert">{soundError}</p>{/if}{#if soundNotice}<p
        class="notice"
        role="status"
      >
        {soundNotice}
      </p>{/if}
    {#if canCreateSounds}<form class="card" onsubmit={uploadSound}>
        <h3>{$t('ui_upload_sound_48f0b1bd')}</h3>
        <div class="form-grid four">
          <label
            ><span>{$t('ui_name_dcd1d522')}</span><input
              bind:value={soundName}
              minlength="2"
              maxlength="32"
              required
            /></label
          ><label
            ><span>{$t('ui_emoji_61ad8976')} <small>{$t('ui_optional_59be7133')}</small></span
            ><select bind:value={soundEmojiSelection}
              ><option value="none">{$t('ui_no_emoji_9130ed0c')}</option><option value="unicode"
                >{$t('ui_unicode_emoji_3fb39976')}</option
              >{#each soundCustomEmojis as emoji (entityKey(emoji))}<option
                  value={`custom:${emoji.id}`}>:{emoji.name}:</option
                >{/each}</select
            ></label
          >{#if soundEmojiSelection === 'unicode'}<label
              ><span>{$t('ui_unicode_emoji_3fb39976')}</span><input
                bind:value={soundEmoji}
                maxlength="64"
                required
              /></label
            >{/if}
          ><label
            ><span>{$t('ui_default_volume_2aed61f3')}</span><input
              type="range"
              min="0"
              max="1"
              step="0.05"
              bind:value={soundVolume}
            /><small>{Math.round(soundVolume * 100)}%</small></label
          ><label
            ><span>{$t('ui_mp3_or_ogg_00fb645d')}</span><input
              type="file"
              accept="audio/mpeg,audio/ogg,.mp3,.ogg"
              required
              onchange={(event) => (soundFile = event.currentTarget.files?.[0] ?? null)}
            /></label
          >
        </div>
        {#if soundUploadProgress}<progress max="100" value={soundUploadProgress}></progress>{/if}
        <footer>
          <button class="primary" disabled={soundBusy || sounds.length >= 48}
            >{soundBusy ? $t('ui_processing_42074396') : $t('ui_upload_sound_48f0b1bd')}</button
          >
        </footer>
      </form>{/if}
    {#if canUseSounds}<label class="playback-channel"
        ><span>{$t('ui_voice_channel_you_joined_bd938267')}</span><select
          bind:value={playbackChannel}
          ><option value="">{$t('ui_choose_a_voice_channel_46fdbd9d')}</option
          >{#each voiceChannels as channel (entityKey(channel))}<option value={entityRef(channel)}
              >{channel.name}</option
            >{/each}</select
        ></label
      >{/if}
    <div class="sound-grid">
      {#each sounds as sound (entityKey(sound))}
        {@const customEmoji = customEmojiForSound(sound)}
        <article class="card sound">
          <div class="sound-title">
            {#if customEmoji?.media_hash}<img
                src={assetUrl(customEmoji.media_hash, 'thumbnail_128', customEmoji.origin_domain)}
                alt={`:${customEmoji.name}:`}
                loading="lazy"
              />{:else}<span aria-hidden="true">{sound.emoji_name ?? '♫'}</span>{/if}
            <div>
              <strong>{sound.name}</strong><small
                >{$t('ui_value0_seconds_value1_b07f26dd', {
                  value0: String((sound.duration_ms / 1000).toFixed(1)),
                  value1: String(sound.available ? 'Available' : 'Unavailable')
                })}</small
              >
            </div>
          </div>
          {#if canEditSound(sound) && soundDrafts[entityKey(sound)]}<div class="form-grid three">
              <label
                ><span>{$t('ui_name_dcd1d522')}</span><input
                  value={soundDrafts[entityKey(sound)].name}
                  minlength="2"
                  maxlength="32"
                  oninput={(event) => patchSoundDraft(sound, { name: event.currentTarget.value })}
                /></label
              ><label
                ><span>{$t('ui_emoji_61ad8976')}</span><select
                  value={soundDrafts[entityKey(sound)].emojiSelection}
                  onchange={(event) =>
                    patchSoundDraft(sound, {
                      emojiSelection: event.currentTarget.value,
                      emojiName:
                        event.currentTarget.value === 'unicode'
                          ? soundDrafts[entityKey(sound)].emojiName
                          : ''
                    })}
                >
                  <option value="none">{$t('ui_no_emoji_9130ed0c')}</option><option value="unicode"
                    >{$t('ui_unicode_emoji_3fb39976')}</option
                  >{#if sound.emoji_id && !soundCustomEmojis.some((emoji) => emoji.id === sound.emoji_id)}<option
                      value={`custom:${sound.emoji_id}`}
                      >{$t('ui_current_custom_emoji_b83f1e7e')}</option
                    >{/if}{#each soundCustomEmojis as emoji (entityKey(emoji))}<option
                      value={`custom:${emoji.id}`}>:{emoji.name}:</option
                    >{/each}
                </select></label
              >{#if soundDrafts[entityKey(sound)].emojiSelection === 'unicode'}<label
                  ><span>{$t('ui_unicode_emoji_3fb39976')}</span><input
                    value={soundDrafts[entityKey(sound)].emojiName}
                    maxlength="64"
                    required
                    oninput={(event) =>
                      patchSoundDraft(sound, { emojiName: event.currentTarget.value })}
                  /></label
                >{/if}<label
                ><span>{$t('ui_volume_b10fb966')}</span><input
                  type="range"
                  min="0"
                  max="1"
                  step="0.05"
                  value={soundDrafts[entityKey(sound)].volume}
                  oninput={(event) =>
                    patchSoundDraft(sound, { volume: event.currentTarget.valueAsNumber })}
                /><small>{Math.round(soundDrafts[entityKey(sound)].volume * 100)}%</small></label
              >
            </div>{/if}
          <footer>
            {#if canUseSounds}<button
                type="button"
                class="primary"
                disabled={soundBusy || !sound.available}
                onclick={() => void playSound(sound)}>{$t('ui_play_in_voice_2e7ae7b5')}</button
              >{/if}{#if canEditSound(sound)}<button
                type="button"
                class="secondary"
                disabled={soundBusy}
                onclick={() => void updateSound(sound)}>{$t('ui_save_1509f561')}</button
              ><button
                type="button"
                class="danger"
                disabled={soundBusy}
                onclick={() => void deleteSound(sound)}>{$t('ui_delete_e2d0a549')}</button
              >{/if}
          </footer>
        </article>
      {:else}<p>{$t('ui_no_guild_sounds_have_been_uploaded_331929c6')}</p>{/each}
    </div>
  </section>
{/if}

<style>
  .tool-section {
    scroll-margin-top: 1rem;
    margin: 0 0 2rem;
  }
  header,
  footer,
  .sound-title {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    flex-wrap: wrap;
  }
  h2,
  h3,
  p {
    margin: 0.2rem 0;
  }
  header p,
  .card > p,
  small {
    color: var(--text-muted);
  }
  .card,
  .rule-list {
    border: 1px solid var(--line);
    border-radius: 13px;
    background: var(--surface);
    padding: 1rem;
  }
  .split {
    display: grid;
    grid-template-columns: minmax(180px, 0.35fr) minmax(0, 1fr);
    gap: 1rem;
    margin-top: 1rem;
    align-items: start;
  }
  .rule-list {
    display: grid;
    gap: 0.4rem;
  }
  .rule-list button {
    display: grid;
    gap: 0.2rem;
    text-align: left;
    border: 1px solid transparent;
    border-radius: 9px;
    padding: 0.7rem;
    color: inherit;
    background: transparent;
  }
  .rule-list button.active,
  .rule-list button:hover {
    border-color: var(--accent);
    background: var(--surface-hover);
  }
  .editor,
  fieldset {
    display: grid;
    gap: 0.85rem;
  }
  fieldset {
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 0.8rem;
  }
  legend {
    padding: 0 0.3rem;
    font-weight: 800;
  }
  label {
    display: grid;
    gap: 0.3rem;
    font-weight: 700;
  }
  label > small {
    font-weight: 400;
  }
  input,
  select,
  textarea {
    box-sizing: border-box;
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.65rem;
    color: var(--text);
    background: var(--surface-raised, var(--surface));
    font: inherit;
  }
  select[multiple] {
    min-height: 7rem;
  }
  .check {
    grid-template-columns: auto minmax(0, 1fr);
    align-items: start;
  }
  .check input {
    width: auto;
    margin-top: 0.25rem;
  }
  .check span,
  .check strong,
  .check small {
    display: block;
  }
  .check.compact {
    display: inline-grid;
    margin-right: 1rem;
  }
  .form-grid,
  .cards,
  .sound-grid {
    display: grid;
    gap: 0.8rem;
  }
  .form-grid.two,
  .cards {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .form-grid.three {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
  .form-grid.four {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }
  .cards,
  .sound-grid {
    margin-top: 1rem;
  }
  .sound-grid {
    grid-template-columns: repeat(auto-fit, minmax(290px, 1fr));
  }
  .sound {
    display: grid;
    gap: 0.8rem;
  }
  .sound-title {
    justify-content: flex-start;
  }
  .sound-title > span,
  .sound-title > img {
    display: grid;
    width: 2.5rem;
    height: 2.5rem;
    place-items: center;
    border-radius: 9px;
    background: var(--surface-hover);
    font-size: 1.3rem;
  }
  .sound-title > img {
    object-fit: contain;
  }
  .sound-title strong,
  .sound-title small {
    display: block;
  }
  button {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.65rem 0.85rem;
    font: inherit;
    font-weight: 800;
    color: var(--text);
    background: var(--surface);
  }
  button.primary {
    border-color: var(--accent);
    color: white;
    background: var(--accent);
  }
  button.danger {
    border-color: var(--danger, #d84a4a);
    color: var(--danger, #ef6767);
    background: transparent;
  }
  button:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  footer {
    justify-content: flex-end;
    margin-top: 0.4rem;
  }
  .error,
  .notice,
  .result {
    border-radius: 9px;
    padding: 0.7rem;
  }
  .error {
    color: var(--danger, #ef6767);
    background: color-mix(in srgb, var(--danger, #d84a4a) 12%, transparent);
  }
  .notice,
  .result {
    background: var(--surface-hover);
  }
  .failures {
    max-height: 12rem;
    overflow: auto;
    color: var(--danger, #ef6767);
  }
  .playback-channel {
    max-width: 30rem;
    margin: 1rem 0;
  }
  progress {
    width: 100%;
  }
  @media (max-width: 800px) {
    .split,
    .form-grid.two,
    .form-grid.three,
    .form-grid.four,
    .cards {
      grid-template-columns: 1fr;
    }
  }
</style>
