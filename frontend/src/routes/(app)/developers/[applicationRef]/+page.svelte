<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { api, userErrorMessage } from '$lib/api/client';
  import { assetUrl } from '$lib/media/assets';
  import ApplicationCommandEditor from '$lib/components/ApplicationCommandEditor.svelte';
  import { commandDraftError, readCommandDraft } from '$lib/chat/command-editor';
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
  import { applicationSettingsMatch } from '$lib/chat/application-settings';
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
    icon_hash: string | null;
    banner_hash: string | null;
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
    bot_user: { id: string; origin_domain: string; handle: string };
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
  let readinessLabels: Record<DirectoryReadinessKey, string> = $derived({
    directory_enabled: 'Directory listing enabled',
    summary: 'Summary',
    category: 'Category',
    tags: 'One to five tags',
    description: $t('ui_description_526e0087'),
    support_url: 'Support URL',
    privacy_url: 'Privacy policy URL',
    terms_url: 'Terms of service URL',
    media: 'Product-page media',
    external_links: 'External links valid',
    supported_locales: 'Supported languages valid',
    description_localizations: 'Localized descriptions valid',
    install_path: 'Active install path',
    user_install_command: 'Active global user-install command'
  });
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
  let savedCommandsText = $state('[]');
  let commandsLoaded = $state(false);
  let commandError = $state('');
  let commandNotice = $state('');
  let publishingCommands = $state(false);
  const commandsDirty = $derived(commandsText !== savedCommandsText);
  const panels = [
    {
      id: 'general',
      title: 'Overview',
      subtitle: 'Identity and setup',
      symbol: '◈',
      heading: 'Your app, at a glance',
      description: 'Give your app an identity, then choose what you want to set up next.'
    },
    {
      id: 'directory',
      title: 'Directory listing',
      subtitle: 'Profile, media & discovery',
      symbol: '▦',
      heading: 'Make a great first impression',
      description: 'Help people understand your app before they install it.'
    },
    {
      id: 'access',
      title: 'Permissions & installs',
      subtitle: 'Access and availability',
      symbol: '◇',
      heading: 'Choose what your app can access',
      description: 'Request only the permissions and events your app needs.'
    },
    {
      id: 'commands',
      title: 'Commands',
      subtitle: 'Slash commands & actions',
      symbol: '/',
      heading: 'Give people a way to interact',
      description: 'Create the commands people will see in chat and in the Apps menu.'
    },
    {
      id: 'deployment',
      title: 'Credentials & workers',
      subtitle: 'Connect your bot code',
      symbol: '⌘',
      heading: 'Connect your running bot',
      description: 'Create a deployment credential, then enroll the workers that run your app.'
    },
    {
      id: 'distribution',
      title: 'Invite links & servers',
      subtitle: 'Share and manage installs',
      symbol: '↗',
      heading: 'Bring your app to a community',
      description: 'Create an install link and see which servers have installed your app.'
    }
  ] as const;
  let activePanel = $state<(typeof panels)[number]['id']>('general');
  const currentPanel = $derived(panels.find((panel) => panel.id === activePanel)!);

  function selectPanel(panel: (typeof panels)[number]['id']) {
    activePanel = panel;
    window.scrollTo(0, 0);
  }

  function updateCommands(value: string) {
    commandsText = value;
    commandError = '';
    commandNotice = '';
  }
  let error = $state('');
  let notice = $state('');
  let directoryTags = $state('');
  let savedSettings = $state('');
  let saveState = $state<'idle' | 'saving' | 'saved' | 'error'>('idle');
  let saveError = $state('');
  let accessSearch = $state('');
  const visibleScopes = $derived(
    scopes.filter((scope) => scope.includes(accessSearch.trim().toLowerCase()))
  );
  function settingsSnapshot(app: Application | null, tags: string): string {
    return JSON.stringify({
      application: app ? { ...app, icon_hash: undefined, banner_hash: undefined } : app,
      directoryTags: tags
    });
  }
  const settingsDraft = $derived(settingsSnapshot(application, directoryTags));
  const hasUnsavedChanges = $derived(!!application && settingsDraft !== savedSettings);
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
        throw new Error($t('ui_the_application_response_returned_a_different_bb3280e9'));
      }
      // Inventory actions must not replace the settings the user is editing.
      if (!application) {
        application = app;
        directoryTags = app.directory_tags.join(', ');
        savedSettings = settingsSnapshot(app, directoryTags);
      }
      if (!commandsLoaded) {
        commandsText = JSON.stringify(commandList, null, 2);
        savedCommandsText = commandsText;
        commandsLoaded = true;
      }
      credentials = credentialList;
      workers = workerList;
      templates = templateList;
      installations = installationList;
      rules = ruleList;
      directoryAssets = assetList;
    } catch (caught) {
      if (loadIsCurrent(applicationRef, controller, generation)) {
        error = userErrorMessage(caught, $t('ui_could_not_load_this_application_b2582710'));
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
        throw new Error($t('ui_the_directory_preview_returned_a_different_ap_03cd19f1'));
      }
      directoryPreview = preview;
    } catch (caught) {
      if (previewIsCurrent(applicationRef, controller, generation)) {
        directoryPreview = null;
        previewError = userErrorMessage(
          caught,
          $t('ui_could_not_load_the_directory_preview_6f22f5bc')
        );
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
    const submittedApplication = $state.snapshot(currentApplication);
    const submittedTags = directoryTags;
    busy = true;
    saveState = 'saving';
    saveError = '';
    error = '';
    notice = '';
    try {
      const directoryPayload = directorySettingsPayload({
        media: currentApplication.directory_media,
        externalLinks: currentApplication.directory_external_links,
        supportedLocales: currentApplication.directory_supported_locales,
        descriptionLocalizations: currentApplication.directory_description_localizations
      });
      const submittedSettings = {
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
      };
      // PATCH only edited fields so an older form cannot reset unrelated settings.
      const previousApplication: Application = JSON.parse(savedSettings).application;
      const changedSettings = Object.fromEntries(
        Object.entries(submittedSettings).filter(
          ([key, value]) => !applicationSettingsMatch(previousApplication, { [key]: value })
        )
      );
      const updatedApplication = await api<Application>(
        `/applications/${encodeURIComponent(applicationRef)}`,
        {
          method: 'PATCH',
          body: JSON.stringify(changedSettings)
        }
      );
      if (mutationIsCurrent(applicationRef, generation, currentApplication)) {
        if (
          updatedApplication.ref !== applicationRef ||
          !applicationSettingsMatch(updatedApplication, changedSettings)
        ) {
          saveState = 'error';
          saveError = $t('ui_the_server_did_not_confirm_all_of_your_change_21776b21');
          return;
        }
        const savedTags = updatedApplication.directory_tags.join(', ');
        savedSettings = settingsSnapshot(updatedApplication, savedTags);
        // Apply server normalization without losing edits made during the request.
        const newerEdits = Object.fromEntries(
          (Object.keys(submittedApplication) as (keyof Application)[])
            .filter(
              (key) =>
                JSON.stringify(currentApplication[key]) !==
                JSON.stringify(submittedApplication[key])
            )
            .map((key) => [key, currentApplication[key]])
        );
        application = { ...updatedApplication, ...newerEdits };
        if (directoryTags === submittedTags) directoryTags = savedTags;
        saveState = 'saved';

        void loadDirectoryPreview(applicationRef);
      }
    } catch (caught) {
      if (mutationIsCurrent(applicationRef, generation, currentApplication)) {
        saveState = 'error';
        saveError = userErrorMessage(caught, $t('ui_could_not_save_the_application_d8ac44a2'));
      }
    } finally {
      if (mutationIsCurrent(applicationRef, generation)) busy = false;
    }
  }
  async function saveCommands() {
    if (busy || !commandsDirty || !routeOwnsApplication(ref, application)) return;
    commandError = commandDraftError(commandsText);
    if (commandError) return;
    const applicationRef = ref;
    const currentApplication = application;
    const commandDraft = commandsText;
    const generation = ++mutationGeneration;
    busy = true;
    publishingCommands = true;
    commandNotice = '';
    try {
      await api(`/applications/${encodeURIComponent(applicationRef)}/commands`, {
        method: 'PUT',
        body: JSON.stringify({ commands: readCommandDraft(commandDraft) })
      });
      if (!mutationIsCurrent(applicationRef, generation, currentApplication)) return;
      savedCommandsText = commandDraft;
      commandNotice = $t('ui_commands_published_f067e39c');
      void loadDirectoryPreview(applicationRef);
    } catch (caught) {
      if (mutationIsCurrent(applicationRef, generation, currentApplication)) {
        commandError = userErrorMessage(
          caught,
          $t('ui_could_not_publish_commands_your_draft_is_stil_34700cfe')
        );
      }
    } finally {
      if (mutationIsCurrent(applicationRef, generation)) {
        busy = false;
        publishingCommands = false;
      }
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
      notice = $t('ui_control_credential_created_copy_it_now_it_wil_d3e4ba34');
      await load(applicationRef);
    } catch (caught) {
      if (mutationIsCurrent(applicationRef, generation, currentApplication)) {
        error = userErrorMessage(caught, $t('ui_could_not_create_the_control_credential_2ebf1ae5'));
      }
    } finally {
      if (mutationIsCurrent(applicationRef, generation)) busy = false;
    }
  }
  async function revokeCredential(id: string) {
    if (!routeOwnsApplication(ref, application)) return;
    const applicationRef = ref;
    const currentApplication = application;
    if (!confirm($t('ui_revoke_this_control_credential_d9571ed2'))) return;
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
      notice = $t('ui_worker_enrolled_8eb34994');
      await load(applicationRef);
    } catch (caught) {
      if (mutationIsCurrent(applicationRef, generation, currentApplication)) {
        error = userErrorMessage(caught, $t('ui_could_not_enroll_the_worker_e3f1b6a7'));
      }
    } finally {
      if (mutationIsCurrent(applicationRef, generation)) busy = false;
    }
  }
  async function revokeWorker(id: string) {
    if (!routeOwnsApplication(ref, application)) return;
    const applicationRef = ref;
    const currentApplication = application;
    if (!confirm($t('ui_revoke_this_worker_existing_tokens_and_gatewa_b261f624'))) return;
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
      notice =
        currentApplication?.status === 'draft'
          ? 'Invite link created. Your application is now active; you can enroll your worker and publish commands.'
          : 'Invite link created.';
      await load(applicationRef);
    } catch (caught) {
      if (mutationIsCurrent(applicationRef, generation, currentApplication)) {
        error = userErrorMessage(caught, $t('ui_could_not_create_the_invite_link_a628cd8b'));
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
        error = userErrorMessage(caught, $t('ui_could_not_save_the_instance_rule_60845bb1'));
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
    savedSettings = '';
    saveState = 'idle';
    saveError = '';
    accessSearch = '';
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
    savedCommandsText = '[]';
    commandsLoaded = false;
    commandError = '';
    commandNotice = '';
    publishingCommands = false;
    activePanel = 'general';
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
    >{$t('ui_value0_developer_portal_c6be8bbc', {
      value0: String(loadedRef === ref ? (application?.name ?? 'Application') : 'Application')
    })}</title
  ></svelte:head
>
<main class="page">
  <header class="top">
    <a class="back-link" href={resolve('/developers')}>{$t('ui_all_applications_18f901c8')}</a>
    <div class="app-heading">
      <div class="app-monogram" aria-hidden="true">
        {#if application?.icon_hash}<img
            src={assetUrl(application.icon_hash, 'thumbnail_128', application.origin_domain)}
            alt=""
          />{:else}{application?.name.slice(0, 1).toUpperCase() || 'K'}{/if}
      </div>
      <div>
        <small>{$t('ui_developer_portal_1eb68022')}</small>
        <h1>
          {loadedRef === ref
            ? (application?.name ?? $t('ui_loading_ba3bbbe1'))
            : $t('ui_loading_ba3bbbe1')}
        </h1>
        <p>{loadedRef === ref ? (application?.bot_user.handle ?? ref) : ref}</p>
      </div>
      {#if application}<span class="app-status">{application.status.replaceAll('_', ' ')}</span
        >{/if}
    </div>
  </header>
  {#if error}<div class="notice error" role="alert">{error}</div>{/if}{#if notice}<div
      class="notice success"
    >
      <span>{notice}</span><button
        class="notice-dismiss"
        aria-label={$t('ui_dismiss_48845bff')}
        onclick={() => (notice = '')}>×</button
      >
    </div>{/if}
  {#if application && loadedRef === ref}
    {#if activePanel === 'commands'}
      <aside
        class="save-card command-save-card"
        aria-label={$t('ui_publish_application_commands_673bcaba')}
      >
        <div class="save-status" role="status" aria-live="polite">
          <strong
            >{publishingCommands
              ? $t('ui_publishing_commands_066633b6')
              : commandError
                ? $t('ui_commands_not_published_bb128a80')
                : commandsDirty
                  ? $t('ui_unpublished_command_changes_7d89734e')
                  : commandNotice || $t('ui_commands_up_to_date_3ce16817')}</strong
          >
          <small
            >{commandError ||
              (commandsDirty
                ? $t('ui_publish_to_update_the_commands_people_can_use_e76b7219')
                : $t('ui_commands_and_app_settings_are_saved_separatel_161ca214'))}</small
          >
          <small
            >{saveState === 'error'
              ? saveError
              : saveState === 'saving'
                ? $t('ui_saving_app_settings_b1ea9324')
                : hasUnsavedChanges
                  ? $t('ui_app_settings_also_have_unsaved_changes_5bbf3e9d')
                  : $t('ui_app_settings_are_saved_73903ba3')}</small
          >
        </div>
        <div class="save-actions">
          <button class="secondary" onclick={saveApplication} disabled={busy || !hasUnsavedChanges}
            >{$t('ui_save_app_settings_b6f3074e')}</button
          >
          <button onclick={saveCommands} disabled={busy || !commandsDirty}
            >{publishingCommands
              ? $t('ui_publishing_582e0f1a')
              : $t('ui_publish_commands_f399e404')}</button
          >
        </div>
      </aside>
    {:else}
      <aside class="save-card" aria-label={$t('ui_save_application_settings_12dfbeb2')}>
        <div class="save-status" role="status" aria-live="polite">
          <strong
            >{saveState === 'saving'
              ? $t('ui_saving_settings_67c658e7')
              : saveState === 'error'
                ? $t('ui_settings_not_saved_b0ebcb44')
                : hasUnsavedChanges
                  ? $t('ui_unsaved_changes_a710c2b9')
                  : saveState === 'saved'
                    ? $t('ui_application_settings_saved_bb68cc15')
                    : $t('ui_app_settings_saved_a6280bef')}</strong
          >
          <small
            >{saveState === 'error'
              ? saveError
              : hasUnsavedChanges
                ? $t('ui_save_your_general_discovery_and_access_settin_d1be2438')
                : commandsDirty
                  ? $t('ui_you_also_have_unpublished_changes_in_commands_f931e9f0')
                  : $t('ui_your_app_settings_are_up_to_date_b7334b9f')}</small
          >
        </div>
        <button onclick={saveApplication} disabled={busy || !hasUnsavedChanges}>
          {saveState === 'saving'
            ? $t('ui_saving_23e39291')
            : saveState === 'error'
              ? $t('ui_retry_save_71fdfa79')
              : $t('ui_save_changes_dd0ae7a5')}
        </button>
      </aside>
    {/if}
    {#if application.status === 'draft'}
      <div class="notice activation-notice" role="status">
        <strong>{$t('ui_activate_your_application_before_connecting_y_fc7323ee')}</strong>
        <p>{$t('ui_new_applications_start_as_drafts_save_your_pe_31d3e2cf')}</p>
        <button class="activation-action" onclick={() => selectPanel('distribution')}
          >{$t('ui_go_to_invite_links_af191ab3')}</button
        >
      </div>
    {/if}
    <div class="layout">
      <nav aria-label={$t('ui_application_sections_43a5ee22')}>
        <span class="nav-label">{$t('ui_configure_your_app_91a4e22a')}</span>
        {#each panels as panel, index (panel.id)}
          {#if index === 3}<span class="nav-label nav-divider"
              >{$t('ui_build_manage_cafdba3c')}</span
            >{/if}
          <button
            class:active={activePanel === panel.id}
            aria-current={activePanel === panel.id ? 'page' : undefined}
            aria-controls={`${panel.id}-panel`}
            onclick={() => selectPanel(panel.id)}
          >
            <span class="nav-symbol" aria-hidden="true">{panel.symbol}</span><span
              ><strong>{panel.title}</strong><small>{panel.subtitle}</small
              >{#if panel.id === 'commands' && commandsDirty}<small class="draft-marker"
                  >{$t('ui_unpublished_changes_1a020bb7')}</small
                >{/if}</span
            >
          </button>
        {/each}
        <p class="nav-tip">{$t('ui_settings_save_together_commands_have_their_ow_f5d8b87b')}</p>
      </nav>
      <div class="sections">
        <div class="panel-heading">
          <span class="panel-eyebrow">{currentPanel.title}</span>
          <h2>{currentPanel.heading}</h2>
          <p>{currentPanel.description}</p>
        </div>
        <div id="general-panel" class="panel-sections" hidden={activePanel !== 'general'}>
          <section id="general">
            <h2>{$t('ui_general_information_3960ba72')}</h2>
            <div class="grid">
              <label
                >{$t('ui_name_dcd1d522')}<input
                  bind:value={application.name}
                  maxlength="100"
                /></label
              ><label>{$t('ui_status_920e413c')}<input value={application.status} disabled /></label
              >
            </div>
            <label
              >{$t('ui_description_526e0087')}<textarea
                bind:value={application.description}
                rows="3"
                maxlength="1000"
              ></textarea></label
            >
          </section>
          <section aria-labelledby="bot-reference-heading">
            <h2 id="bot-reference-heading">{$t('ui_bot_identity_78c13cf0')}</h2>
            <p>
              {$t('ui_the_snowflake_is_your_bot_s_numeric_id_combin_5305add8')}
              <code>bot_ref</code>.
            </p>
            <div class="identity-grid">
              <div>
                <label for="bot-snowflake">{$t('ui_bot_id_snowflake_43ade0e3')}</label>
                <div class="identity-value">
                  <input id="bot-snowflake" value={application.bot_user.id} readonly />
                  <button class="secondary" onclick={() => copy(application!.bot_user.id, 'Bot ID')}
                    >{$t('ui_copy_id_72ac0d58')}</button
                  >
                </div>
              </div>
              <div>
                <label for="bot-reference">{$t('ui_bot_reference_d7d6c461')}</label>
                <div class="identity-value">
                  <input
                    id="bot-reference"
                    value={`${application.bot_user.id}@${application.bot_user.origin_domain}`}
                    readonly
                  />
                  <button
                    class="secondary"
                    onclick={() =>
                      copy(
                        `${application!.bot_user.id}@${application!.bot_user.origin_domain}`,
                        'Bot reference'
                      )}>{$t('ui_copy_ref_a115ec27')}</button
                  >
                </div>
              </div>
              <div>
                <label for="bot-username">{$t('ui_full_bot_username_84cd8554')}</label>
                <div class="identity-value">
                  <input id="bot-username" value={application.bot_user.handle} readonly />
                  <button
                    class="secondary"
                    onclick={() => copy(application!.bot_user.handle, 'Bot username')}
                    >{$t('ui_copy_username_7ba3cdab')}</button
                  >
                </div>
              </div>
            </div>
            <p class="identity-help">
              {$t('ui_for_tools_asking_for_73aeb7dd')} <code>application_ref</code>{$t(
                'ui_use_6195382a'
              )} <code>{application.ref}</code>
              instead.
            </p>
          </section>
          <section id="media">
            <h2>{$t('ui_bot_profile_652c08c4')}</h2>
            <p>{$t('ui_give_your_bot_a_profile_picture_and_banner_th_feb536a9')}</p>
            <ApplicationMediaManager
              applicationRef={ref}
              bind:iconHash={application.icon_hash}
              bind:bannerHash={application.banner_hash}
              onAssetsChange={handleAssetsChange}
            />
          </section>
          <div class="setup-heading">
            <h2>{$t('ui_keep_building_9af6b1c1')}</h2>
            <p>{$t('ui_pick_the_next_step_for_your_app_your_drafts_s_c67a163e')}</p>
          </div>
          <div class="setup-cards">
            <button onclick={() => selectPanel('directory')}
              ><span>{$t('ui_01_present_your_app_21801ed4')}</span><strong
                >{$t('ui_create_a_directory_listing_fb560fcf')}
                <span aria-hidden="true">↗</span></strong
              ><small>{$t('ui_add_a_summary_screenshots_and_support_links_d5a2d7b9')}</small
              ></button
            >
            <button onclick={() => selectPanel('commands')}
              ><span>{$t('ui_02_add_interactions_592aceb8')}</span><strong
                >{$t('ui_build_your_first_command_5557bd26')}
                <span aria-hidden="true">↗</span></strong
              ><small>{$t('ui_create_slash_commands_and_actions_with_a_guid_dc2fcf33')}</small
              ></button
            >
            <button onclick={() => selectPanel('deployment')}
              ><span>{$t('ui_03_connect_your_code_f42def75')}</span><strong
                >{$t('ui_set_up_your_bot_f65862aa')} <span aria-hidden="true">↗</span></strong
              ><small>{$t('ui_manage_credentials_and_the_workers_that_respo_c845d00c')}</small
              ></button
            >
          </div>
        </div>
        <div id="directory-panel" class="panel-sections" hidden={activePanel !== 'directory'}>
          <section id="discovery-settings">
            <h2>{$t('ui_discovery_settings_97d48058')}</h2>
            <p>{$t('ui_describe_how_your_app_appears_in_the_desktop__98a25ffa')}</p>
            <div class="grid">
              <label
                >{$t('ui_category_292c06f0')}<select bind:value={application.directory_category}
                  ><option value={null}>{$t('ui_choose_a_category_42d2f266')}</option><option
                    value="entertainment">{$t('ui_entertainment_ceaa553e')}</option
                  ><option value="games">{$t('ui_games_d9dae781')}</option><option
                    value="moderation">{$t('ui_moderation_126d4415')}</option
                  ><option value="productivity">{$t('ui_productivity_f42bca63')}</option><option
                    value="social">{$t('ui_social_f1b7505a')}</option
                  ><option value="utilities">{$t('ui_utilities_0a035c2b')}</option></select
                ></label
              ><label
                >{$t('ui_tags_1331275b')}<input
                  bind:value={directoryTags}
                  placeholder={$t('ui_moderation_utility_community_68b4af19')}
                /><small>{$t('ui_1_5_unique_lowercase_tags_separated_by_commas_e3b39c85')}</small
                ></label
              >
            </div>
            <label
              >{$t('ui_directory_summary_a01017e6')}<textarea
                bind:value={application.directory_summary}
                rows="2"
                maxlength="200"
                placeholder={$t('ui_a_short_explanation_of_what_your_app_helps_pe_06e4f1b8')}
              ></textarea></label
            >
            <div class="grid">
              <label
                >{$t('ui_support_url_6cca2fba')}<input
                  type="url"
                  bind:value={application.support_url}
                  placeholder="https://support.example"
                /></label
              ><label
                >{$t('ui_privacy_policy_url_ce306668')}<input
                  type="url"
                  bind:value={application.privacy_url}
                  placeholder="https://example/privacy"
                /></label
              >
            </div>
            <label
              >{$t('ui_terms_of_service_url_20185ca0')}<input
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
                <h2>{$t('ui_discovery_status_8d9e51d9')}</h2>
                <p>{$t('ui_review_the_saved_publication_checklist_before_3d286a74')}</p>
              </div>
              <button
                class="secondary"
                onclick={() => void loadDirectoryPreview(ref)}
                disabled={previewLoading}
                >{previewLoading ? $t('ui_refreshing_1c0def7b') : $t('ui_refresh_0e916101')}</button
              >
            </div>
            <label
              >{$t('ui_directory_status_cf9de437')}<input
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
                ><strong>{$t('ui_list_in_the_app_directory_47855e09')}</strong><small
                  >{$t('ui_your_home_instance_reviews_the_listing_before_7d961ff2')}</small
                ></span
              ></label
            >
            {#if previewError}<p class="warning" role="alert">{previewError}</p>{/if}
            {#if directoryPreview}
              <div class="readiness-summary">
                <strong
                  >{directoryPreview.readiness.status === 'approved'
                    ? $t('ui_approved_87b42e40')
                    : directoryPreview.readiness.status === 'ready_for_review'
                      ? $t('ui_ready_for_review_75c2a5c8')
                      : $t('ui_incomplete_75f33cdf')}</strong
                >
                <small>{$t('ui_this_checklist_reflects_your_last_saved_setti_3bf06151')}</small>
              </div>
              <ul class="checklist">
                {#each directoryPreview.readiness.items as item (item.key)}
                  <li class:ready={item.ready}>
                    <span aria-hidden="true">{item.ready ? '✓' : '○'}</span>
                    {readinessLabels[item.key]}
                  </li>
                {/each}
              </ul>
            {:else if previewLoading}<p>{$t('ui_loading_readiness_dabdd1b0')}</p>{/if}
            {#if application.directory_enabled && !application.directory_approved}
              <p>{$t('ui_your_saved_listing_will_await_approval_from_y_56d17dbe')}</p>
            {/if}
          </section>
          <section id="discovery-preview">
            <div class="section-title-row">
              <div>
                <h2>{$t('ui_product_page_preview_ba9e20de')}</h2>
                <p>{$t('ui_preview_the_saved_listing_that_reviewers_and__08dcb16d')}</p>
              </div>
            </div>
            {#if directoryPreview?.application}
              {@const product = directoryPreview.application}
              <article class="product-preview">
                <div class="preview-identity">
                  <span class="preview-icon">{product.name.slice(0, 1).toUpperCase()}</span>
                  <div>
                    <small>{product.category ?? $t('ui_category_not_set_91a910aa')}</small>
                    <h3>{product.name}{product.verified ? ' ✓' : ''}</h3>
                    <p>
                      {product.summary ?? $t('ui_add_a_summary_to_complete_this_preview_b59a1799')}
                    </p>
                  </div>
                </div>
                <p class="preview-description">
                  {product.description ??
                    $t('ui_add_a_description_to_complete_this_preview_db56cbee')}
                </p>
                {#if product.media.length}
                  <div
                    class="preview-media"
                    aria-label={`${product.media.length} product media items`}
                  >
                    {#each product.media as item (`${item.type}:${item.type === 'image' ? item.asset_id : item.video_id}`)}
                      <span>{item.type === 'image' ? item.name : `YouTube · ${item.video_id}`}</span
                      >
                    {/each}
                  </div>
                {/if}
                <div class="preview-meta">
                  <span>{product.tags.join(' · ')}</span>
                  <span
                    >{product.install_template?.name ??
                      $t('ui_install_path_not_configured_d2f6f538')}</span
                  >
                  {#if product.supported_locales.length}<span
                      >{$t('ui_value0_supported_language_value1_805a269a', {
                        value0: String(product.supported_locales.length),
                        value1: String(product.supported_locales.length === 1 ? '' : 's')
                      })}</span
                    >{/if}
                </div>
              </article>
            {:else if previewLoading}
              <p>{$t('ui_loading_product_preview_37ea6af0')}</p>
            {:else if !previewError}<p>{$t('ui_product_preview_unavailable_e30623dc')}</p>{/if}
          </section>
        </div>
        <div id="access-panel" class="panel-sections" hidden={activePanel !== 'access'}>
          <section id="access">
            <h2>{$t('ui_api_access_923fd434')}</h2>
            <p>{$t('ui_scopes_control_what_the_bot_can_request_inten_6b4c081d')}</p>
            <div class="section-title-row access-heading">
              <h3>{$t('ui_scopes_0d5644ff')}</h3>
              <span
                >{$t('ui_value0_selected_975acbfe', {
                  value0: String(application.default_scopes.length)
                })}</span
              >
            </div>
            <label class="scope-search"
              >{$t('ui_search_scopes_4072c9de')}<input
                type="search"
                bind:value={accessSearch}
                placeholder={$t('ui_filter_by_name_e_g_messages_758bc50b')}
              /></label
            >
            <div class="chips scope-grid">
              {#each visibleScopes as scope (scope)}<label
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
            {#if visibleScopes.length === 0}<p>
                {$t('ui_no_scopes_match_your_search_d171c91a')}
              </p>{/if}
            <div class="section-title-row access-heading">
              <h3>{$t('ui_gateway_intents_cf01b087')}</h3>
              <span
                >{$t('ui_value0_selected_975acbfe', {
                  value0: String(application.default_intents.length)
                })}</span
              >
            </div>
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
                >{$t('ui_target_policy_67430900')}<select bind:value={application.target_policy}
                  ><option value="open">{$t('ui_open_federation_d1174896')}</option><option
                    value="allowlist">{$t('ui_allowlist_only_07b99b6b')}</option
                  ><option value="blocklist"
                    >{$t('ui_open_except_blocked_instances_aad789d8')}</option
                  ><option value="local_only">{$t('ui_local_instance_only_c7bf2ec3')}</option
                  ></select
                ></label
              >
            </div>
            <h3>{$t('ui_installation_contexts_075d2fd6')}</h3>
            <p>{$t('ui_choose_where_discord_style_add_app_authorizat_154a5fa8')}</p>
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
              <h3>{$t('ui_user_install_scopes_a673a0bc')}</h3>
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
              <h3>{$t('ui_user_install_command_contexts_14cab2cc')}</h3>
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
            <p class="warning">{$t('ui_message_content_and_history_remain_unavailabl_fd99caf8')}</p>
          </section>
          <section id="federation">
            <h2>{$t('ui_federated_instance_policy_40316a71')}</h2>
            <p>{$t('ui_rules_match_exact_verified_instance_domains_d_ea65ceb9')}</p>
            <div class="inline">
              <input bind:value={ruleDomain} placeholder="instance.example" /><select
                bind:value={ruleEffect}
                ><option value="deny">{$t('ui_deny_05a2d733')}</option><option value="allow"
                  >{$t('ui_allow_e213c161')}</option
                ></select
              ><button onclick={addRule}>{$t('ui_add_rule_a27cff51')}</button>
            </div>
            <div class="rows">
              {#each rules as rule (rule.target_domain)}<article>
                  <code>{rule.target_domain}</code><span class:revoked={rule.effect === 'deny'}
                    >{rule.effect}</span
                  ><button onclick={() => deleteRule(rule.target_domain)}
                    >{$t('ui_remove_c3812fc4')}</button
                  >
                </article>{/each}
            </div>
          </section>
        </div>
        <div id="commands-panel" class="panel-sections" hidden={activePanel !== 'commands'}>
          <section id="commands">
            <ApplicationCommandEditor
              value={commandsText}
              onChange={updateCommands}
              disabled={busy}
              installTypes={JSON.parse(savedSettings).application.supported_install_types}
            />
            {#if commandError}<p class="command-error" role="alert">{commandError}</p>{/if}
            {#if commandNotice}<p class="command-success" role="status">{commandNotice}</p>{/if}
          </section>
        </div>
        <div id="deployment-panel" class="panel-sections" hidden={activePanel !== 'deployment'}>
          <section id="credentials">
            <h2>{$t('ui_control_credentials_e4590f5d')}</h2>
            <p>{$t('ui_deployment_tools_use_these_scoped_secrets_onl_d092f56b')}</p>
            <div class="inline">
              <input
                bind:value={credentialLabel}
                maxlength="100"
                placeholder={$t('ui_deployment_870a8ffd')}
              /><button onclick={createCredential} disabled={busy || !credentialLabel.trim()}
                >{$t('ui_create_credential_4d8f1172')}</button
              >
            </div>
            {#if credentialToken}
              <div class="secret" role="status">
                <strong>{$t('ui_copy_this_token_now_d7db5a6e')}</strong><code
                  >{credentialToken}</code
                ><button onclick={() => copy(credentialToken, 'Credential')}
                  >{$t('ui_copy_e21f935f')}</button
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
                    >{credential.revoked_at
                      ? $t('ui_revoked_f6f738d0')
                      : $t('ui_active_92340695')}</span
                  >{#if !credential.revoked_at}<button
                      class="danger"
                      onclick={() => revokeCredential(credential.id)}
                      >{$t('ui_revoke_87e6d00b')}</button
                    >{/if}
                </article>{/each}
            </div>
          </section>
          <section id="workers">
            <h2>{$t('ui_worker_keys_32de92f0')}</h2>
            <p>{$t('ui_a_worker_signs_short_lived_token_assertions_a_730723a3')}</p>
            <div class="grid">
              <label>{$t('ui_worker_name_73f1a739')}<input bind:value={workerName} /></label><label
                >{$t('ui_ed25519_public_key_base64url_376654e9')}<input
                  bind:value={workerKey}
                  placeholder={$t('ui_43_character_public_key_11f315fc')}
                /></label
              >
            </div>
            <label
              >{$t('ui_target_domains_comma_separated_empty_means_an_5f34f0fa')}<input
                bind:value={workerTargets}
                placeholder={$t('ui_chat_example_community_example_724bfe02')}
              /></label
            ><button onclick={createWorker} disabled={busy || workerKey.length < 43}
              >{$t('ui_enroll_worker_56dd9f32')}</button
            >
            <div class="rows">
              {#each workers as worker (worker.id)}<article>
                  <div>
                    <strong>{worker.name}</strong><small
                      >#{worker.id} · {worker.target_domains.join(', ') ||
                        $t('ui_all_approved_targets_d0c8a4b1')}</small
                    >
                  </div>
                  <span class:revoked={worker.revoked_at}
                    >{worker.revoked_at
                      ? $t('ui_revoked_f6f738d0')
                      : $t('ui_active_92340695')}</span
                  >{#if !worker.revoked_at}<button
                      class="danger"
                      onclick={() => revokeWorker(worker.id)}>{$t('ui_revoke_87e6d00b')}</button
                    >{/if}
                </article>{/each}
            </div>
          </section>
        </div>
        <div id="distribution-panel" class="panel-sections" hidden={activePanel !== 'distribution'}>
          <section id="invites">
            <h2>{$t('ui_bot_invite_links_9fe9f9e7')}</h2>
            <p>{$t('ui_invite_pages_show_the_app_origin_requested_pe_cbc07163')}</p>
            <div class="grid">
              <label>{$t('ui_slug_d15387ec')}<input bind:value={templateSlug} /></label><label
                >{$t('ui_invite_name_d596f9ea')}<input bind:value={templateName} /></label
              >
            </div>
            <label>{$t('ui_description_526e0087')}<input bind:value={templateDescription} /></label
            ><button onclick={createTemplate} disabled={busy}
              >{$t('ui_create_invite_link_f71e6194')}</button
            >
            <div class="rows">
              {#each templates as template (template.id)}<article>
                  <div>
                    <strong>{template.name}</strong>
                    <small
                      >{$t('ui_invite_link_value0_451dde78', {
                        value0: String(template.active ? 'Active' : 'Disabled')
                      })}</small
                    >
                    <small>
                      {$t('ui_encrypted_chat_participation_value0_a6f65c6d', {
                        value0: String(
                          template.e2ee_mode === 'participant' ? 'Enabled' : 'Disabled'
                        )
                      })}
                    </small>
                  </div>
                  <code>{template.invite_url}</code><button
                    onclick={() => copy(template.invite_url)}>{$t('ui_copy_e21f935f')}</button
                  >
                </article>{/each}
            </div>
          </section>
          <section id="installations">
            <h2>{$t('ui_installations_df2bd1e0')}</h2>
            <div class="rows">
              {#each installations as installation (installation.id)}<article>
                  <div>
                    <strong>{installation.guild_ref}</strong><small
                      >{$t('ui_value0_revision_value1_value2_6b5665d8', {
                        value0: String(installation.e2ee_mode),
                        value1: String(installation.grant_revision),
                        value2: String(
                          installation.channel_restrictions.length
                            ? `${installation.channel_restrictions.length} channel restrictions`
                            : 'all role-permitted channels'
                        )
                      })}</small
                    >
                  </div>
                  <span class:revoked={installation.status !== 'active'}>{installation.status}</span
                  >
                </article>{/each}{#if installations.length === 0}<p>
                  {$t('ui_no_guilds_have_installed_this_application_ea04990e')}
                </p>{/if}
            </div>
          </section>
        </div>
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
    padding: 1.5rem clamp(1rem, 4vw, 4rem) 11rem;
    background: var(--bg);
    color: var(--text);
  }
  .top {
    display: grid;
    grid-template-columns: 230px minmax(0, 1fr);
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
  .save-card button,
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
  .save-card {
    position: fixed;
    z-index: 30;
    right: 1.5rem;
    bottom: calc(1.5rem + env(safe-area-inset-bottom, 0px));
    display: flex;
    align-items: center;
    gap: 1.25rem;
    max-width: min(560px, calc(100vw - 3rem));
    box-sizing: border-box;
    padding: 1rem;
    border: 1px solid var(--line);
    border-radius: 14px;
    background: var(--surface);
    box-shadow: 0 8px 32px #0003;
  }
  .command-save-card {
    flex-wrap: wrap;
    max-width: min(620px, calc(100vw - 3rem));
  }
  .command-save-card .save-status {
    flex: 1 1 100%;
  }
  .save-actions {
    display: flex;
    gap: 0.6rem;
    margin-left: auto;
    flex-wrap: wrap;
  }
  .save-actions .secondary {
    background: transparent;
    color: var(--text);
    border: 1px solid var(--line);
    font-weight: 600;
  }
  .save-status {
    display: grid;
    gap: 0.3rem;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .save-status small,
  .access-heading span {
    color: var(--text-muted);
  }
  .save-card button {
    flex-shrink: 0;
  }
  button:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .access-heading {
    align-items: baseline;
    margin-top: 1.3rem;
  }
  .access-heading h3 {
    margin: 0 0 0.6rem;
  }
  .scope-search {
    margin-top: 0;
  }
  .chips.scope-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 230px), 1fr));
  }
  .chips label:focus-within {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
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
    grid-template-columns: 230px minmax(0, 1fr);
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
  .layout > nav button {
    border-radius: 7px;
    padding: 0.55rem 0.7rem;
    color: var(--text-muted);
    text-decoration: none;
  }
  .layout > nav button:hover {
    color: var(--text);
    background: var(--surface-hover);
  }
  .sections {
    min-width: 0;
  }
  .panel-sections {
    display: grid;
    gap: 1.25rem;
  }
  .panel-sections[hidden] {
    display: none;
  }
  .panel-heading {
    margin: 0.2rem 0 1.5rem;
  }
  .panel-eyebrow {
    color: var(--accent);
    font-size: 0.72rem;
    font-weight: 750;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .panel-heading h2 {
    margin: 0.5rem 0;
    font-size: clamp(1.4rem, 2.5vw, 1.9rem);
    letter-spacing: -0.03em;
  }
  .panel-heading p {
    margin: 0;
    color: var(--text-muted);
    line-height: 1.6;
    font-size: 0.9rem;
  }
  .app-heading {
    display: flex;
    align-items: center;
    gap: 1rem;
    min-width: 0;
  }
  .app-heading > div:not(.app-monogram) {
    min-width: 0;
  }
  .app-heading h1 {
    font-size: clamp(1.4rem, 2.5vw, 2rem);
    letter-spacing: -0.03em;
    overflow-wrap: anywhere;
  }
  .app-heading p {
    font-size: 0.8rem;
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }
  .app-monogram {
    display: grid;
    place-items: center;
    flex: 0 0 56px;
    height: 56px;
    border-radius: 16px;
    background: color-mix(in srgb, var(--accent) 12%, var(--surface));
    border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--line));
    color: var(--accent);
    font-size: 1.5rem;
    font-weight: 800;
  }
  .app-monogram img {
    width: 56px;
    height: 56px;
    object-fit: cover;
    border-radius: inherit;
  }
  .app-status {
    align-self: start;
    margin-left: auto;
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 0.35rem 0.65rem;
    font-size: 0.7rem;
    color: var(--text-muted);
    background: var(--surface);
    text-transform: capitalize;
    white-space: nowrap;
  }
  .nav-label {
    padding: 0.5rem 0.75rem;
    font-size: 0.65rem;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-weight: 750;
  }
  .nav-divider {
    margin-top: 1rem;
  }
  .layout > nav button {
    display: flex;
    align-items: start;
    gap: 0.75rem;
    border: 1px solid transparent;
    background: transparent;
    font: inherit;
    text-align: left;
    cursor: pointer;
    padding: 0.8rem;
  }
  .layout > nav button.active {
    border-color: color-mix(in srgb, var(--accent) 25%, var(--line));
    background: color-mix(in srgb, var(--accent) 9%, var(--surface));
    color: var(--text);
  }
  .nav-symbol {
    flex: 0 0 22px;
    font-size: 1.1rem;
    color: var(--accent);
    text-align: center;
  }
  .layout > nav strong {
    font-size: 0.8rem;
    font-weight: 650;
  }
  .layout > nav small {
    display: block;
    margin-top: 0.25rem;
    font-size: 0.68rem;
    color: var(--text-muted);
  }
  .layout > nav .draft-marker {
    color: var(--accent);
  }
  .nav-tip {
    border-top: 1px solid var(--line);
    margin: 1rem 0.75rem 0;
    padding-top: 1rem;
    color: var(--text-muted);
    font-size: 0.75rem;
    line-height: 1.6;
  }
  .setup-heading h2 {
    margin: 0.5rem 0;
    font-size: 1rem;
  }
  .setup-heading p {
    margin: 0;
    font-size: 0.85rem;
    color: var(--text-muted);
    line-height: 1.6;
  }
  .setup-cards {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 0.8rem;
  }
  .setup-cards button {
    display: grid;
    align-content: start;
    gap: 0.8rem;
    padding: 1.15rem;
    text-align: left;
    font: inherit;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 12px;
    cursor: pointer;
  }
  .setup-cards button:hover {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 4%, var(--surface));
  }
  .setup-cards button > span {
    font-size: 0.65rem;
    color: var(--accent);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 700;
  }
  .setup-cards strong {
    font-size: 0.85rem;
    line-height: 1.5;
  }
  .setup-cards small {
    font-size: 0.75rem;
    color: var(--text-muted);
    line-height: 1.6;
  }
  .command-error {
    color: var(--danger);
  }
  .command-success {
    color: var(--success);
  }
  section {
    scroll-margin-top: 1rem;
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: clamp(1rem, 2.5vw, 1.75rem);
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
    font-size: 0.88rem;
    line-height: 1.65;
  }
  label {
    display: grid;
    align-content: start;
    gap: 0.4rem;
    margin: 0.7rem 0;
    font-size: 0.8rem;
    font-weight: 700;
  }
  .grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    align-items: start;
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
  input:not([type='checkbox']):not([type='radio']),
  select {
    min-height: 2.75rem;
  }
  label > small {
    color: var(--text-muted);
    font-weight: 400;
    line-height: 1.5;
  }
  .identity-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    align-items: start;
    gap: 1rem;
  }
  @media (max-width: 1100px) {
    .identity-grid {
      grid-template-columns: minmax(0, 1fr);
    }
  }
  .identity-value {
    display: flex;
    align-items: stretch;
    gap: 0.5rem;
  }
  .identity-value input {
    min-width: 0;
    font-family: var(--font-mono, monospace);
  }
  .identity-value button {
    flex-shrink: 0;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.6rem 0.8rem;
    background: var(--surface-hover);
    color: var(--text);
    cursor: pointer;
  }
  .identity-help code {
    overflow-wrap: anywhere;
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
    border-radius: 8px;
    padding: 0.55rem 0.7rem;
    color: var(--text-muted);
    cursor: pointer;
  }
  .chips label.active {
    border-color: var(--accent);
    color: var(--text);
    background: color-mix(in srgb, var(--accent) 15%, transparent);
  }
  .chips input {
    width: auto;
    margin: 0 0.45rem 0 0;
    accent-color: var(--accent);
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
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: start;
    gap: 1rem;
    border-color: var(--success);
    overflow-wrap: anywhere;
  }
  .notice-dismiss {
    border: 0;
    color: inherit;
    background: none;
    font-size: 1.2rem;
    line-height: 1;
    padding: 0.25rem;
    cursor: pointer;
  }
  .activation-notice {
    display: grid;
    gap: 0.65rem;
    line-height: 1.6;
  }
  .activation-notice p {
    margin: 0;
    color: var(--text-muted);
  }
  .activation-action {
    justify-self: start;
    max-width: 100%;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.6rem 0.9rem;
    color: var(--text);
    background: var(--surface-hover);
    font: inherit;
    font-weight: 600;
    cursor: pointer;
  }
  @media (max-width: 760px) {
    .save-card {
      left: 1rem;
      right: 1rem;
      bottom: calc(1rem + env(safe-area-inset-bottom, 0px));
      max-width: none;
      gap: 0.75rem;
    }
    .save-status {
      flex: 1;
    }
    .command-save-card {
      max-width: none;
    }
    .command-save-card .save-status {
      flex: 1 1 100%;
    }
    .save-actions {
      width: 100%;
    }
    .save-actions button {
      flex: 1;
      font-size: 0.8rem;
      padding: 0.7rem;
    }
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
      margin: 0 -1rem 1.5rem;
      padding: 0 1rem 0.5rem;
      gap: 0.5rem;
    }
    .layout > nav button {
      flex-shrink: 0;
      padding: 0.65rem 0.8rem;
      align-items: center;
    }
    .layout > nav button small,
    .nav-label,
    .nav-tip {
      display: none;
    }
    .app-status {
      display: none;
    }
    .setup-cards {
      grid-template-columns: 1fr;
    }
    .top {
      gap: 1rem;
    }
    .top .app-heading {
      grid-column: 1/-1;
    }
    .panel-heading {
      margin-top: 0;
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
      padding: 1rem 1rem 13rem;
    }
  }
</style>
