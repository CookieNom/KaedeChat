<script lang="ts">
  import { api, ApiError, userErrorMessage } from '$lib/api/client';
  import Icon from '$lib/components/Icon.svelte';
  import { Permission } from '$lib/generated/permissions';
  import { onDestroy, onMount } from 'svelte';
  import { SvelteMap, SvelteSet } from 'svelte/reactivity';
  import { isNativeDesktop } from '$lib/platform/native';
  import { initializeE2EE } from '$lib/e2ee/client';
  import { base64url, randomBytes } from '$lib/e2ee/encoding';
  import { entityKey, entityRef } from '$lib/chat/refs';
  import { userDisplayName } from '$lib/chat/users';
  import type { Channel } from '$lib/chat/types';
  import { chatEntities as entities } from '$lib/stores/entities.svelte';
  import { authenticatedGateway } from '$lib/gateway/runtime.svelte';
  import ScreenShareDialog from './ScreenShareDialog.svelte';
  import type { MediaQualityPreferences } from './quality';
  import {
    loadSoundboardMedia,
    soundboardChannelSupported,
    soundboardPlaybackUnavailableReason,
    soundboardSourceAllowed
  } from './soundboard';
  import { formatVoiceElapsed } from './elapsed';
  import type { VoiceOccupant } from './occupancy';
  import { canManageStageChannel } from './stage-permissions';

  import {
    attachVideo,
    VoiceConnectionFence,
    VoiceSession,
    expectedVoicePolicy,
    type VoiceToken
  } from './session';

  let {
    channelRef,
    callRef,
    permissions = null,
    occupants = [],
    startedAt = null,
    onApps
  }: {
    channelRef?: string;
    callRef?: string;
    permissions?: string | null;
    occupants?: VoiceOccupant[];
    startedAt?: number | null;
    onApps?: () => void;
  } = $props();
  const voice = new VoiceSession(undefined, (state) =>
    authenticatedGateway.client.setSelfVoiceState(state.self_mute, state.self_deaf)
  );
  let revision = $state(0);
  let elapsedClock = $state(Date.now());
  let error = $state('');
  let takeoverPrompt = $state<string | null>(null);
  let screenShareOpen = $state(false);
  let soundboardOpen = $state(false);
  let soundboardLoading = $state(false);
  let soundboardBusy = $state('');
  interface SoundboardSound {
    id: string;
    origin_domain: string;
    guild_id: string | null;
    guild_domain: string | null;
    name: string;
    version: string;
    emoji_name?: string | null;
  }
  interface SoundboardGroup {
    key: string;
    label: string;
    sounds: SoundboardSound[];
  }
  interface StageInstance {
    id: string;
    origin_domain: string;
    guild_id: string;
    guild_domain: string;
    channel_id: string;
    channel_domain: string;
    topic: string;
    privacy_level: 2;
    discoverable_disabled: boolean;
    guild_scheduled_event_id: string | null;
    guild_scheduled_event_domain: string | null;
  }
  let soundboardGroups = $state<SoundboardGroup[]>([]);
  let stageInstance = $state<StageInstance | null>(null);
  let stageLoading = $state(false);
  let stageLoaded = $state(false);
  let stageVoiceBusy = $state('');
  let stageVoiceOverrides = $state<Record<string, Partial<VoiceOccupant>>>({});
  let audioHost = $state<HTMLElement | null>(null);
  let detachAudio: (() => void) | null = null;
  const activeSoundboardAudio = new SvelteSet<HTMLAudioElement>();
  const soundboardObjectUrls = new SvelteMap<HTMLAudioElement, string>();
  let mounted = false;
  const connectionFence = new VoiceConnectionFence();
  let connectionId = base64url(randomBytes(32));
  let joinController: AbortController | null = null;
  const permissionBits = $derived.by(() => {
    if (callRef || permissions === null) return null;
    try {
      return BigInt(permissions);
    } catch {
      return 0n;
    }
  });
  const canConnect = $derived(
    permissionBits === null ||
      Boolean(permissionBits & (Permission.ADMINISTRATOR | Permission.CONNECT))
  );
  const isStageChannel = $derived(selectedChannel()?.type === 13);
  const canManageStage = $derived(
    isStageChannel && permissions !== null && canManageStageChannel({ type: 13, permissions })
  );
  const canNotifyStage = $derived(
    isStageChannel &&
      permissionBits !== null &&
      Boolean(permissionBits & (Permission.ADMINISTRATOR | Permission.MENTION_EVERYONE))
  );
  // Discord exposes the participant moderation controls to Stage moderators,
  // which are defined by the same complete three-permission set as lifecycle
  // controls. The lower-level API still honors MUTE_MEMBERS for bot parity.
  const canModerateStage = $derived(canManageStage);
  const canRequestToSpeak = $derived(
    isStageChannel &&
      permissionBits !== null &&
      Boolean(permissionBits & (Permission.ADMINISTRATOR | Permission.REQUEST_TO_SPEAK))
  );
  const canJoinVoice = $derived(
    canConnect && (!isStageChannel || (stageLoaded && stageInstance !== null))
  );
  const permittedToSpeak = $derived(
    permissionBits === null ||
      isStageChannel ||
      Boolean(permissionBits & (Permission.ADMINISTRATOR | Permission.SPEAK))
  );
  const permittedToStream = $derived(
    permissionBits === null ||
      Boolean(permissionBits & (Permission.ADMINISTRATOR | Permission.STREAM))
  );
  const permittedToUseSoundboard = $derived(
    soundboardChannelSupported(selectedChannel()?.type, Boolean(callRef)) &&
      permissionBits !== null &&
      (Boolean(permissionBits & Permission.ADMINISTRATOR) ||
        (permissionBits &
          (Permission.VIEW_CHANNEL |
            Permission.CONNECT |
            Permission.SPEAK |
            Permission.USE_SOUNDBOARD)) ===
          (Permission.VIEW_CHANNEL |
            Permission.CONNECT |
            Permission.SPEAK |
            Permission.USE_SOUNDBOARD))
  );
  const canUseExternalSounds = $derived(
    !callRef &&
      permissionBits !== null &&
      Boolean(permissionBits & (Permission.ADMINISTRATOR | Permission.USE_EXTERNAL_SOUNDS))
  );
  const voiceCapabilitySummary = $derived(
    !canConnect
      ? 'You do not have permission to connect'
      : !permittedToSpeak && !permittedToStream
        ? 'You may listen, but cannot speak, use video, or share your screen'
        : !permittedToSpeak
          ? 'You may listen and share video, but cannot speak'
          : !permittedToStream
            ? 'You may speak, but cannot use video or share your screen'
            : 'Join to talk, share video, or present your screen'
  );
  const view = $derived.by(() => {
    // VoiceSession deliberately owns the LiveKit lifecycle outside Svelte's
    // proxy system. Reading the revision here makes its event-driven state
    // changes visible to the template.
    void revision;
    return {
      connected: voice.connected,
      connecting: voice.connecting,
      encrypted: voice.encrypted,
      microphone: voice.microphone,
      deafened: voice.deafened,
      camera: voice.camera,
      screen: voice.screen,
      canSpeak: voice.canSpeak,
      canStream: voice.canStream,
      participants: voice.participants(),
      prioritySpeakers: voice.prioritySpeakers(),
      tiles: voice.tiles()
    };
  });
  const elapsedVoiceTime = $derived(formatVoiceElapsed(startedAt, elapsedClock));
  $effect(() => {
    if (startedAt === null || !Number.isSafeInteger(startedAt) || startedAt <= 0) return;
    elapsedClock = Date.now();
    const timer = window.setInterval(() => (elapsedClock = Date.now()), 1_000);
    return () => window.clearInterval(timer);
  });
  const stageOccupants = $derived(
    occupants.map((occupant) => ({
      ...occupant,
      ...(stageVoiceOverrides[`${occupant.user_id}@${occupant.user_domain}`] ?? {})
    }))
  );
  const participantCards = $derived.by(() => {
    if (!isNativeDesktop() || !view.connected || occupants.length === 0) {
      return view.participants.map((participant) => ({
        ...participant,
        priority: view.prioritySpeakers.has(participant.identity)
      }));
    }
    const localParticipant = view.participants.find((participant) => participant.local);
    return occupants.map((occupant) => {
      const local =
        entities.currentUser?.id === occupant.user_id &&
        entities.currentUser?.origin_domain === occupant.user_domain;
      const priority = view.prioritySpeakers.has(occupant.identity);
      return {
        key: occupant.identity,
        identity: occupant.identity,
        name: stageOccupantName(occupant),
        local,
        speaking: priority || (local && (localParticipant?.speaking ?? false)),
        microphone:
          occupant.can_speak !== false &&
          !occupant.self_mute &&
          !occupant.self_deaf &&
          !occupant.server_mute &&
          !occupant.server_deaf,
        camera: local && (localParticipant?.camera ?? false),
        screen: local && (localParticipant?.screen ?? false),
        priority
      };
    });
  });
  const activePriorityCards = $derived(
    participantCards.filter((participant) => participant.priority)
  );
  const currentStageVoiceState = $derived(
    stageOccupants.find(
      (occupant) =>
        entities.currentUser?.id === occupant.user_id &&
        entities.currentUser?.origin_domain === occupant.user_domain
    ) ?? null
  );
  const targetSoundboardGuildRef = $derived.by(() => {
    const channel = selectedChannel();
    return channel?.guild_id && channel.guild_domain
      ? `${channel.guild_id}@${channel.guild_domain}`
      : null;
  });
  const soundboardUnavailableReason = $derived(
    soundboardPlaybackUnavailableReason({
      connected: view.connected,
      canSpeak: view.canSpeak && currentStageVoiceState?.can_speak !== false,
      selfMuted: !view.microphone || currentStageVoiceState?.self_mute === true,
      selfDeafened: view.deafened,
      serverMuted: currentStageVoiceState?.server_mute,
      serverDeafened: currentStageVoiceState?.server_deaf,
      suppressed: currentStageVoiceState?.suppressed
    })
  );
  const canPlaySoundboard = $derived(
    permittedToUseSoundboard && soundboardUnavailableReason === null
  );
  const visibleSoundboardGroups = $derived(
    soundboardGroups.filter((group) =>
      soundboardSourceAllowed(
        targetSoundboardGuildRef,
        group.key === 'default' ? null : group.key,
        canUseExternalSounds
      )
    )
  );
  const stageSpeakers = $derived(
    stageOccupants.filter((occupant) => occupant.suppressed === false)
  );
  const stageRequesting = $derived(
    stageOccupants.filter(
      (occupant) => occupant.suppressed && Boolean(occupant.request_to_speak_timestamp)
    )
  );
  const stageAudience = $derived(
    stageOccupants.filter((occupant) => occupant.suppressed && !occupant.request_to_speak_timestamp)
  );

  const changed = () => {
    revision += 1;
    error = voice.error;
  };

  function selectedChannel(reference = channelRef) {
    if (!reference) return null;
    return entities.channels.values.find((item) => entityKey(item) === reference) ?? null;
  }

  function stageOccupantName(occupant: VoiceOccupant): string {
    return userDisplayName(entities.users.get(`${occupant.user_id}@${occupant.user_domain}`));
  }

  function isCurrentStageOccupant(occupant: VoiceOccupant): boolean {
    return (
      entities.currentUser?.id === occupant.user_id &&
      entities.currentUser?.origin_domain === occupant.user_domain
    );
  }

  async function updateStageVoiceState(
    occupant: VoiceOccupant | null,
    patch: {
      suppress?: boolean;
      request_to_speak_timestamp?: string | null;
    }
  ) {
    const channel = selectedChannel();
    if (!channel?.guild_id || !channel.guild_domain || stageVoiceBusy) return;
    const self = occupant === null;
    const userRef = self
      ? entities.currentUser
        ? entityRef(entities.currentUser)
        : ''
      : `${occupant.user_id}@${occupant.user_domain}`;
    if (!userRef) return;
    stageVoiceBusy = userRef;
    error = '';
    try {
      const result = await api<Partial<VoiceOccupant>>(
        `/guilds/${encodeURIComponent(`${channel.guild_id}@${channel.guild_domain}`)}/voice-states/${self ? '@me' : encodeURIComponent(userRef)}`,
        { method: 'PATCH', body: JSON.stringify(patch) }
      );
      stageVoiceOverrides = {
        ...stageVoiceOverrides,
        [userRef]: { ...(stageVoiceOverrides[userRef] ?? {}), ...patch, ...result }
      };
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not update this Stage participant.');
    } finally {
      stageVoiceBusy = '';
    }
  }

  function toggleRequestToSpeak() {
    if (
      !currentStageVoiceState?.suppressed ||
      (!currentStageVoiceState.request_to_speak_timestamp && !canRequestToSpeak)
    )
      return;
    void updateStageVoiceState(null, {
      request_to_speak_timestamp: currentStageVoiceState.request_to_speak_timestamp
        ? null
        : new Date().toISOString()
    });
  }

  async function senderDeviceId(channel: Channel): Promise<string | null> {
    if (channel?.encryption_mode !== 'e2ee') return null;
    const user = entities.currentUser;
    if (!user) throw new Error('Sign in again before joining encrypted voice.');
    return (await initializeE2EE(user)).deviceId;
  }

  const moved = (event: Event) => {
    const { grant, channelRef: targetRef } = (
      event as CustomEvent<{ grant: VoiceToken; channelRef: string }>
    ).detail;
    if ((grant.move_session_id ?? null) !== voice.moveSessionId) return;
    const generation = connectionFence.begin();
    joinController?.abort();
    joinController = null;
    void (async () => {
      try {
        const channel = selectedChannel(targetRef);
        if (!channel) throw new Error('The destination voice channel is unavailable.');
        const targetEncrypted = channel.encryption_mode === 'e2ee';
        if (voice.encrypted !== targetEncrypted) {
          await voice.disconnect();
          throw new Error(
            'The destination uses a different voice encryption policy. Review it and join manually.'
          );
        }
        const key = await mediaKey(grant, channel);
        connectionId = grant.connection_id;
        const deviceId = await senderDeviceId(channel);
        if (!mounted || !connectionFence.isCurrent(generation)) return;
        await voice.disconnect();
        if (!mounted || !connectionFence.isCurrent(generation)) return;
        if (isNativeDesktop()) {
          await voice.connectNative(targetRef, false, grant, channel, key, deviceId ?? undefined);
          return;
        }
        await voice.connect(grant, channel, key);
        if (!mounted || !connectionFence.isCurrent(generation)) await voice.disconnect();
      } catch (caught) {
        if (mounted && connectionFence.isCurrent(generation)) {
          error = userErrorMessage(caught, 'Could not move voice rooms. Try joining again.');
        }
      }
    })();
  };

  const soundboardPlayed = (event: Event) => {
    const detail = (
      event as CustomEvent<{
        channel_id?: string;
        channel_domain?: string;
        download_url?: string;
        media_authority?: string;
        media_origin?: string;
        effective_volume?: number;
        sound?: { name?: string; media_hash?: string; content_type?: string };
      }>
    ).detail;
    if (
      !voice.connected ||
      !channelRef ||
      `${detail.channel_id}@${detail.channel_domain}` !== channelRef
    )
      return;
    const expectedChannelRef = channelRef;
    void (async () => {
      try {
        const blob = await loadSoundboardMedia({
          downloadUrl: detail.download_url ?? '',
          authorityDomain: detail.media_authority ?? '',
          mediaOrigin: detail.media_origin ?? '',
          expectedSha256: detail.sound?.media_hash ?? '',
          contentType: detail.sound?.content_type ?? ''
        });
        if (!mounted || !voice.connected || channelRef !== expectedChannelRef) return;
        const objectUrl = URL.createObjectURL(blob);
        const audio = new Audio(objectUrl);
        audio.preload = 'auto';
        audio.volume = Math.min(1, Math.max(0, Number(detail.effective_volume ?? 1)));
        activeSoundboardAudio.add(audio);
        soundboardObjectUrls.set(audio, objectUrl);
        let disposed = false;
        const dispose = () => {
          if (disposed) return;
          disposed = true;
          activeSoundboardAudio.delete(audio);
          soundboardObjectUrls.delete(audio);
          audio.removeAttribute('src');
          audio.load();
          URL.revokeObjectURL(objectUrl);
        };
        audio.addEventListener('ended', dispose, { once: true });
        audio.addEventListener('error', dispose, { once: true });
        await audio.play().catch((caught) => {
          dispose();
          throw caught;
        });
      } catch (caught) {
        if (mounted && voice.connected && channelRef === expectedChannelRef) {
          error = userErrorMessage(
            caught,
            `Could not play ${detail.sound?.name ? `“${detail.sound.name}”` : 'the guild sound'}. Check this app's audio permissions.`
          );
        }
      }
    })();
  };

  const stageChanged = (event: Event) => {
    const detail = (
      event as CustomEvent<{
        eventType: 'STAGE_INSTANCE_CREATE' | 'STAGE_INSTANCE_UPDATE' | 'STAGE_INSTANCE_DELETE';
        stage: StageInstance;
      }>
    ).detail;
    if (!channelRef || `${detail.stage.channel_id}@${detail.stage.channel_domain}` !== channelRef)
      return;
    stageInstance = detail.eventType === 'STAGE_INSTANCE_DELETE' ? null : detail.stage;
    stageLoaded = true;
  };

  onMount(() => {
    mounted = true;
    voice.addEventListener('change', changed);
    window.addEventListener('kaede:voice-token', moved);
    window.addEventListener('kaede:voice-soundboard', soundboardPlayed);
    window.addEventListener('kaede:stage-instance', stageChanged);
    if (audioHost) detachAudio = voice.attachAudio(audioHost);
    if (selectedChannel()?.type === 13) void loadStageInstance();
  });

  async function mediaKey(grant: VoiceToken, channel: Channel): Promise<ArrayBuffer | undefined> {
    expectedVoicePolicy(grant, channel);
    if (!grant.e2ee) return undefined;
    const user = entities.currentUser;
    if (!user) throw new Error('Encrypted room state is unavailable on this device.');
    const client = await initializeE2EE(user);
    await client.syncRoomState(channel);
    return client.exportMediaKey(
      channel,
      [
        'kaede-livekit-key-v1',
        grant.media_protocol,
        grant.media_suite,
        grant.media_session_id,
        grant.media_epoch,
        grant.room
      ].join('\0')
    );
  }

  onDestroy(() => {
    mounted = false;
    connectionFence.invalidate();
    joinController?.abort();
    joinController = null;
    voice.removeEventListener('change', changed);
    window.removeEventListener('kaede:voice-token', moved);
    window.removeEventListener('kaede:voice-soundboard', soundboardPlayed);
    window.removeEventListener('kaede:stage-instance', stageChanged);
    for (const audio of activeSoundboardAudio) {
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
      const objectUrl = soundboardObjectUrls.get(audio);
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    }
    activeSoundboardAudio.clear();
    soundboardObjectUrls.clear();
    detachAudio?.();
    void voice.disconnect();
  });

  async function leave() {
    connectionFence.invalidate();
    joinController?.abort();
    joinController = null;
    await voice.disconnect();
  }

  async function join(takeover = false) {
    if (!mounted) return;
    if (!canJoinVoice) {
      error = isStageChannel
        ? 'This Stage has not started yet.'
        : 'You do not have permission to join this voice channel.';
      return;
    }
    const generation = connectionFence.begin();
    joinController?.abort();
    const controller = new AbortController();
    joinController = controller;
    error = '';
    takeoverPrompt = null;
    try {
      const channel = selectedChannel();
      if (!channel) throw new Error('Voice channel policy is unavailable. Refresh and try again.');
      const deviceId = await senderDeviceId(channel);
      const path = callRef
        ? `/calls/${encodeURIComponent(callRef)}/voice/token`
        : `/channels/${encodeURIComponent(channelRef ?? '')}/voice/token`;
      const grant = await api<VoiceToken>(path, {
        method: 'POST',
        body: JSON.stringify({
          sender_device_id: deviceId,
          connection_id: connectionId,
          takeover,
          client_kind: isNativeDesktop() ? 'desktop' : 'web'
        }),
        signal: controller.signal
      });
      if (!mounted || !connectionFence.isCurrent(generation) || controller.signal.aborted) return;
      const key = await mediaKey(grant, channel);
      if (!mounted || !connectionFence.isCurrent(generation) || controller.signal.aborted) return;
      if (isNativeDesktop()) {
        const reference = callRef ?? channelRef;
        if (!reference) throw new Error('Voice channel is unavailable.');
        await voice.connectNative(
          reference,
          Boolean(callRef),
          grant,
          channel,
          key,
          deviceId ?? undefined,
          takeover
        );
      } else {
        await voice.connect(grant, channel, key);
      }
      if (!mounted || !connectionFence.isCurrent(generation) || controller.signal.aborted) {
        // Native joins have their own generation fence in Rust. A stale
        // invocation may finish after a newer native join has started, so an
        // unconditional leave here could tear down that newer call.
        if (!isNativeDesktop()) await voice.disconnect();
        return;
      }
      if (audioHost) {
        detachAudio?.();
        detachAudio = voice.attachAudio(audioHost);
      }
    } catch (caught) {
      if (mounted && connectionFence.isCurrent(generation) && !controller.signal.aborted) {
        if (caught instanceof ApiError && caught.code === 'VOICE_ACTIVE_ELSEWHERE') {
          const activeClient =
            typeof caught.detail.active_client === 'string' ? caught.detail.active_client : '';
          takeoverPrompt =
            activeClient === 'mobile'
              ? 'your phone or tablet'
              : activeClient === 'desktop'
                ? 'the desktop app'
                : activeClient === 'web'
                  ? 'another browser'
                  : 'another device';
          error = '';
        } else {
          error =
            caught instanceof ApiError
              ? caught.code === 'MISSING_PERMISSIONS'
                ? 'You do not have permission to join this voice channel.'
                : caught.message
              : voice.error ||
                userErrorMessage(
                  caught,
                  'Could not join voice. Check your network and microphone permission, then try again.'
                );
        }
      }
    } finally {
      if (joinController === controller) joinController = null;
    }
  }

  async function safely(action: () => Promise<void>) {
    error = '';
    try {
      await action();
    } catch (caught) {
      if (mounted) error = userErrorMessage(caught, 'Voice control failed. Try again.');
    }
  }

  async function toggleSoundboard() {
    if (!canPlaySoundboard) {
      soundboardOpen = false;
      return;
    }
    soundboardOpen = !soundboardOpen;
    if (!soundboardOpen || soundboardGroups.length || soundboardLoading) return;
    const channel = selectedChannel();
    if (!channel?.guild_id || !channel.guild_domain) return;
    soundboardLoading = true;
    try {
      const guilds = [...entities.guilds.values]
        .filter((guild) =>
          soundboardSourceAllowed(
            `${channel.guild_id}@${channel.guild_domain}`,
            entityRef(guild),
            canUseExternalSounds
          )
        )
        .sort((left, right) => {
          const leftCurrent =
            left.id === channel.guild_id && left.origin_domain === channel.guild_domain;
          const rightCurrent =
            right.id === channel.guild_id && right.origin_domain === channel.guild_domain;
          if (leftCurrent !== rightCurrent) return leftCurrent ? -1 : 1;
          return left.name.localeCompare(right.name);
        });
      const [defaultResult, ...guildResults] = await Promise.allSettled([
        api<SoundboardSound[]>('/soundboard-default-sounds'),
        ...guilds.map((guild) =>
          api<{ items: SoundboardSound[] }>(
            `/guilds/${encodeURIComponent(entityRef(guild))}/soundboard-sounds`
          )
        )
      ]);
      if (
        defaultResult.status === 'rejected' &&
        guildResults.every((result) => result.status === 'rejected')
      ) {
        throw defaultResult.reason;
      }
      const groups: SoundboardGroup[] = [];
      if (defaultResult.status === 'fulfilled' && defaultResult.value.length) {
        groups.push({ key: 'default', label: 'Discord Sounds', sounds: defaultResult.value });
      }
      guildResults.forEach((result, index) => {
        if (result.status !== 'fulfilled' || !result.value.items.length) return;
        const source = guilds[index];
        groups.push({
          key: entityRef(source),
          label:
            source.id === channel.guild_id && source.origin_domain === channel.guild_domain
              ? `${source.name} · Current server`
              : source.name,
          sounds: result.value.items
        });
      });
      soundboardGroups = groups;
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not load available soundboard sounds.');
      soundboardOpen = false;
    } finally {
      soundboardLoading = false;
    }
  }

  async function sendSoundboardSound(sound: SoundboardSound) {
    const sourceGuildRef =
      sound.guild_id && sound.guild_domain ? `${sound.guild_id}@${sound.guild_domain}` : null;
    if (
      !channelRef ||
      soundboardBusy ||
      !canPlaySoundboard ||
      !soundboardSourceAllowed(targetSoundboardGuildRef, sourceGuildRef, canUseExternalSounds)
    )
      return;
    soundboardBusy = entityRef(sound);
    try {
      await api(`/channels/${encodeURIComponent(channelRef)}/send-soundboard-sound`, {
        method: 'POST',
        body: JSON.stringify({
          sound_id: entityRef(sound),
          sound_version: sound.version,
          ...(sound.guild_id && sound.guild_domain
            ? { source_guild_id: `${sound.guild_id}@${sound.guild_domain}` }
            : {})
        })
      });
      soundboardOpen = false;
    } catch (caught) {
      error = userErrorMessage(caught, `Could not play “${sound.name}” in voice.`);
    } finally {
      soundboardBusy = '';
    }
  }

  async function loadStageInstance() {
    if (!channelRef || stageLoading) return;
    stageLoading = true;
    try {
      stageInstance = await api<StageInstance>(
        `/stage-instances/${encodeURIComponent(channelRef)}`
      );
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 404) {
        stageInstance = null;
      } else {
        error = userErrorMessage(caught, 'Could not load this Stage.');
      }
    } finally {
      stageLoading = false;
      stageLoaded = true;
    }
  }

  async function startStage() {
    if (!channelRef || !canManageStage || stageLoading) return;
    const topic = prompt('What is this Stage about?', stageInstance?.topic ?? '')?.trim();
    if (!topic) return;
    if (topic.length > 120) {
      error = 'Stage topics can be at most 120 characters.';
      return;
    }
    stageLoading = true;
    error = '';
    try {
      const sendStartNotification =
        canNotifyStage && confirm('Notify everyone in this server that the Stage is starting?');
      stageInstance = await api<StageInstance>('/stage-instances', {
        method: 'POST',
        body: JSON.stringify({
          channel_id: channelRef,
          topic,
          privacy_level: 2,
          send_start_notification: sendStartNotification
        })
      });
      stageLoaded = true;
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not start this Stage.');
    } finally {
      stageLoading = false;
    }
  }

  async function editStageTopic() {
    if (!channelRef || !stageInstance || !canManageStage || stageLoading) return;
    const topic = prompt('Stage topic', stageInstance.topic)?.trim();
    if (!topic || topic === stageInstance.topic) return;
    if (topic.length > 120) {
      error = 'Stage topics can be at most 120 characters.';
      return;
    }
    stageLoading = true;
    error = '';
    try {
      stageInstance = await api<StageInstance>(
        `/stage-instances/${encodeURIComponent(channelRef)}`,
        { method: 'PATCH', body: JSON.stringify({ topic }) }
      );
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not update the Stage topic.');
    } finally {
      stageLoading = false;
    }
  }

  async function endStage() {
    if (
      !channelRef ||
      !stageInstance ||
      !canManageStage ||
      stageLoading ||
      !confirm('End this Stage for everyone?')
    )
      return;
    stageLoading = true;
    error = '';
    try {
      await api(`/stage-instances/${encodeURIComponent(channelRef)}`, { method: 'DELETE' });
      stageInstance = null;
      stageLoaded = true;
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not end this Stage.');
    } finally {
      stageLoading = false;
    }
  }

  async function startScreenShare(preferences: MediaQualityPreferences, sourceId: string | null) {
    await voice.startScreenShare(preferences, sourceId);
  }
</script>

{#snippet stageGroup(title: string, people: VoiceOccupant[])}
  {#if people.length}
    <section class="stage-participant-group" aria-label={title}>
      <h3>{title} — {people.length}</h3>
      <div class="stage-participant-list">
        {#each people as occupant (`${occupant.user_id}@${occupant.user_domain}`)}
          {@const self = isCurrentStageOccupant(occupant)}
          <article class="stage-participant">
            <span class="stage-participant-avatar" aria-hidden="true">
              {stageOccupantName(occupant).slice(0, 1).toUpperCase()}
            </span>
            <div>
              <strong>{stageOccupantName(occupant)}</strong>
              {#if self}<small>You</small>{/if}
            </div>
            {#if canModerateStage && !self}
              <button
                class="secondary"
                disabled={Boolean(stageVoiceBusy)}
                onclick={() =>
                  void updateStageVoiceState(occupant, { suppress: !occupant.suppressed })}
              >
                {occupant.suppressed ? 'Invite to speak' : 'Move to audience'}
              </button>
            {/if}
          </article>
        {/each}
      </div>
    </section>
  {/if}
{/snippet}

<section class="voice-panel" aria-label="Voice channel">
  <header class="voice-heading">
    <div class="voice-status">
      <span class:connected={view.connected} class="status-dot" aria-hidden="true"></span>
      <div>
        <strong>
          {isStageChannel
            ? (stageInstance?.topic ?? 'Stage channel')
            : view.connected
              ? 'Voice connected'
              : 'Voice channel'}
        </strong>
        <span>
          {isStageChannel && !stageLoaded
            ? 'Loading Stage…'
            : isStageChannel && !stageInstance
              ? 'The Stage has not started'
              : view.connected
                ? `${participantCards.length} ${participantCards.length === 1 ? 'participant' : 'participants'} · ${view.encrypted ? 'End-to-end encrypted' : 'Not end-to-end encrypted'}${elapsedVoiceTime ? ` · ${elapsedVoiceTime}` : ''}`
                : elapsedVoiceTime
                  ? `Active for ${elapsedVoiceTime} · ${voiceCapabilitySummary}`
                  : voiceCapabilitySummary}
        </span>
      </div>
    </div>
    <div class="voice-heading-actions">
      {#if canManageStage && stageLoaded}
        {#if stageInstance}
          <button class="secondary" disabled={stageLoading} onclick={editStageTopic}
            >Edit topic</button
          >
          <button class="danger" disabled={stageLoading} onclick={endStage}>End Stage</button>
        {:else}
          <button class="primary" disabled={stageLoading} onclick={startStage}>Start Stage</button>
        {/if}
      {/if}
      {#if !view.connected && (!isStageChannel || stageInstance)}
        <button
          class="primary"
          disabled={view.connecting || !canJoinVoice}
          title={!canConnect ? 'You do not have permission to join this voice channel.' : undefined}
          onclick={() => join()}
        >
          {view.connecting ? 'Connecting…' : isStageChannel ? 'Join audience' : 'Join voice'}
        </button>
      {/if}
    </div>
  </header>

  <div class="audio-host" bind:this={audioHost}></div>

  <main class="voice-stage">
    {#if error}<p class="voice-error" role="alert">{error}</p>{/if}
    {#if takeoverPrompt}
      <div class="takeover-prompt" role="alertdialog" aria-labelledby="voice-takeover-title">
        <span><Icon name="screen" size={28} /></span>
        <strong id="voice-takeover-title">Voice is active on {takeoverPrompt}</strong>
        <p>Moving voice here will disconnect that device. It will not reconnect automatically.</p>
        <div class="takeover-actions">
          <button class="primary" disabled={view.connecting} onclick={() => join(true)}>
            {view.connecting ? 'Moving voice…' : 'Move voice here'}
          </button>
          <button class="secondary" onclick={() => (takeoverPrompt = null)}>Keep it there</button>
        </div>
      </div>
    {:else if view.connected}
      {#if isStageChannel}
        <div class="stage-roster">
          {@render stageGroup('Speakers', stageSpeakers)}
          {@render stageGroup('Requested to speak', stageRequesting)}
          {@render stageGroup('Audience', stageAudience)}
          {#if !stageOccupants.length}
            <div class="join-prompt">
              <strong>No one else is here yet</strong>
              <p>Stage participants will appear here as they join.</p>
            </div>
          {/if}
        </div>
      {:else if view.tiles.length > 0}
        <div
          class="video-grid"
          class:has-screen={view.tiles.some((tile) => tile.source === 'screen_share')}
        >
          {#if activePriorityCards.length}
            <div class="priority-speaker-roster" role="status">
              <Icon name="megaphone" size={17} />
              <span>
                {activePriorityCards.map((participant) => participant.name).join(', ')}
                {activePriorityCards.length === 1 ? ' is' : ' are'} speaking with priority
              </span>
            </div>
          {/if}
          {#each view.tiles as tile (tile.key)}
            <article
              class:screen-tile={tile.source === 'screen_share'}
              class:priority-speaker={view.prioritySpeakers.has(tile.identity)}
              class="video-tile"
            >
              {#if view.prioritySpeakers.has(tile.identity)}
                <span
                  class="priority-speaker-cue"
                  title="Priority speaker"
                  aria-label="Priority speaker"
                >
                  <Icon name="megaphone" size={17} />
                </span>
              {/if}
              <div class="video-host" use:attachVideo={tile}></div>
              <span class="video-tile-name">{tile.name}{tile.local ? ' (you)' : ''}</span>
            </article>
          {/each}
        </div>
      {:else}
        <div class="participant-grid">
          {#each participantCards as participant (participant.key)}
            <article
              class:speaking={participant.speaking}
              class:priority-speaker={participant.priority}
              class="participant-card"
            >
              {#if participant.priority}
                <span
                  class="priority-speaker-cue"
                  title="Priority speaker"
                  aria-label="Priority speaker"
                >
                  <Icon name="megaphone" size={17} />
                </span>
              {/if}
              <div class="participant-avatar" aria-hidden="true">
                {participant.name.slice(0, 1).toUpperCase()}
              </div>
              <div class="participant-name">
                <strong>{participant.name}</strong>
                {#if participant.local}<span>You</span>{/if}
              </div>
              <span
                class:muted={!participant.microphone}
                class="participant-mic"
                title={participant.microphone ? 'Microphone on' : 'Muted'}
              >
                <Icon name={participant.microphone ? 'microphone' : 'microphone-off'} size={17} />
              </span>
            </article>
          {/each}
        </div>
      {/if}
    {:else if isStageChannel && stageLoaded && !stageInstance}
      <div class="join-prompt">
        <span><Icon name="microphone" size={28} /></span>
        <strong>This Stage hasn’t started yet</strong>
        <p>
          {canManageStage
            ? 'Start the Stage when you are ready to bring the audience in.'
            : 'Check back when a moderator starts the Stage.'}
        </p>
        {#if canManageStage}
          <button class="primary" disabled={stageLoading} onclick={startStage}>Start Stage</button>
        {/if}
      </div>
    {:else if !canConnect}
      <div class="join-prompt permission-prompt">
        <span><Icon name="lock" size={26} /></span>
        <strong>You cannot join this voice channel</strong>
        <p>Your roles do not include the Connect permission for this channel.</p>
      </div>
    {:else}
      <div class="join-prompt">
        <span><Icon name="volume" size={28} /></span>
        <strong>Ready when you are</strong>
        <p>Join the room to talk with everyone already here.</p>
      </div>
    {/if}
  </main>

  {#if view.connected}
    {#if isStageChannel && currentStageVoiceState}
      <div class="stage-self-actions" role="status">
        {#if currentStageVoiceState.suppressed}
          <button
            class="secondary"
            disabled={(!currentStageVoiceState.request_to_speak_timestamp && !canRequestToSpeak) ||
              Boolean(stageVoiceBusy)}
            title={!currentStageVoiceState.request_to_speak_timestamp && !canRequestToSpeak
              ? 'You do not have permission to request to speak.'
              : undefined}
            onclick={toggleRequestToSpeak}
          >
            {currentStageVoiceState.request_to_speak_timestamp
              ? 'Cancel request'
              : 'Request to speak'}
          </button>
        {:else}
          <button
            class="secondary"
            disabled={Boolean(stageVoiceBusy)}
            onclick={() => void updateStageVoiceState(null, { suppress: true })}
            >Move to audience</button
          >
        {/if}
      </div>
    {/if}
    {#if !view.canSpeak || !view.canStream}
      <div class="voice-permission-notice" role="status">
        {#if isStageChannel && currentStageVoiceState?.suppressed}
          You are listening from the audience. A Stage moderator can invite you to speak.
        {:else if !view.canSpeak && !view.canStream}
          You can listen, but your roles do not allow speaking, camera, or screen sharing here.
        {:else if !view.canSpeak}
          You can listen and share video, but your roles do not allow speaking here.
        {:else}
          You can speak, but your roles do not allow camera or screen sharing here.
        {/if}
      </div>
    {/if}
    <footer class="voice-dock" aria-label="Voice controls">
      <button
        class:active={view.camera}
        class="control-button"
        disabled={!view.canStream}
        aria-pressed={view.camera}
        aria-label={view.camera ? 'Turn camera off' : 'Turn camera on'}
        title={!view.canStream
          ? 'You do not have permission to use video in this channel.'
          : view.camera
            ? 'Camera off'
            : 'Camera on'}
        onclick={() => safely(() => voice.toggleCamera())}
      >
        <Icon name={view.camera ? 'video' : 'video-off'} size={21} />
      </button>
      <button
        class:active={view.screen}
        class="control-button"
        disabled={!view.canStream}
        aria-pressed={view.screen}
        aria-label={view.screen ? 'Stop sharing screen' : 'Share screen'}
        title={!view.canStream
          ? 'You do not have permission to share your screen in this channel.'
          : view.screen
            ? 'Stop sharing'
            : 'Share screen'}
        onclick={() => {
          if (view.screen) void safely(() => voice.stopScreenShare());
          else screenShareOpen = true;
        }}
      >
        <Icon name="screen" size={21} />
      </button>
      {#if onApps}
        <button
          class="control-button"
          type="button"
          aria-label="Open Apps"
          aria-haspopup="dialog"
          title="Apps"
          onclick={onApps}
        >
          <Icon name="sparkles" size={20} />
        </button>
      {/if}
      <button
        class:off={!view.microphone}
        class="control-button"
        disabled={!view.canSpeak}
        aria-pressed={view.microphone}
        aria-label={view.microphone ? 'Mute microphone' : 'Unmute microphone'}
        title={!view.canSpeak
          ? 'You do not have permission to speak in this channel.'
          : view.microphone
            ? 'Mute'
            : 'Unmute'}
        onclick={() => safely(() => voice.toggleMicrophone())}
      >
        <Icon name={view.microphone ? 'microphone' : 'microphone-off'} size={20} />
      </button>
      <button
        class:off={view.deafened}
        class="control-button"
        aria-pressed={view.deafened}
        aria-label={view.deafened ? 'Undeafen' : 'Deafen'}
        title={view.deafened ? 'Undeafen' : 'Deafen'}
        onclick={() => safely(() => voice.toggleDeafen())}
      >
        <Icon name={view.deafened ? 'headphones-off' : 'headphones'} size={20} />
      </button>
      {#if permittedToUseSoundboard}
        <div class="soundboard-control">
          <button
            class:active={soundboardOpen}
            class="control-button"
            disabled={!canPlaySoundboard}
            aria-expanded={soundboardOpen && canPlaySoundboard}
            aria-label="Open soundboard"
            title={soundboardUnavailableReason ?? 'Soundboard'}
            onclick={() => void toggleSoundboard()}
          >
            <Icon name="music" size={20} />
          </button>
          {#if soundboardOpen && canPlaySoundboard}
            <div class="soundboard-popover" aria-label="Soundboard sounds">
              <strong>Soundboard</strong>
              {#if soundboardLoading}
                <span>Loading sounds…</span>
              {:else if visibleSoundboardGroups.length}
                {#each visibleSoundboardGroups as group (group.key)}
                  <h4>{group.label}</h4>
                  {#each group.sounds as sound (entityRef(sound))}
                    <button
                      type="button"
                      disabled={Boolean(soundboardBusy)}
                      onclick={() => void sendSoundboardSound(sound)}
                    >
                      <span aria-hidden="true">{sound.emoji_name || '♫'}</span>
                      {sound.name}
                    </button>
                  {/each}
                {/each}
              {:else}
                <span>No soundboard sounds are available.</span>
              {/if}
            </div>
          {/if}
        </div>
      {/if}
      <span class="control-divider" aria-hidden="true"></span>
      <button
        class="control-button danger"
        aria-label={isStageChannel ? 'Exit Quietly' : 'Leave voice'}
        title={isStageChannel ? 'Exit Quietly' : 'Leave voice'}
        onclick={() => safely(leave)}
      >
        <Icon name="phone-off" size={21} />
      </button>
    </footer>
  {/if}
</section>

<ScreenShareDialog bind:open={screenShareOpen} onShare={startScreenShare} />

<style>
  .voice-panel {
    display: grid;
    width: 100%;
    height: 100%;
    min-height: 0;
    grid-template-rows: auto minmax(0, 1fr) auto;
    gap: 0;
    overflow: hidden;
    background: var(--paper);
  }

  .soundboard-control {
    position: relative;
  }

  .soundboard-popover {
    position: absolute;
    right: 0;
    bottom: calc(100% + 0.65rem);
    z-index: 4;
    display: grid;
    width: min(19rem, calc(100vw - 2rem));
    max-height: 18rem;
    gap: 0.35rem;
    overflow-y: auto;
    border: 1px solid var(--line);
    border-radius: 11px;
    padding: 0.65rem;
    background: var(--paper-raised);
    box-shadow: var(--shadow-lg);
  }

  .soundboard-popover > strong,
  .soundboard-popover > span {
    padding: 0.3rem 0.4rem;
    font-size: 0.76rem;
  }

  .soundboard-popover > span {
    color: var(--text-muted);
  }

  .soundboard-popover > h4 {
    margin: 0.35rem 0.4rem 0.1rem;
    color: var(--text-muted);
    font-size: 0.68rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .soundboard-popover > button {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    border: 0;
    border-radius: 8px;
    padding: 0.5rem 0.6rem;
    color: var(--ink);
    background: transparent;
    text-align: left;
    cursor: pointer;
  }

  .soundboard-popover > button:hover:not(:disabled) {
    background: var(--surface-subtle);
  }

  .voice-permission-notice {
    border-top: 1px solid var(--line-soft);
    padding: 0.65rem 1rem;
    color: var(--text-muted);
    background: color-mix(in srgb, var(--surface-subtle) 82%, transparent);
    font-size: 0.78rem;
    text-align: center;
  }

  .permission-prompt span {
    color: var(--text-muted);
  }

  .voice-heading {
    display: flex;
    min-width: 0;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--line);
    padding: 0.85rem clamp(1rem, 2.5vw, 1.6rem);
    background: color-mix(in srgb, var(--paper-raised) 42%, var(--paper));
  }

  .voice-status {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 0.7rem;
  }

  .voice-heading-actions {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    gap: 0.45rem;
  }

  .voice-heading-actions .danger {
    border: 1px solid color-mix(in srgb, var(--danger) 45%, var(--line));
    border-radius: 9px;
    padding: 0.52rem 0.78rem;
    color: var(--danger);
    background: transparent;
    font: inherit;
    font-size: 0.74rem;
    font-weight: 750;
    cursor: pointer;
  }

  .voice-heading-actions button:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }

  .voice-status > div {
    display: grid;
    min-width: 0;
    gap: 0.12rem;
  }

  .voice-status strong,
  .voice-status span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .voice-status strong {
    font-size: 0.88rem;
  }

  .voice-status span {
    color: var(--ink-soft);
    font-size: 0.72rem;
  }

  .status-dot {
    width: 0.62rem;
    height: 0.62rem;
    flex: 0 0 auto;
    border-radius: 999px;
    background: var(--text-muted);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--text-muted) 12%, transparent);
  }

  .status-dot.connected {
    background: var(--pine);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--pine) 14%, transparent);
  }

  .primary {
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 0.52rem 0.78rem;
    background: var(--maple);
    color: var(--on-accent);
    border-color: var(--maple);
    font-size: 0.74rem;
    font-weight: 720;
    cursor: pointer;
    transition: filter 140ms ease;
  }

  .primary:hover:not(:disabled) {
    filter: brightness(1.08);
  }

  button:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }

  .voice-error {
    position: absolute;
    top: 1rem;
    left: 50%;
    z-index: 2;
    width: min(32rem, calc(100% - 2rem));
    transform: translateX(-50%);
    margin: 0;
    border: 1px solid color-mix(in srgb, var(--danger) 40%, var(--line));
    border-radius: 11px;
    padding: 0.65rem 0.8rem;
    color: var(--danger);
    background: color-mix(in srgb, var(--danger) 12%, var(--paper));
    font-size: 0.8rem;
  }

  .audio-host {
    display: none;
  }

  .voice-stage {
    position: relative;
    display: grid;
    min-height: 0;
    place-items: center;
    overflow: auto;
    padding: clamp(1rem, 3vw, 2.5rem);
    background:
      radial-gradient(
        circle at 50% 42%,
        color-mix(in srgb, var(--maple) 5%, transparent),
        transparent 32rem
      ),
      var(--paper);
  }

  .video-grid {
    display: grid;
    width: min(100%, 76rem);
    min-height: 0;
    grid-template-columns: repeat(auto-fit, minmax(min(280px, 100%), 1fr));
    gap: 0.8rem;
  }

  .video-grid.has-screen {
    grid-template-columns: minmax(0, 2fr) minmax(220px, 1fr);
  }

  .priority-speaker-roster {
    display: flex;
    grid-column: 1 / -1;
    align-items: center;
    gap: 0.5rem;
    border: 1px solid color-mix(in srgb, var(--maple) 50%, var(--line));
    border-radius: 10px;
    padding: 0.55rem 0.75rem;
    color: var(--ink);
    background: color-mix(in srgb, var(--maple) 12%, var(--paper-raised));
    font-size: 0.76rem;
    font-weight: 700;
  }

  .video-tile {
    position: relative;
    min-height: 210px;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 14px;
    background: #121211;
    box-shadow: 0 12px 30px rgb(0 0 0 / 14%);
  }

  .video-tile.screen-tile {
    grid-row: span 2;
    min-height: min(420px, 48vh);
  }

  .video-tile.priority-speaker {
    border-color: var(--maple);
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--maple) 55%, transparent);
  }

  .video-host {
    width: 100%;
    height: 100%;
  }

  .video-host :global(video) {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .video-host :global(canvas) {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .screen-tile .video-host :global(video) {
    object-fit: contain;
  }

  .video-tile-name {
    position: absolute;
    left: 0.7rem;
    bottom: 0.7rem;
    padding: 0.25rem 0.5rem;
    border-radius: 999px;
    background: rgb(0 0 0 / 72%);
    color: white;
    font-size: 0.72rem;
    font-weight: 650;
    backdrop-filter: blur(8px);
  }

  .participant-grid {
    display: grid;
    width: min(100%, 64rem);
    grid-template-columns: repeat(auto-fit, minmax(min(230px, 100%), 280px));
    justify-content: center;
    gap: 0.8rem;
  }

  .stage-roster {
    display: grid;
    width: min(100%, 58rem);
    gap: 1.25rem;
    align-self: start;
  }

  .stage-participant-group {
    display: grid;
    gap: 0.55rem;
  }

  .stage-participant-group h3 {
    margin: 0;
    color: var(--ink-soft);
    font-size: 0.72rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .stage-participant-list {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(240px, 100%), 1fr));
    gap: 0.55rem;
  }

  .stage-participant {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.65rem;
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 0.65rem;
    background: color-mix(in srgb, var(--paper-raised) 72%, var(--paper));
  }

  .stage-participant-avatar {
    display: grid;
    width: 2.25rem;
    height: 2.25rem;
    place-items: center;
    border-radius: 50%;
    color: var(--on-accent);
    background: var(--maple);
    font-weight: 800;
  }

  .stage-participant > div {
    display: grid;
    min-width: 0;
    gap: 0.1rem;
  }

  .stage-participant strong {
    overflow: hidden;
    font-size: 0.78rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .stage-participant small {
    color: var(--ink-soft);
    font-size: 0.65rem;
  }

  .stage-self-actions {
    display: flex;
    justify-content: center;
    border-top: 1px solid var(--line-soft);
    padding: 0.6rem 1rem;
    background: color-mix(in srgb, var(--surface-subtle) 82%, transparent);
  }

  .participant-card {
    position: relative;
    display: grid;
    aspect-ratio: 4 / 3;
    place-items: center;
    align-content: center;
    gap: 0.85rem;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 1.25rem;
    background: color-mix(in srgb, var(--paper-raised) 72%, var(--paper));
    box-shadow: 0 8px 22px rgb(0 0 0 / 10%);
    transition:
      border-color 120ms ease,
      box-shadow 120ms ease;
  }

  .participant-card.speaking {
    border-color: var(--pine);
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--pine) 50%, transparent);
  }

  .participant-card.priority-speaker {
    border-color: var(--maple);
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--maple) 55%, transparent);
  }

  .priority-speaker-cue {
    position: absolute;
    top: 0.8rem;
    right: 0.8rem;
    display: grid;
    width: 2rem;
    height: 2rem;
    place-items: center;
    border-radius: 999px;
    color: var(--on-accent);
    background: var(--maple);
    box-shadow: 0 4px 12px rgb(0 0 0 / 18%);
  }

  .participant-avatar {
    display: grid;
    width: clamp(4rem, 7vw, 5.25rem);
    height: clamp(4rem, 7vw, 5.25rem);
    place-items: center;
    border-radius: 50%;
    color: var(--on-accent);
    background: linear-gradient(
      145deg,
      var(--maple),
      color-mix(in srgb, var(--maple) 62%, #5f426f)
    );
    font-size: 1.65rem;
    font-weight: 800;
    box-shadow: inset 0 0 0 1px rgb(255 255 255 / 13%);
  }

  .participant-name {
    display: flex;
    max-width: 100%;
    align-items: center;
    gap: 0.4rem;
  }

  .participant-name strong {
    overflow: hidden;
    font-size: 0.86rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .participant-name span {
    border-radius: 999px;
    padding: 0.15rem 0.38rem;
    color: var(--ink-soft);
    background: var(--paper);
    font-size: 0.6rem;
    font-weight: 700;
  }

  .participant-mic {
    position: absolute;
    right: 0.7rem;
    bottom: 0.7rem;
    display: grid;
    width: 1.85rem;
    height: 1.85rem;
    place-items: center;
    border: 1px solid var(--line);
    border-radius: 50%;
    color: var(--pine);
    background: var(--paper);
  }

  .participant-mic.muted {
    color: var(--danger);
  }

  .join-prompt {
    display: grid;
    max-width: 26rem;
    place-items: center;
    gap: 0.55rem;
    text-align: center;
  }

  .takeover-prompt {
    display: grid;
    width: min(100%, 30rem);
    place-items: center;
    gap: 0.7rem;
    border: 1px solid color-mix(in srgb, var(--maple) 45%, var(--line));
    border-radius: 14px;
    padding: 1.4rem;
    background: var(--paper-raised);
    text-align: center;
    box-shadow: 0 14px 34px rgb(0 0 0 / 16%);
  }

  .takeover-prompt > span {
    color: var(--maple);
  }

  .takeover-prompt p {
    margin: 0;
    color: var(--ink-soft);
    font-size: 0.8rem;
  }

  .takeover-actions {
    display: flex;
    gap: 0.6rem;
    margin-top: 0.35rem;
  }

  .secondary {
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 0.52rem 0.78rem;
    color: var(--ink);
    background: var(--paper);
    font-size: 0.74rem;
    font-weight: 700;
    cursor: pointer;
  }

  .join-prompt > span {
    display: grid;
    width: 4rem;
    height: 4rem;
    place-items: center;
    margin-bottom: 0.25rem;
    border-radius: 50%;
    color: var(--maple);
    background: color-mix(in srgb, var(--maple) 12%, var(--paper-raised));
  }

  .join-prompt strong {
    font-size: 1rem;
  }

  .join-prompt p {
    margin: 0;
    color: var(--ink-soft);
    font-size: 0.78rem;
  }

  .voice-dock {
    display: flex;
    align-items: center;
    justify-self: center;
    gap: 0.4rem;
    width: fit-content;
    max-width: 100%;
    margin: 0 auto 1.15rem;
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 0.4rem;
    background: color-mix(in srgb, var(--paper-raised) 94%, transparent);
    box-shadow: 0 10px 26px rgb(0 0 0 / 16%);
    backdrop-filter: blur(14px);
  }

  .control-button {
    display: grid;
    width: 2.85rem;
    height: 2.85rem;
    flex: 0 0 auto;
    place-items: center;
    border: 0;
    border-radius: 50%;
    padding: 0;
    color: var(--ink);
    background: color-mix(in srgb, var(--ink) 8%, transparent);
    cursor: pointer;
    transition:
      background-color 120ms ease,
      color 120ms ease,
      transform 120ms ease;
  }

  .control-button:hover:not(:disabled) {
    background: color-mix(in srgb, var(--ink) 14%, transparent);
    transform: translateY(-1px);
  }

  .control-button.active {
    color: var(--on-accent);
    background: var(--maple);
  }

  .control-button.off,
  .control-button.danger {
    color: white;
    background: var(--danger);
  }

  .control-button.danger:hover:not(:disabled),
  .control-button.off:hover:not(:disabled) {
    background: color-mix(in srgb, var(--danger) 82%, black);
  }

  .control-divider {
    width: 1px;
    height: 1.7rem;
    margin-inline: 0.12rem;
    background: var(--line);
  }

  @media (max-width: 720px) {
    .voice-heading {
      padding-inline: 0.85rem;
    }

    .voice-status span {
      max-width: 44vw;
    }

    .voice-stage {
      padding: 0.85rem;
    }

    .video-grid.has-screen {
      grid-template-columns: 1fr;
    }

    .video-tile.screen-tile {
      min-height: 260px;
    }

    .participant-grid {
      grid-template-columns: repeat(auto-fit, minmax(min(150px, 100%), 1fr));
    }

    .voice-dock {
      margin-bottom: 0.75rem;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .primary,
    .participant-card,
    .control-button {
      transition: none;
    }
  }
</style>
