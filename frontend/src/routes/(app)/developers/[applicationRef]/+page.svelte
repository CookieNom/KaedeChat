<script lang="ts">
  import { api, userErrorMessage } from '$lib/api/client';
  import ApplicationDirectorySettings from '$lib/components/ApplicationDirectorySettings.svelte';
  import ApplicationMediaManager from '$lib/components/ApplicationMediaManager.svelte';
  import PermissionChecklist from '$lib/components/PermissionChecklist.svelte';
  import type {
    DirectoryExternalLink,
    DirectoryLocale,
    DirectoryMediaInput,
    DirectoryPreviewResponse,
    DirectoryReadinessKey
  } from '$lib/chat/application-directory';
  import {
    directorySettingsPayload,
    syncDirectoryMediaWithAssets,
    type ApplicationAsset
  } from '$lib/chat/application-directory-editor';
  import { permissionMask } from '$lib/chat/permission-selection';
  import { BOT_INTENT_NAMES } from '$lib/generated/ops';
  import { resolve } from '$app/paths';
  import { onDestroy } from 'svelte';

  let { data } = $props<{ data: { applicationRef: string } }>();
  const ref = $derived(data.applicationRef);
  interface Application {
    origin_domain: string;
    ref: string;
    name: string;
    description: string | null;
    support_url: string | null;
    privacy_url: string | null;
    terms_url: string | null;
    directory_enabled: boolean;
    directory_approved: boolean;
    directory_summary: string | null;
    directory_category:
      'entertainment' | 'games' | 'moderation' | 'productivity' | 'social' | 'utilities' | null;
    directory_tags: string[];
    directory_media: DirectoryMediaInput[];
    directory_external_links: DirectoryExternalLink[];
    directory_supported_locales: DirectoryLocale[];
    directory_description_localizations: Partial<Record<DirectoryLocale, string>>;
    status: string;
    target_policy: string;
    default_scopes: string[];
    default_intents: string[];
    default_permissions: string;
    supported_install_types: Array<'guild_install' | 'user_install'>;
    user_install_scopes: string[];
    user_install_contexts: Array<'guild' | 'bot_dm' | 'private_channel'>;
    e2ee_modes: string[];
    bot_user: { handle: string };
  }
  interface Credential {
    id: string;
    label: string;
    token_hint: string;
    scopes: string[];
    created_at: string;
    last_used_at: string | null;
    revoked_at: string | null;
  }
  interface Worker {
    id: string;
    name: string;
    scopes: string[];
    intents: string[];
    target_domains: string[];
    revoked_at: string | null;
  }
  interface Template {
    id: string;
    slug: string;
    name: string;
    description: string | null;
    scopes: string[];
    intents: string[];
    permissions: string;
    e2ee_mode: string;
    active: boolean;
    invite_url: string;
  }
  interface Installation {
    id: string;
    guild_ref: string;
    status: string;
    scopes: string[];
    intents: string[];
    permissions: string;
    channel_restrictions: string[];
    e2ee_mode: string;
    grant_revision: string;
  }
  interface Rule {
    target_domain: string;
    effect: 'allow' | 'deny';
  }

  const scopes = [
    'applications.assets.manage',
    'applications.commands',
    'applications.emojis.manage',
    'interactions.respond',
    'audit_logs.read',
    'automod.executions.read',
    'automod.rules.read',
    'automod.rules.manage',
    'guilds.read',
    'guilds.manage',
    'guilds.assets.manage',
    'channels.read',
    'channels.manage',
    'channels.overwrites.read',
    'channels.overwrites.manage',
    'members.read',
    'roles.read',
    'roles.manage',
    'events.read',
    'events.manage',
    'expressions.read',
    'expressions.manage',
    'installations.read',
    'integrations.read',
    'integrations.manage',
    'messages.metadata',
    'messages.content',
    'messages.history',
    'messages.send',
    'messages.edit.own',
    'messages.delete.own',
    'messages.manage',
    'tasks.read',
    'tasks.write',
    'tasks.manage',
    'attachments.read',
    'attachments.write',
    'reactions.read',
    'reactions.write',
    'polls.read',
    'polls.write',
    'moderation.bans',
    'moderation.members',
    'moderation.messages',
    'moderation.prune',
    'soundboard.read',
    'soundboard.use',
    'soundboard.manage',
    'voice.states.read',
    'voice.connect',
    'voice.listen',
    'voice.speak',
    'voice.stream',
    'voice.moderate',
    'invites.read',
    'invites.manage',
    'webhooks.read',
    'webhooks.manage',
    'emojis.manage',
    'dm.send'
  ];
  const intents = BOT_INTENT_NAMES;
  const userInstallScopes = [
    'applications.commands',
    'interactions.respond',
    'attachments.read',
    'attachments.write'
  ];
  const requiredUserInstallScopes = new Set(['applications.commands', 'interactions.respond']);
  const installTypes = [
    ['guild_install', 'Guild install'],
    ['user_install', 'User install']
  ] as const;
  const userInstallContexts = [
    ['guild', 'Servers'],
    ['bot_dm', 'App direct messages'],
    ['private_channel', 'Group and user direct messages']
  ] as const;
  const readinessLabels: Record<DirectoryReadinessKey, string> = {
    directory_enabled: 'Directory listing enabled',
    summary: 'Summary',
    category: 'Category',
    tags: 'One to five tags',
    description: 'Description',
    support_url: 'Support URL',
    privacy_url: 'Privacy policy URL',
    terms_url: 'Terms of service URL',
    media: 'Product-page media',
    external_links: 'External links valid',
    supported_locales: 'Supported languages valid',
    description_localizations: 'Localized descriptions valid',
    install_path: 'Active install path',
    user_install_command: 'Active global user-install command'
  };
  let application = $state<Application | null>(null);
  let directoryAssets = $state<ApplicationAsset[]>([]);
  let directoryPreview = $state<DirectoryPreviewResponse | null>(null);
  let previewLoading = $state(false);
  let previewError = $state('');
  let credentials = $state<Credential[]>([]);
  let workers = $state<Worker[]>([]);
  let templates = $state<Template[]>([]);
  let installations = $state<Installation[]>([]);
  let rules = $state<Rule[]>([]);
  let commandsText = $state('[]');
  let error = $state('');
  let notice = $state('');
  let busy = $state(false);
  let credentialLabel = $state('Deployment');
  let credentialToken = $state('');
  let workerName = $state('Production worker');
  let workerKey = $state('');
  let workerTargets = $state('');
  let templateSlug = $state('install');
  let templateName = $state('Install bot');
  let templateDescription = $state('');
  let ruleDomain = $state('');
  let ruleEffect = $state<'allow' | 'deny'>('deny');
  let directoryTags = $state('');
  let loadedRef = $state('');
  let loadController = new AbortController();
  let previewController = new AbortController();
  let loadGeneration = 0;
  let previewGeneration = 0;
  let mutationGeneration = 0;

  function loadIsCurrent(
    applicationRef: string,
    controller: AbortController,
    generation: number
  ): boolean {
    return (
      !controller.signal.aborted &&
      loadedRef === applicationRef &&
      loadGeneration === generation &&
      ref === applicationRef
    );
  }

  function routeOwnsApplication(
    applicationRef: string,
    expectedApplication?: Application | null
  ): boolean {
    return (
      loadedRef === applicationRef &&
      ref === applicationRef &&
      (expectedApplication === undefined || application === expectedApplication)
    );
  }

  function mutationIsCurrent(
    applicationRef: string,
    generation: number,
    expectedApplication?: Application | null
  ): boolean {
    return (
      mutationGeneration === generation && routeOwnsApplication(applicationRef, expectedApplication)
    );
  }

  async function load(applicationRef = ref) {
    loadController.abort();
    const controller = new AbortController();
    loadController = controller;
    const generation = ++loadGeneration;
    error = '';
    try {
      const [
        app,
        commandList,
        credentialList,
        workerList,
        templateList,
        installationList,
        ruleList,
        assetList
      ] = await Promise.all([
        api<Application>(`/applications/${encodeURIComponent(applicationRef)}`, {
          signal: controller.signal
        }),
        api<Record<string, unknown>[]>(
          `/applications/${encodeURIComponent(applicationRef)}/commands`,
          { signal: controller.signal }
        ),
        api<Credential[]>(`/applications/${encodeURIComponent(applicationRef)}/credentials`, {
          signal: controller.signal
        }),
        api<Worker[]>(`/applications/${encodeURIComponent(applicationRef)}/workers`, {
          signal: controller.signal
        }),
        api<Template[]>(`/applications/${encodeURIComponent(applicationRef)}/install-templates`, {
          signal: controller.signal
        }),
        api<Installation[]>(`/applications/${encodeURIComponent(applicationRef)}/installations`, {
          signal: controller.signal
        }),
        api<Rule[]>(`/applications/${encodeURIComponent(applicationRef)}/instance-rules`, {
          signal: controller.signal
        }),
        api<ApplicationAsset[]>(`/applications/${encodeURIComponent(applicationRef)}/assets`, {
          signal: controller.signal
        })
      ]);
      if (!loadIsCurrent(applicationRef, controller, generation)) return;
      if (
        app.ref !== applicationRef ||
        assetList.some((asset) => asset.application_ref !== applicationRef)
      ) {
        throw new Error('The application response returned a different identity.');
      }
      application = app;
      directoryTags = app.directory_tags.join(', ');
      commandsText = JSON.stringify(commandList, null, 2);
      credentials = credentialList;
      workers = workerList;
      templates = templateList;
      installations = installationList;
      rules = ruleList;
      directoryAssets = assetList;
    } catch (caught) {
      if (loadIsCurrent(applicationRef, controller, generation)) {
        error = userErrorMessage(caught, 'Could not load this application.');
      }
    }
  }

  function previewIsCurrent(
    applicationRef: string,
    controller: AbortController,
    generation: number
  ): boolean {
    return (
      !controller.signal.aborted &&
      loadedRef === applicationRef &&
      ref === applicationRef &&
      previewGeneration === generation
    );
  }

  async function loadDirectoryPreview(applicationRef = ref): Promise<void> {
    previewController.abort();
    const controller = new AbortController();
    previewController = controller;
    const generation = ++previewGeneration;
    previewLoading = true;
    previewError = '';
    try {
      const preview = await api<DirectoryPreviewResponse>(
        `/applications/${encodeURIComponent(applicationRef)}/directory-preview`,
        { signal: controller.signal }
      );
      if (!previewIsCurrent(applicationRef, controller, generation)) return;
      if (
        preview.application_ref !== applicationRef ||
        preview.application.ref !== applicationRef
      ) {
        throw new Error('The directory preview returned a different application.');
      }
      directoryPreview = preview;
    } catch (caught) {
      if (previewIsCurrent(applicationRef, controller, generation)) {
        directoryPreview = null;
        previewError = userErrorMessage(caught, 'Could not load the directory preview.');
      }
    } finally {
      if (previewIsCurrent(applicationRef, controller, generation)) previewLoading = false;
    }
  }

  function handleAssetsChange(nextAssets: ApplicationAsset[]): void {
    const currentApplication = application;
    if (!currentApplication || !routeOwnsApplication(ref, currentApplication)) return;
    const previousAssets = directoryAssets;
    const changed =
      previousAssets.length !== nextAssets.length ||
      previousAssets.some((asset, index) => {
        const next = nextAssets[index];
        return (
          !next ||
          asset.id !== next.id ||
          asset.kind !== next.kind ||
          asset.version !== next.version
        );
      });
    currentApplication.directory_media = syncDirectoryMediaWithAssets(
      currentApplication.directory_media,
      previousAssets,
      nextAssets
    );
    directoryAssets = nextAssets.map((asset) => ({ ...asset }));
    if (changed) void loadDirectoryPreview(ref);
  }

  function updateDirectoryMedia(value: DirectoryMediaInput[]): void {
    if (application && routeOwnsApplication(ref, application)) application.directory_media = value;
  }

  function updateDirectoryExternalLinks(value: DirectoryExternalLink[]): void {
    if (application && routeOwnsApplication(ref, application)) {
      application.directory_external_links = value;
    }
  }

  function updateDirectorySupportedLocales(value: DirectoryLocale[]): void {
    if (application && routeOwnsApplication(ref, application)) {
      application.directory_supported_locales = value;
    }
  }

  function updateDirectoryDescriptionLocalizations(
    value: Partial<Record<DirectoryLocale, string>>
  ): void {
    if (application && routeOwnsApplication(ref, application)) {
      application.directory_description_localizations = value;
    }
  }

  function toggle(list: string[], value: string) {
    return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
  }

  function toggleInstallType(value: 'guild_install' | 'user_install') {
    if (!application) return;
    const enabling = !application.supported_install_types.includes(value);
    application.supported_install_types = toggle(
      application.supported_install_types,
      value
    ) as Array<'guild_install' | 'user_install'>;
    if (value === 'user_install' && enabling) {
      application.default_scopes = [
        ...new Set([...application.default_scopes, ...application.user_install_scopes])
      ];
      application.default_intents = [...new Set([...application.default_intents, 'interactions'])];
    }
  }

  function permissionBits(value: string): string {
    return permissionMask(value).toString();
  }

  async function saveApplication() {
    if (busy || !application || loadedRef !== ref) return;
    const applicationRef = ref;
    const currentApplication = application;
    const generation = ++mutationGeneration;
    busy = true;
    error = '';
    notice = '';
    try {
      const directoryPayload = directorySettingsPayload({
        media: currentApplication.directory_media,
        externalLinks: currentApplication.directory_external_links,
        supportedLocales: currentApplication.directory_supported_locales,
        descriptionLocalizations: currentApplication.directory_description_localizations
      });
      const updatedApplication = await api<Application>(
        `/applications/${encodeURIComponent(applicationRef)}`,
        {
          method: 'PATCH',
          body: JSON.stringify({
            name: currentApplication.name,
            description: currentApplication.description,
            support_url: currentApplication.support_url || null,
            privacy_url: currentApplication.privacy_url || null,
            terms_url: currentApplication.terms_url || null,
            directory_enabled: currentApplication.directory_enabled,
            directory_summary: currentApplication.directory_summary || null,
            directory_category: currentApplication.directory_category,
            directory_tags: directoryTags
              .split(',')
              .map((tag) => tag.trim().toLowerCase())
              .filter(Boolean),
            ...directoryPayload,
            target_policy: currentApplication.target_policy,
            default_scopes: currentApplication.default_scopes,
            default_intents: currentApplication.default_intents,
            // Permission masks extend beyond JavaScript's safe-integer range.
            // Keep the exact decimal representation on the wire.
            default_permissions: permissionBits(currentApplication.default_permissions),
            supported_install_types: currentApplication.supported_install_types,
            user_install_scopes: currentApplication.user_install_scopes,
            user_install_contexts: currentApplication.user_install_contexts,
            e2ee_modes: currentApplication.e2ee_modes
          })
        }
      );
      if (mutationIsCurrent(applicationRef, generation, currentApplication)) {
        application = updatedApplication;
        directoryTags = updatedApplication.directory_tags.join(', ');
        notice = 'Application settings saved.';
        void loadDirectoryPreview(applicationRef);
      }
    } catch (caught) {
      if (mutationIsCurrent(applicationRef, generation, currentApplication)) {
        error = userErrorMessage(caught, 'Could not save the application.');
      }
    } finally {
      if (mutationIsCurrent(applicationRef, generation)) busy = false;
    }
  }
  async function saveCommands() {
    if (busy || !routeOwnsApplication(ref, application)) return;
    const applicationRef = ref;
    const currentApplication = application;
    const commandDraft = commandsText;
    const generation = ++mutationGeneration;
    busy = true;
    error = '';
    try {
      const parsed = JSON.parse(commandDraft);
      if (!Array.isArray(parsed)) throw new Error('Commands must be a JSON array.');
      await api(`/applications/${encodeURIComponent(applicationRef)}/commands`, {
        method: 'PUT',
        body: JSON.stringify({ commands: parsed })
      });
      if (!mutationIsCurrent(applicationRef, generation, currentApplication)) return;
      notice = 'Commands published.';
      await load(applicationRef);
    } catch (caught) {
      if (mutationIsCurrent(applicationRef, generation, currentApplication)) {
        error =
          caught instanceof SyntaxError ||
          (caught instanceof Error && caught.message.startsWith('Commands'))
            ? caught.message
            : userErrorMessage(caught, 'Could not publish commands.');
      }
    } finally {
      if (mutationIsCurrent(applicationRef, generation)) busy = false;
    }
  }
  async function createCredential() {
    if (busy || !routeOwnsApplication(ref, application)) return;
    const applicationRef = ref;
    const currentApplication = application;
    const label = credentialLabel;
    const generation = ++mutationGeneration;
    busy = true;
    error = '';
    credentialToken = '';
    try {
      const created = await api<{ token: string }>(
        `/applications/${encodeURIComponent(applicationRef)}/credentials`,
        {
          method: 'POST',
          body: JSON.stringify({
            label,
            scopes: ['workers.manage', 'commands.manage']
          })
        }
      );
      if (!mutationIsCurrent(applicationRef, generation, currentApplication)) return;
      credentialToken = created.token;
      notice = 'Control credential created. Copy it now; it will not be shown again.';
      await load(applicationRef);
    } catch (caught) {
      if (mutationIsCurrent(applicationRef, generation, currentApplication)) {
        error = userErrorMessage(caught, 'Could not create the control credential.');
      }
    } finally {
      if (mutationIsCurrent(applicationRef, generation)) busy = false;
    }
  }
  async function revokeCredential(id: string) {
    if (!routeOwnsApplication(ref, application)) return;
    const applicationRef = ref;
    const currentApplication = application;
    if (!confirm('Revoke this control credential?')) return;
    await api(`/applications/${encodeURIComponent(applicationRef)}/credentials/${id}`, {
      method: 'DELETE'
    });
    if (routeOwnsApplication(applicationRef, currentApplication)) await load(applicationRef);
  }
  async function createWorker() {
    if (busy || !application || !routeOwnsApplication(ref, application)) return;
    const applicationRef = ref;
    const currentApplication = application;
    const name = workerName;
    const publicKey = workerKey.trim();
    const targetDomains = workerTargets
      .split(',')
      .map((value) => value.trim())
      .filter(Boolean);
    const generation = ++mutationGeneration;
    busy = true;
    error = '';
    try {
      await api(`/applications/${encodeURIComponent(applicationRef)}/workers`, {
        method: 'POST',
        body: JSON.stringify({
          name,
          public_key: publicKey,
          scopes: currentApplication.default_scopes,
          intents: currentApplication.default_intents,
          target_domains: targetDomains,
          session_limit: 1
        })
      });
      if (!mutationIsCurrent(applicationRef, generation, currentApplication)) return;
      workerKey = '';
      notice = 'Worker enrolled.';
      await load(applicationRef);
    } catch (caught) {
      if (mutationIsCurrent(applicationRef, generation, currentApplication)) {
        error = userErrorMessage(caught, 'Could not enroll the worker.');
      }
    } finally {
      if (mutationIsCurrent(applicationRef, generation)) busy = false;
    }
  }
  async function revokeWorker(id: string) {
    if (!routeOwnsApplication(ref, application)) return;
    const applicationRef = ref;
    const currentApplication = application;
    if (!confirm('Revoke this worker? Existing tokens and gateway sessions will stop working.'))
      return;
    await api(`/applications/${encodeURIComponent(applicationRef)}/workers/${id}`, {
      method: 'DELETE'
    });
    if (routeOwnsApplication(applicationRef, currentApplication)) await load(applicationRef);
  }
  async function createTemplate() {
    if (busy || !application || !routeOwnsApplication(ref, application)) return;
    const applicationRef = ref;
    const currentApplication = application;
    const slug = templateSlug;
    const name = templateName;
    const description = templateDescription || null;
    const generation = ++mutationGeneration;
    busy = true;
    error = '';
    try {
      await api(`/applications/${encodeURIComponent(applicationRef)}/install-templates`, {
        method: 'POST',
        body: JSON.stringify({
          slug,
          name,
          description,
          scopes: currentApplication.default_scopes,
          intents: currentApplication.default_intents,
          permissions: permissionBits(currentApplication.default_permissions),
          contexts: ['guild'],
          e2ee_mode: currentApplication.e2ee_modes.includes('participant')
            ? 'participant'
            : 'disabled'
        })
      });
      if (!mutationIsCurrent(applicationRef, generation, currentApplication)) return;
      notice = 'Invite link created.';
      await load(applicationRef);
    } catch (caught) {
      if (mutationIsCurrent(applicationRef, generation, currentApplication)) {
        error = userErrorMessage(caught, 'Could not create the invite link.');
      }
    } finally {
      if (mutationIsCurrent(applicationRef, generation)) busy = false;
    }
  }
  async function addRule() {
    const applicationRef = ref;
    const currentApplication = application;
    const targetDomain = ruleDomain.trim();
    const effect = ruleEffect;
    if (!targetDomain || !routeOwnsApplication(applicationRef, currentApplication)) return;
    try {
      await api(
        `/applications/${encodeURIComponent(applicationRef)}/instance-rules/${encodeURIComponent(targetDomain)}`,
        { method: 'PUT', body: JSON.stringify({ effect }) }
      );
      if (!routeOwnsApplication(applicationRef, currentApplication)) return;
      ruleDomain = '';
      await load(applicationRef);
    } catch (caught) {
      if (routeOwnsApplication(applicationRef, currentApplication)) {
        error = userErrorMessage(caught, 'Could not save the instance rule.');
      }
    }
  }
  async function deleteRule(domain: string) {
    if (!routeOwnsApplication(ref, application)) return;
    const applicationRef = ref;
    const currentApplication = application;
    await api(
      `/applications/${encodeURIComponent(applicationRef)}/instance-rules/${encodeURIComponent(domain)}`,
      { method: 'DELETE' }
    );
    if (routeOwnsApplication(applicationRef, currentApplication)) await load(applicationRef);
  }
  async function copy(value: string, label = 'Invite link') {
    await navigator.clipboard.writeText(value);
    notice = `${label} copied.`;
  }
  $effect(() => {
    const applicationRef = ref;
    if (applicationRef === loadedRef) return;
    loadedRef = applicationRef;
    application = null;
    credentials = [];
    workers = [];
    templates = [];
    installations = [];
    rules = [];
    directoryAssets = [];
    directoryPreview = null;
    previewError = '';
    previewLoading = true;
    commandsText = '[]';
    directoryTags = '';
    credentialToken = '';
    notice = '';
    mutationGeneration += 1;
    busy = false;
    void load(applicationRef);
    void loadDirectoryPreview(applicationRef);
  });

  onDestroy(() => {
    loadGeneration += 1;
    mutationGeneration += 1;
    loadController.abort();
    previewGeneration += 1;
    previewController.abort();
  });
</script>

<svelte:head
  ><title
    >{loadedRef === ref ? (application?.name ?? 'Application') : 'Application'} · Developer Portal</title
  ></svelte:head
>
<main class="page">
  <header class="top">
    <a href={resolve('/developers')}>← Applications</a>
    <div>
      <small>Developer Portal</small>
      <h1>{loadedRef === ref ? (application?.name ?? 'Loading…') : 'Loading…'}</h1>
      <p>{loadedRef === ref ? (application?.bot_user.handle ?? ref) : ref}</p>
    </div>
    <button onclick={saveApplication} disabled={busy || !application || loadedRef !== ref}
      >Save changes</button
    >
  </header>
  {#if error}<div class="notice error" role="alert">{error}</div>{/if}{#if notice}<div
      class="notice success"
    >
      {notice}<button aria-label="Dismiss" onclick={() => (notice = '')}>×</button>
    </div>{/if}
  {#if application && loadedRef === ref}
    <div class="layout">
      <nav>
        <a href="#general">General</a><a href="#discovery-settings">Discovery settings</a><a
          href="#discovery-status">Discovery status</a
        ><a href="#discovery-preview">Product preview</a><a href="#access">Access</a><a
          href="#credentials">Credentials</a
        ><a href="#commands">Commands</a><a href="#workers">Workers</a><a href="#media"
          >Assets & emoji</a
        ><a href="#invites">Invite links</a><a href="#federation">Federation</a><a
          href="#installations">Installations</a
        >
      </nav>
      <div class="sections">
        <section id="general">
          <h2>General information</h2>
          <div class="grid">
            <label>Name<input bind:value={application.name} maxlength="100" /></label><label
              >Status<input value={application.status} disabled /></label
            >
          </div>
          <label
            >Description<textarea bind:value={application.description} rows="3" maxlength="1000"
            ></textarea></label
          >
        </section>
        <section id="discovery-settings">
          <h2>Discovery settings</h2>
          <p>Describe how your app appears in the desktop and browser App Directory.</p>
          <div class="grid">
            <label
              >Category<select bind:value={application.directory_category}
                ><option value={null}>Choose a category</option><option value="entertainment"
                  >Entertainment</option
                ><option value="games">Games</option><option value="moderation">Moderation</option
                ><option value="productivity">Productivity</option><option value="social"
                  >Social</option
                ><option value="utilities">Utilities</option></select
              ></label
            ><label
              >Tags<input
                bind:value={directoryTags}
                placeholder="moderation, utility, community"
              /><small>1–5 unique lowercase tags, separated by commas.</small></label
            >
          </div>
          <label
            >Directory summary<textarea
              bind:value={application.directory_summary}
              rows="2"
              maxlength="200"
              placeholder="A short explanation of what your app helps people do."
            ></textarea></label
          >
          <div class="grid">
            <label
              >Support URL<input
                type="url"
                bind:value={application.support_url}
                placeholder="https://support.example"
              /></label
            ><label
              >Privacy policy URL<input
                type="url"
                bind:value={application.privacy_url}
                placeholder="https://example/privacy"
              /></label
            >
          </div>
          <label
            >Terms of service URL<input
              type="url"
              bind:value={application.terms_url}
              placeholder="https://example/terms"
            /></label
          >
          <ApplicationDirectorySettings
            originDomain={application.origin_domain}
            media={application.directory_media}
            externalLinks={application.directory_external_links}
            supportedLocales={application.directory_supported_locales}
            descriptionLocalizations={application.directory_description_localizations}
            assets={directoryAssets}
            disabled={busy}
            onMediaChange={updateDirectoryMedia}
            onExternalLinksChange={updateDirectoryExternalLinks}
            onSupportedLocalesChange={updateDirectorySupportedLocales}
            onDescriptionLocalizationsChange={updateDirectoryDescriptionLocalizations}
          />
        </section>
        <section id="discovery-status">
          <div class="section-title-row">
            <div>
              <h2>Discovery status</h2>
              <p>Review the saved publication checklist before submitting your listing.</p>
            </div>
            <button
              class="secondary"
              onclick={() => void loadDirectoryPreview(ref)}
              disabled={previewLoading}>{previewLoading ? 'Refreshing…' : 'Refresh'}</button
            >
          </div>
          <label
            >Directory status<input
              value={application.directory_approved
                ? 'Approved'
                : application.directory_enabled
                  ? 'Submitted for review'
                  : 'Not listed'}
              disabled
            /></label
          >
          <label class="toggle"
            ><input type="checkbox" bind:checked={application.directory_enabled} /><span
              ><strong>List in the App Directory</strong><small
                >Your home instance reviews the listing before it becomes searchable. Account
                installation requires an active global command that explicitly supports user
                installation.</small
              ></span
            ></label
          >
          {#if previewError}<p class="warning" role="alert">{previewError}</p>{/if}
          {#if directoryPreview}
            <div class="readiness-summary">
              <strong
                >{directoryPreview.readiness.status === 'approved'
                  ? 'Approved'
                  : directoryPreview.readiness.status === 'ready_for_review'
                    ? 'Ready for review'
                    : 'Incomplete'}</strong
              >
              <small>This checklist reflects your last saved settings.</small>
            </div>
            <ul class="checklist">
              {#each directoryPreview.readiness.items as item (item.key)}
                <li class:ready={item.ready}>
                  <span aria-hidden="true">{item.ready ? '✓' : '○'}</span>
                  {readinessLabels[item.key]}
                </li>
              {/each}
            </ul>
          {:else if previewLoading}<p>Loading readiness…</p>{/if}
          {#if application.directory_enabled && !application.directory_approved}
            <p>Your saved listing will await approval from your home instance once it is ready.</p>
          {/if}
        </section>
        <section id="discovery-preview">
          <div class="section-title-row">
            <div>
              <h2>Product page preview</h2>
              <p>Preview the saved listing that reviewers and members will see.</p>
            </div>
          </div>
          {#if directoryPreview?.application}
            {@const product = directoryPreview.application}
            <article class="product-preview">
              <div class="preview-identity">
                <span class="preview-icon">{product.name.slice(0, 1).toUpperCase()}</span>
                <div>
                  <small>{product.category ?? 'Category not set'}</small>
                  <h3>{product.name}{product.verified ? ' ✓' : ''}</h3>
                  <p>{product.summary ?? 'Add a summary to complete this preview.'}</p>
                </div>
              </div>
              <p class="preview-description">
                {product.description ?? 'Add a description to complete this preview.'}
              </p>
              {#if product.media.length}
                <div
                  class="preview-media"
                  aria-label={`${product.media.length} product media items`}
                >
                  {#each product.media as item (`${item.type}:${item.type === 'image' ? item.asset_id : item.video_id}`)}
                    <span>{item.type === 'image' ? item.name : `YouTube · ${item.video_id}`}</span>
                  {/each}
                </div>
              {/if}
              <div class="preview-meta">
                <span>{product.tags.join(' · ')}</span>
                <span>{product.install_template?.name ?? 'Install path not configured'}</span>
                {#if product.supported_locales.length}<span
                    >{product.supported_locales.length} supported language{product.supported_locales
                      .length === 1
                      ? ''
                      : 's'}</span
                  >{/if}
              </div>
            </article>
          {:else if previewLoading}
            <p>Loading product preview…</p>
          {:else if !previewError}<p>Product preview unavailable.</p>{/if}
        </section>
        <section id="access">
          <h2>API access</h2>
          <p>
            Scopes control what the bot can request. Intents control which live events are
            delivered. A guild may approve less.
          </p>
          <h3>Scopes</h3>
          <div class="chips">
            {#each scopes as scope (scope)}<label
                class:active={application.default_scopes.includes(scope)}
                ><input
                  type="checkbox"
                  checked={application.default_scopes.includes(scope)}
                  onchange={() =>
                    application &&
                    (application.default_scopes = toggle(application.default_scopes, scope))}
                />{scope}</label
              >{/each}
          </div>
          <h3>Gateway intents</h3>
          <div class="chips">
            {#each intents as intent (intent)}<label
                class:active={application.default_intents.includes(intent)}
                ><input
                  type="checkbox"
                  checked={application.default_intents.includes(intent)}
                  onchange={() =>
                    application &&
                    (application.default_intents = toggle(application.default_intents, intent))}
                />{intent}</label
              >{/each}
          </div>
          <PermissionChecklist
            value={application.default_permissions}
            onChange={(value) => application && (application.default_permissions = value)}
          />
          <div class="grid">
            <label
              >Target policy<select bind:value={application.target_policy}
                ><option value="open">Open federation</option><option value="allowlist"
                  >Allowlist only</option
                ><option value="blocklist">Open except blocked instances</option><option
                  value="local_only">Local instance only</option
                ></select
              ></label
            >
          </div>
          <h3>Installation contexts</h3>
          <p>
            Choose where Discord-style Add App authorization is offered. User installs authorize
            only interactions the account explicitly starts.
          </p>
          <div class="chips">
            {#each installTypes as installType (installType[0])}
              <label class:active={application.supported_install_types.includes(installType[0])}
                ><input
                  type="checkbox"
                  checked={application.supported_install_types.includes(installType[0])}
                  disabled={application.supported_install_types.length === 1 &&
                    application.supported_install_types.includes(installType[0])}
                  onchange={() => toggleInstallType(installType[0])}
                />{installType[1]}</label
              >
            {/each}
          </div>
          {#if application.supported_install_types.includes('user_install')}
            <h3>User-install scopes</h3>
            <div class="chips">
              {#each userInstallScopes as scope (scope)}<label
                  class:active={application.user_install_scopes.includes(scope)}
                  ><input
                    type="checkbox"
                    checked={application.user_install_scopes.includes(scope)}
                    disabled={requiredUserInstallScopes.has(scope)}
                    onchange={() =>
                      application &&
                      (application.user_install_scopes = toggle(
                        application.user_install_scopes,
                        scope
                      ))}
                  />{scope}</label
                >{/each}
            </div>
            <h3>User-install command contexts</h3>
            <div class="chips">
              {#each userInstallContexts as context (context[0])}<label
                  class:active={application.user_install_contexts.includes(context[0])}
                  ><input
                    type="checkbox"
                    checked={application.user_install_contexts.includes(context[0])}
                    disabled={application.user_install_contexts.length === 1 &&
                      application.user_install_contexts.includes(context[0])}
                    onchange={() =>
                      application &&
                      (application.user_install_contexts = toggle(
                        application.user_install_contexts,
                        context[0]
                      ) as Array<'guild' | 'bot_dm' | 'private_channel'>)}
                  />{context[1]}</label
                >{/each}
            </div>
          {/if}
          <p class="warning">
            Message content and history remain unavailable in E2EE channels unless the bot is
            installed and explicitly admitted per channel as a visible cryptographic participant.
            Admission grants only future encrypted content and rotates the room keys.
          </p>
        </section>
        <section id="credentials">
          <h2>Control credentials</h2>
          <p>
            Deployment tools use these scoped secrets only to enroll workers and publish command
            definitions. They cannot connect as the bot or sign in as a user.
          </p>
          <div class="inline">
            <input bind:value={credentialLabel} maxlength="100" placeholder="Deployment" /><button
              onclick={createCredential}
              disabled={busy || !credentialLabel.trim()}>Create credential</button
            >
          </div>
          {#if credentialToken}
            <div class="secret" role="status">
              <strong>Copy this token now</strong><code>{credentialToken}</code><button
                onclick={() => copy(credentialToken, 'Credential')}>Copy</button
              >
            </div>
          {/if}
          <div class="rows">
            {#each credentials as credential (credential.id)}<article>
                <div>
                  <strong>{credential.label}</strong><small
                    >{credential.token_hint} · {credential.scopes.join(', ')}</small
                  >
                </div>
                <span class:revoked={credential.revoked_at}
                  >{credential.revoked_at ? 'Revoked' : 'Active'}</span
                >{#if !credential.revoked_at}<button
                    class="danger"
                    onclick={() => revokeCredential(credential.id)}>Revoke</button
                  >{/if}
              </article>{/each}
          </div>
        </section>
        <section id="commands">
          <h2>Slash and context commands</h2>
          <p>
            Publish up to 100 chat-input commands, 15 user commands, and 15 message commands.
            Slash-command names use Discord's lowercase Unicode naming rules; context-command names
            may use spaces and title case.
          </p>
          <textarea class="code" bind:value={commandsText} rows="14" spellcheck="false"
          ></textarea><button onclick={saveCommands} disabled={busy}>Publish commands</button>
        </section>
        <section id="workers">
          <h2>Worker keys</h2>
          <p>
            A worker signs short-lived token assertions and connects directly to every target
            instance. Private keys never leave the worker.
          </p>
          <div class="grid">
            <label>Worker name<input bind:value={workerName} /></label><label
              >Ed25519 public key (base64url)<input
                bind:value={workerKey}
                placeholder="43-character public key"
              /></label
            >
          </div>
          <label
            >Target domains (comma separated, empty means any approved target)<input
              bind:value={workerTargets}
              placeholder="chat.example, community.example"
            /></label
          ><button onclick={createWorker} disabled={busy || workerKey.length < 43}
            >Enroll worker</button
          >
          <div class="rows">
            {#each workers as worker (worker.id)}<article>
                <div>
                  <strong>{worker.name}</strong><small
                    >#{worker.id} · {worker.target_domains.join(', ') ||
                      'all approved targets'}</small
                  >
                </div>
                <span class:revoked={worker.revoked_at}
                  >{worker.revoked_at ? 'Revoked' : 'Active'}</span
                >{#if !worker.revoked_at}<button
                    class="danger"
                    onclick={() => revokeWorker(worker.id)}>Revoke</button
                  >{/if}
              </article>{/each}
          </div>
        </section>
        <section id="media">
          <h2>Application assets and emoji</h2>
          <p>
            These assets belong to the application, are versioned with its federated manifest, and
            can also be managed through the bot REST API and Python SDK.
          </p>
          <ApplicationMediaManager applicationRef={ref} onAssetsChange={handleAssetsChange} />
        </section>
        <section id="invites">
          <h2>Bot invite links</h2>
          <p>
            Invite pages show the app origin, requested permissions, data access, and E2EE behavior
            before an administrator approves.
          </p>
          <div class="grid">
            <label>Slug<input bind:value={templateSlug} /></label><label
              >Invite name<input bind:value={templateName} /></label
            >
          </div>
          <label>Description<input bind:value={templateDescription} /></label><button
            onclick={createTemplate}
            disabled={busy}>Create invite link</button
          >
          <div class="rows">
            {#each templates as template (template.id)}<article>
                <div>
                  <strong>{template.name}</strong><small
                    >{template.e2ee_mode} · {template.active ? 'active' : 'disabled'}</small
                  >
                </div>
                <code>{template.invite_url}</code><button onclick={() => copy(template.invite_url)}
                  >Copy</button
                >
              </article>{/each}
          </div>
        </section>
        <section id="federation">
          <h2>Federated instance policy</h2>
          <p>
            Rules match exact verified instance domains. Deny always wins. Wildcards are not
            supported.
          </p>
          <div class="inline">
            <input bind:value={ruleDomain} placeholder="instance.example" /><select
              bind:value={ruleEffect}
              ><option value="deny">Deny</option><option value="allow">Allow</option></select
            ><button onclick={addRule}>Add rule</button>
          </div>
          <div class="rows">
            {#each rules as rule (rule.target_domain)}<article>
                <code>{rule.target_domain}</code><span class:revoked={rule.effect === 'deny'}
                  >{rule.effect}</span
                ><button onclick={() => deleteRule(rule.target_domain)}>Remove</button>
              </article>{/each}
          </div>
        </section>
        <section id="installations">
          <h2>Installations</h2>
          <div class="rows">
            {#each installations as installation (installation.id)}<article>
                <div>
                  <strong>{installation.guild_ref}</strong><small
                    >{installation.e2ee_mode} · revision {installation.grant_revision} ·
                    {installation.channel_restrictions.length
                      ? `${installation.channel_restrictions.length} channel restrictions`
                      : 'all role-permitted channels'}</small
                  >
                </div>
                <span class:revoked={installation.status !== 'active'}>{installation.status}</span>
              </article>{/each}{#if installations.length === 0}<p>
                No guilds have installed this application.
              </p>{/if}
          </div>
        </section>
      </div>
    </div>
  {/if}
</main>

<style>
  :global(body) {
    overflow: auto;
  }
  .page {
    min-height: 100dvh;
    padding: 1.5rem clamp(1rem, 4vw, 4rem) 5rem;
    background: var(--bg);
    color: var(--text);
  }
  .top {
    display: grid;
    grid-template-columns: 180px 1fr auto;
    gap: 1rem;
    align-items: center;
    max-width: 1280px;
    margin: auto;
  }
  .top a {
    color: var(--text-muted);
    text-decoration: none;
  }
  .top h1,
  .top p {
    margin: 0;
  }
  .top small {
    color: var(--accent);
    font-weight: 800;
    text-transform: uppercase;
  }
  .top button,
  section > button,
  .inline button,
  .rows button {
    border: 0;
    border-radius: 8px;
    padding: 0.7rem 1rem;
    color: var(--on-accent, white);
    background: var(--accent);
    font: inherit;
    font-weight: 800;
    cursor: pointer;
  }
  .secret {
    display: grid;
    gap: 0.65rem;
    margin-top: 1rem;
    padding: 1rem;
    border: 1px solid var(--warning, #d7a447);
    border-radius: 10px;
    background: color-mix(in srgb, var(--warning, #d7a447) 10%, transparent);
  }
  .secret code {
    overflow-wrap: anywhere;
    user-select: all;
  }
  .layout {
    display: grid;
    grid-template-columns: 180px minmax(0, 900px);
    gap: 2rem;
    max-width: 1280px;
    margin: 2rem auto;
  }
  .layout > nav {
    position: sticky;
    top: 1rem;
    display: grid;
    align-content: start;
    gap: 0.2rem;
    height: max-content;
  }
  .layout > nav a {
    border-radius: 7px;
    padding: 0.55rem 0.7rem;
    color: var(--text-muted);
    text-decoration: none;
  }
  .layout > nav a:hover {
    color: var(--text);
    background: var(--surface-hover);
  }
  .sections {
    display: grid;
    gap: 1rem;
  }
  section {
    scroll-margin-top: 1rem;
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 1.3rem;
    background: var(--surface);
  }
  section h2 {
    margin: 0 0 0.3rem;
  }
  section h3 {
    margin: 1.3rem 0 0.6rem;
  }
  section p {
    color: var(--text-muted);
  }
  label {
    display: grid;
    gap: 0.4rem;
    margin: 0.7rem 0;
    font-size: 0.8rem;
    font-weight: 700;
  }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
  }
  input,
  textarea,
  select {
    box-sizing: border-box;
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.7rem;
    color: var(--text);
    background: var(--input-bg, var(--bg));
    font: inherit;
  }
  .code {
    font-family: ui-monospace, monospace;
    font-size: 0.82rem;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
  }
  .chips label {
    display: block;
    margin: 0;
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 0.35rem 0.65rem;
    color: var(--text-muted);
    cursor: pointer;
  }
  .chips label.active {
    border-color: var(--accent);
    color: var(--text);
    background: color-mix(in srgb, var(--accent) 15%, transparent);
  }
  .chips input {
    position: absolute;
    opacity: 0;
    width: 1px;
  }
  .warning {
    border-left: 3px solid var(--warning, #d79b36);
    padding: 0.7rem 1rem;
    background: var(--surface-hover);
  }
  .section-title-row,
  .preview-identity,
  .readiness-summary {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    align-items: start;
  }
  .section-title-row p,
  .readiness-summary small {
    margin: 0;
  }
  .section-title-row button.secondary {
    border: 0;
    border-radius: 8px;
    padding: 0.65rem 0.85rem;
    cursor: pointer;
    color: var(--text);
    background: var(--surface-hover);
    font: inherit;
    font-weight: 750;
  }
  .toggle {
    grid-template-columns: auto 1fr;
    gap: 0.75rem;
    align-items: start;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 0.9rem;
  }
  .toggle input {
    width: auto;
    margin-top: 0.2rem;
  }
  .toggle span {
    display: grid;
    gap: 0.25rem;
  }
  .toggle small {
    color: var(--text-muted);
    font-weight: 500;
  }
  .readiness-summary {
    align-items: baseline;
    margin-top: 1rem;
  }
  .checklist {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.45rem;
    margin: 0.8rem 0 0;
    padding: 0;
    list-style: none;
  }
  .checklist li {
    display: flex;
    gap: 0.45rem;
    color: var(--text-muted);
    font-size: 0.85rem;
  }
  .checklist li.ready {
    color: var(--success);
  }
  .product-preview {
    display: grid;
    gap: 1rem;
    margin-top: 1rem;
    border: 1px solid var(--line);
    border-radius: 13px;
    padding: 1rem;
    background: var(--bg);
  }
  .preview-identity {
    justify-content: flex-start;
    align-items: center;
  }
  .preview-icon {
    display: grid;
    place-items: center;
    flex: 0 0 58px;
    height: 58px;
    border-radius: 15px;
    color: white;
    background: var(--accent);
    font-size: 1.4rem;
    font-weight: 850;
  }
  .preview-identity h3,
  .preview-identity p,
  .preview-description {
    margin: 0;
  }
  .preview-identity small,
  .preview-meta {
    color: var(--text-muted);
  }
  .preview-description {
    white-space: pre-wrap;
    line-height: 1.55;
  }
  .preview-media,
  .preview-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
  }
  .preview-media span,
  .preview-meta span {
    border-radius: 999px;
    padding: 0.3rem 0.55rem;
    background: var(--surface-hover);
    font-size: 0.75rem;
  }
  .rows {
    display: grid;
    gap: 0.5rem;
    margin-top: 1rem;
  }
  .rows article {
    display: flex;
    align-items: center;
    gap: 0.8rem;
    border-top: 1px solid var(--line);
    padding: 0.8rem 0;
  }
  .rows article > div {
    display: grid;
    flex: 1;
  }
  .rows small {
    color: var(--text-muted);
  }
  .rows code {
    overflow: hidden;
    flex: 1;
    text-overflow: ellipsis;
  }
  .rows span {
    border-radius: 999px;
    padding: 0.2rem 0.5rem;
    background: var(--success);
    color: white;
    font-size: 0.72rem;
  }
  .rows span.revoked,
  .danger {
    background: var(--danger) !important;
  }
  .inline {
    display: grid;
    grid-template-columns: 1fr 130px auto;
    gap: 0.5rem;
  }
  .notice {
    max-width: 1100px;
    margin: 1rem auto;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.8rem 1rem;
  }
  .notice.error {
    border-color: var(--danger);
    color: var(--danger);
  }
  .notice.success {
    border-color: var(--success);
  }
  .notice button {
    float: right;
    border: 0;
    color: inherit;
    background: none;
    font-size: 1.2rem;
  }
  @media (max-width: 760px) {
    .top {
      grid-template-columns: 1fr auto;
    }
    .top > a {
      grid-column: 1/-1;
    }
    .layout {
      display: block;
    }
    .layout > nav {
      position: static;
      display: flex;
      overflow-x: auto;
      margin-bottom: 1rem;
    }
    .grid,
    .inline,
    .checklist {
      grid-template-columns: 1fr;
    }
    .rows article {
      align-items: flex-start;
      flex-wrap: wrap;
    }
    .page {
      padding: 1rem 1rem 4rem;
    }
  }
</style>
