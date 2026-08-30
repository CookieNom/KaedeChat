<script lang="ts">
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
        automodError = userErrorMessage(caught, 'Could not load AutoMod rules.');
    }
  }

  async function saveRule(event: SubmitEvent) {
    event.preventDefault();
    if (automodBusy) return;
    const payload = autoModPayload(draft);
    const actions = payload.actions as unknown[];
    if (!draft.name.trim()) {
      automodError = 'Give this AutoMod rule a name.';
      return;
    }
    if (!actions.length) {
      automodError = 'Choose at least one action for this AutoMod rule.';
      return;
    }
    if (
      (draft.triggerType === 'keyword' || draft.triggerType === 'member_profile') &&
      !uniqueNonemptyLines(draft.keywords).length &&
      !uniqueNonemptyLines(draft.regexPatterns).length
    ) {
      automodError = 'Add at least one keyword, wildcard pattern, or safe regular expression.';
      return;
    }
    if (draft.triggerType === 'keyword_preset' && !draft.presets.length) {
      automodError = 'Choose at least one built-in keyword filter.';
      return;
    }
    if (draft.alertMessage && !draft.alertChannelRef) {
      automodError = 'Choose a plaintext text channel for AutoMod alerts.';
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
        automodError = userErrorMessage(caught, 'Could not save the AutoMod rule.');
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
      automodNotice = 'AutoMod rule deleted.';
    } catch (caught) {
      if (!controller.signal.aborted)
        automodError = userErrorMessage(caught, 'Could not delete the AutoMod rule.');
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
        pruneError = userErrorMessage(caught, 'Could not estimate inactive members.');
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
        pruneError = userErrorMessage(caught, 'Could not prune inactive members.');
    } finally {
      pruneBusy = false;
    }
  }

  async function executeBulkBan(event: SubmitEvent) {
    event.preventDefault();
    if (bulkBusy) return;
    const userIds = uniqueNonemptyLines(bulkUsers);
    if (!userIds.length) {
      bulkError = 'Enter at least one user reference, one per line.';
      return;
    }
    if (userIds.length > 200) {
      bulkError = 'Bulk bans can include at most 200 users at a time.';
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
        bulkError = userErrorMessage(caught, 'Could not complete the bulk ban.');
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
        soundError = userErrorMessage(caught, 'Could not load guild sounds.');
    }
  }

  async function uploadSound(event: SubmitEvent) {
    event.preventDefault();
    if (!canCreateSounds || !soundFile || soundBusy) return;
    if (!['audio/mpeg', 'audio/ogg'].includes(soundFile.type)) {
      soundError = 'Choose an MP3 or Ogg audio file.';
      return;
    }
    if (soundFile.size > 512 * 1024) {
      soundError = 'Soundboard audio can be at most 512 KiB.';
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
        soundError = userErrorMessage(caught, 'Could not upload the sound.');
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
        soundError = userErrorMessage(caught, 'Could not update the sound.');
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
        soundError = userErrorMessage(caught, 'Could not delete the sound.');
    } finally {
      soundBusy = false;
    }
  }

  async function playSound(sound: SoundboardSound) {
    if (!playbackChannel || soundBusy) {
      soundError = 'Choose the voice channel you are currently connected to.';
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
        soundError = userErrorMessage(caught, 'Could not play the sound in voice.');
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
        <h2>AutoMod</h2>
        <p>Block harmful content, alert moderators, and apply bounded timeouts.</p>
      </div>
      <button type="button" class="secondary" disabled={automodBusy} onclick={() => editRule()}
        >New rule</button
      >
    </header>
    {#if automodError}<p class="error" role="alert">{automodError}</p>{/if}
    {#if automodNotice}<p class="notice" role="status">{automodNotice}</p>{/if}
    <div class="split">
      <aside class="rule-list" aria-label="AutoMod rules">
        {#each rules as rule (rule.id)}
          <button
            class:active={selectedRuleId === rule.id}
            type="button"
            onclick={() => editRule(rule)}
          >
            <span>{rule.name}</span><small
              >{rule.enabled ? 'Enabled' : 'Disabled'} · {rule.trigger_type.replaceAll(
                '_',
                ' '
              )}</small
            >
          </button>
        {:else}<p>No AutoMod rules yet.</p>{/each}
      </aside>
      <form class="card editor" onsubmit={saveRule}>
        <div class="form-grid two">
          <label
            ><span>Rule name</span><input
              bind:value={draft.name}
              minlength="1"
              maxlength="100"
              required
              disabled={automodBusy}
            /></label
          >
          <label
            ><span>Trigger</span>
            <select
              value={draft.triggerType}
              disabled={automodBusy}
              onchange={(event) =>
                normalizeDraftForTrigger(event.currentTarget.value as AutoModTrigger)}
            >
              <option value="keyword">Keyword or regex</option><option value="spam">Spam</option
              ><option value="keyword_preset">Keyword preset</option><option value="mention_spam"
                >Mention spam</option
              ><option value="member_profile">Member profile</option>
            </select>
          </label>
        </div>
        <label class="check"
          ><input type="checkbox" bind:checked={draft.enabled} disabled={automodBusy} /><span
            ><strong>Enabled</strong><small
              >Evaluate new matching events immediately after saving.</small
            ></span
          ></label
        >
        {#if draft.triggerType === 'keyword' || draft.triggerType === 'member_profile'}
          <div class="form-grid two">
            <label
              ><span>Keywords and wildcard patterns</span><small
                >One per line; * is supported.</small
              ><textarea bind:value={draft.keywords} rows="5" maxlength="61000"></textarea></label
            >
            <label
              ><span>Safe regular expressions</span><small
                >One per line; lookarounds and backreferences are rejected.</small
              ><textarea bind:value={draft.regexPatterns} rows="5" maxlength="2700"
              ></textarea></label
            >
          </div>
          <label
            ><span>Allowed terms</span><small>One per line. These bypass the keyword matches.</small
            ><textarea bind:value={draft.allowList} rows="3"></textarea></label
          >
        {:else if draft.triggerType === 'keyword_preset'}
          <fieldset>
            <legend>Built-in filters</legend>
            {#each [['profanity', 'Profanity'], ['sexual_content', 'Sexual content'], ['slurs', 'Slurs']] as preset (preset[0])}
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
            ><span>Allowed terms</span><textarea bind:value={draft.allowList} rows="3"
            ></textarea></label
          >
        {:else if draft.triggerType === 'mention_spam'}
          <div class="form-grid two">
            <label
              ><span>Mention limit</span><input
                type="number"
                min="1"
                max="50"
                bind:value={draft.mentionLimit}
              /></label
            >
            <label class="check"
              ><input type="checkbox" bind:checked={draft.mentionRaidProtection} /><span
                ><strong>Raid protection</strong><small
                  >Use coordinated-burst detection in addition to the per-message limit.</small
                ></span
              ></label
            >
          </div>
        {/if}
        <fieldset>
          <legend>Actions</legend>
          {#if draft.triggerType !== 'member_profile'}
            <label class="check"
              ><input type="checkbox" bind:checked={draft.blockMessage} /><span
                ><strong>Block message</strong><small
                  >Stop the matching message before delivery.</small
                ></span
              ></label
            >
            {#if draft.blockMessage}<label
                ><span>Message shown to the author <small>Optional</small></span><input
                  bind:value={draft.blockMessageText}
                  maxlength="150"
                  placeholder="Explain what needs to change"
                /></label
              >{/if}
          {/if}
          <label class="check"
            ><input type="checkbox" bind:checked={draft.alertMessage} /><span
              ><strong>Send moderator alert</strong><small
                >Post a server-authored alert in a plaintext channel.</small
              ></span
            ></label
          >
          {#if draft.alertMessage}<label
              ><span>Alert channel</span><select bind:value={draft.alertChannelRef} required
                ><option value="">Choose a channel</option
                >{#each textChannels as channel (entityKey(channel))}<option
                    value={entityRef(channel)}>#{channel.name}</option
                  >{/each}</select
              ></label
            >{/if}
          {#if draft.triggerType === 'keyword' || draft.triggerType === 'mention_spam'}
            <label class="check"
              ><input type="checkbox" bind:checked={draft.timeout} /><span
                ><strong>Timeout member</strong><small
                  >Requires Moderate Members and supports up to 28 days.</small
                ></span
              ></label
            >
            {#if draft.timeout}<label
                ><span>Timeout seconds</span><input
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
                ><strong>Block member interaction</strong><small
                  >Quarantine the matching member profile from guild interaction.</small
                ></span
              ></label
            >
          {/if}
        </fieldset>
        <div class="form-grid two">
          <label
            ><span>Exempt roles</span><select
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
            ><span>Exempt channels</span><select
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
            >{automodBusy ? 'Saving…' : selectedRuleId ? 'Save rule' : 'Create rule'}</button
          >{#if selectedRuleId}<button
              type="button"
              class="danger"
              disabled={automodBusy}
              onclick={() => void deleteRule()}>Delete rule</button
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
        <h2>Bulk moderation</h2>
        <p>Estimate destructive actions first and review every per-user failure.</p>
      </div>
    </header>
    <div class="cards">
      {#if canPrune}<article class="card">
          <h3>Prune inactive members</h3>
          <p>
            By default, only roleless humans with no guild activity in the selected period are
            eligible.
          </p>
          {#if pruneError}<p class="error" role="alert">{pruneError}</p>{/if}
          <div class="form-grid two">
            <label
              ><span>Inactive for</span><select
                value={pruneDays}
                onchange={(event) => {
                  pruneDays = Number(event.currentTarget.value);
                  pruneEstimate = null;
                  pruneResult = null;
                }}
                ><option value={1}>1 day</option><option value={7}>7 days</option><option value={14}
                  >14 days</option
                ><option value={30}>30 days</option></select
              ></label
            ><label
              ><span>Also include these roles</span><select
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
              <strong>{pruneEstimate}</strong> member{pruneEstimate === 1 ? '' : 's'} currently eligible.
            </p>{/if}
          {#if pruneResult}<p class="result"><strong>{pruneResult.pruned ?? 0}</strong> pruned.</p>
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
              onclick={() => void estimatePrune()}>{pruneBusy ? 'Checking…' : 'Estimate'}</button
            ><button
              type="button"
              class="danger"
              disabled={pruneBusy || pruneEstimate === null || pruneEstimate === 0}
              onclick={() => void executePrune()}>Prune eligible members</button
            >
          </footer>
        </article>{/if}
      {#if canBulkBan}<form class="card" onsubmit={executeBulkBan}>
          <h3>Bulk ban</h3>
          <p>
            Enter canonical user references. Role hierarchy and guild ownership are checked for
            every user.
          </p>
          {#if bulkError}<p class="error" role="alert">{bulkError}</p>{/if}
          <label
            ><span>User references</span><small>One id@domain per line; up to 200.</small><textarea
              bind:value={bulkUsers}
              rows="6"
              required
              placeholder="123456789@chat.example"
            ></textarea></label
          >
          <div class="form-grid two">
            <label
              ><span>Reason <small>Optional</small></span><input
                bind:value={bulkReason}
                maxlength="512"
              /></label
            ><label
              ><span>Delete recent messages</span><select bind:value={bulkDeleteSeconds}
                ><option value={0}>Do not delete</option><option value={3600}>Previous hour</option
                ><option value={86400}>Previous day</option><option value={604800}
                  >Previous 7 days</option
                ></select
              ></label
            >
          </div>
          {#if bulkResult}<p class="result">
              <strong>{bulkResult.banned_users.length}</strong> banned;
              <strong>{bulkResult.failed_users.length}</strong> failed.
            </p>
            {#if bulkResult.failed_user_details.length}<ul class="failures">
                {#each bulkResult.failed_user_details as failure (`${failure.user_id}:${failure.code}`)}<li
                  >
                    <code>{failure.user_id}</code>: {failure.message}
                  </li>{/each}
              </ul>{/if}{/if}
          <footer>
            <button class="danger" disabled={bulkBusy}
              >{bulkBusy ? 'Banning…' : 'Review and ban'}</button
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
        <h2>Soundboard</h2>
        <p>Manage short guild sounds and play them for everyone in your active voice channel.</p>
      </div>
    </header>
    {#if soundError}<p class="error" role="alert">{soundError}</p>{/if}{#if soundNotice}<p
        class="notice"
        role="status"
      >
        {soundNotice}
      </p>{/if}
    {#if canCreateSounds}<form class="card" onsubmit={uploadSound}>
        <h3>Upload sound</h3>
        <div class="form-grid four">
          <label
            ><span>Name</span><input
              bind:value={soundName}
              minlength="2"
              maxlength="32"
              required
            /></label
          ><label
            ><span>Emoji <small>Optional</small></span><select bind:value={soundEmojiSelection}
              ><option value="none">No emoji</option><option value="unicode">Unicode emoji</option
              >{#each soundCustomEmojis as emoji (entityKey(emoji))}<option
                  value={`custom:${emoji.id}`}>:{emoji.name}:</option
                >{/each}</select
            ></label
          >{#if soundEmojiSelection === 'unicode'}<label
              ><span>Unicode emoji</span><input
                bind:value={soundEmoji}
                maxlength="64"
                required
              /></label
            >{/if}
          ><label
            ><span>Default volume</span><input
              type="range"
              min="0"
              max="1"
              step="0.05"
              bind:value={soundVolume}
            /><small>{Math.round(soundVolume * 100)}%</small></label
          ><label
            ><span>MP3 or Ogg</span><input
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
            >{soundBusy ? 'Processing…' : 'Upload sound'}</button
          >
        </footer>
      </form>{/if}
    {#if canUseSounds}<label class="playback-channel"
        ><span>Voice channel you joined</span><select bind:value={playbackChannel}
          ><option value="">Choose a voice channel</option
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
                >{(sound.duration_ms / 1000).toFixed(1)} seconds · {sound.available
                  ? 'Available'
                  : 'Unavailable'}</small
              >
            </div>
          </div>
          {#if canEditSound(sound) && soundDrafts[entityKey(sound)]}<div class="form-grid three">
              <label
                ><span>Name</span><input
                  value={soundDrafts[entityKey(sound)].name}
                  minlength="2"
                  maxlength="32"
                  oninput={(event) => patchSoundDraft(sound, { name: event.currentTarget.value })}
                /></label
              ><label
                ><span>Emoji</span><select
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
                  <option value="none">No emoji</option><option value="unicode"
                    >Unicode emoji</option
                  >{#if sound.emoji_id && !soundCustomEmojis.some((emoji) => emoji.id === sound.emoji_id)}<option
                      value={`custom:${sound.emoji_id}`}>Current custom emoji</option
                    >{/if}{#each soundCustomEmojis as emoji (entityKey(emoji))}<option
                      value={`custom:${emoji.id}`}>:{emoji.name}:</option
                    >{/each}
                </select></label
              >{#if soundDrafts[entityKey(sound)].emojiSelection === 'unicode'}<label
                  ><span>Unicode emoji</span><input
                    value={soundDrafts[entityKey(sound)].emojiName}
                    maxlength="64"
                    required
                    oninput={(event) =>
                      patchSoundDraft(sound, { emojiName: event.currentTarget.value })}
                  /></label
                >{/if}<label
                ><span>Volume</span><input
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
                onclick={() => void playSound(sound)}>Play in voice</button
              >{/if}{#if canEditSound(sound)}<button
                type="button"
                class="secondary"
                disabled={soundBusy}
                onclick={() => void updateSound(sound)}>Save</button
              ><button
                type="button"
                class="danger"
                disabled={soundBusy}
                onclick={() => void deleteSound(sound)}>Delete</button
              >{/if}
          </footer>
        </article>
      {:else}<p>No guild sounds have been uploaded.</p>{/each}
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
