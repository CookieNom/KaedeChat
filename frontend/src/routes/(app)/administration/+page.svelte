<script lang="ts">
  import { resolve } from '$app/paths';
  import { hasAdminCapability } from '$lib/admin/capabilities';
  import { api, userErrorMessage } from '$lib/api/client';
  import Icon, { type IconName } from '$lib/components/Icon.svelte';
  import { authenticatedMedia } from '$lib/media/authenticated';
  import { onMount } from 'svelte';

  type View =
    'overview' | 'users' | 'applications' | 'reports' | 'instances' | 'operators' | 'audit';

  interface AdminIdentity {
    roles: string[];
    capabilities: string[];
    user: {
      id: string;
      origin_domain: string;
      username: string;
      display_name: string | null;
    };
  }

  interface User {
    id: string;
    origin_domain: string;
    username: string;
    display_name: string | null;
    account_type?: string;
    disabled_at: string | null;
    suspended_until: string | null;
  }

  interface App {
    ref: string;
    name: string;
    status: string;
    team_ref: string;
    updated_at: string;
  }

  interface Report {
    id: string;
    source?: 'user' | 'photodna' | string;
    severity?: string;
    reporter_ref?: string | null;
    target_type: string;
    target_ref: string;
    category: string;
    description: string | null;
    message_ref?: string | null;
    evidence: {
      content?: string | null;
      author_ref?: string;
      channel_ref?: string;
      created_at?: string;
      disclosure?: { source?: string; server_verified?: boolean };
      [key: string]: unknown;
    };
    encryption_mode?: string;
    status: string;
    assigned_admin_ref?: string | null;
    created_at: string;
    updated_at?: string;
    resolution: string | null;
  }

  interface ReportDraft {
    status: string;
    resolution: string;
  }

  type AccountAction = 'none' | 'suspend_24h' | 'suspend_7d' | 'suspend_30d' | 'ban_permanent';
  type MessageAction =
    | 'none'
    | 'delete_reported'
    | 'delete_1h'
    | 'delete_24h'
    | 'delete_7d'
    | 'delete_30d'
    | 'delete_all';

  interface EnforcementDraft {
    account_action: AccountAction;
    message_action: MessageAction;
    reason: string;
  }

  interface EnforcementResponse {
    report: Report;
    enforcement: {
      subject_ref: string;
      account_action: AccountAction;
      suspended_until: string | null;
      banned: boolean;
      permanently_suspended: boolean;
      guild_memberships_removed: number;
      message_action: MessageAction;
      messages_deleted: number;
      messages_requiring_remote_action: number;
    };
  }

  interface PhotoDnaFlag {
    source: string;
    violations: string[];
    match_distance: number | null;
    match_id: string | null;
  }

  interface ReportAttachmentMetadata {
    attachment_ref: string;
    uploader_ref?: string;
    filename?: string;
    content_type?: string;
    size?: number;
    encryption_mode?: string;
  }

  interface Block {
    domain: string;
    level: 'silence' | 'suspend';
    include_subdomains: boolean;
    reason: string | null;
  }

  interface Operator {
    id: string;
    role: string;
    user: { id: string; origin_domain: string; username: string; display_name: string | null };
    created_at: string;
  }

  interface Audit {
    id: string;
    actor_ref: string | null;
    actor_kind: string;
    action: string;
    target_type: string;
    target_ref: string;
    metadata: Record<string, unknown>;
    created_at: string;
  }

  interface SectionDefinition {
    id: View;
    label: string;
    icon: IconName;
    description: string;
    capability: string;
  }

  const sections: SectionDefinition[] = [
    {
      id: 'overview',
      label: 'Overview',
      icon: 'server',
      description: 'Instance health and the work that needs attention.',
      capability: 'admin.read'
    },
    {
      id: 'users',
      label: 'Users',
      icon: 'users',
      description: 'Find local accounts and manage account access.',
      capability: 'admin.read'
    },
    {
      id: 'applications',
      label: 'Applications',
      icon: 'sparkles',
      description: 'Review bot applications and suspend unsafe integrations.',
      capability: 'admin.read'
    },
    {
      id: 'reports',
      label: 'Reports',
      icon: 'shield',
      description: 'Triage user and automated safety reports.',
      capability: 'reports.read'
    },
    {
      id: 'instances',
      label: 'Instances',
      icon: 'globe',
      description: 'Control federation with specific remote instances.',
      capability: 'admin.read'
    },
    {
      id: 'operators',
      label: 'Operators',
      icon: 'key',
      description: 'Delegate scoped administrative access.',
      capability: 'admin.read'
    },
    {
      id: 'audit',
      label: 'Audit log',
      icon: 'clock',
      description: 'Review recent administrative changes.',
      capability: 'audit.read'
    }
  ];

  const overviewCards = [
    ['local_users', 'Local users', 'Human and bot accounts hosted here'],
    ['known_instances', 'Known instances', 'Federation peers discovered by this instance'],
    ['applications', 'Applications', 'Registered bot applications'],
    ['active_installations', 'Active installations', 'Bots currently installed in guilds'],
    ['open_reports', 'Open reports', 'Safety cases awaiting a final outcome'],
    ['blocked_instances', 'Blocked instances', 'Active federation restrictions']
  ] as const;

  const reportStatuses = [
    'submitted',
    'triaged',
    'in_review',
    'awaiting_remote',
    'needs_information',
    'action_taken',
    'closed_no_action',
    'duplicate',
    'reopened'
  ];
  const closedReportStatuses = new Set(['action_taken', 'closed_no_action', 'duplicate']);
  const roleOptions = ['administrator', 'trust_safety', 'bot_reviewer', 'operations', 'auditor'];
  const accountActionOptions: { value: AccountAction; label: string }[] = [
    { value: 'none', label: 'No account restriction' },
    { value: 'suspend_24h', label: 'Suspend for 24 hours' },
    { value: 'suspend_7d', label: 'Suspend for 7 days' },
    { value: 'suspend_30d', label: 'Suspend for 30 days' },
    { value: 'ban_permanent', label: 'Ban permanently' }
  ];
  const messageActionOptions: { value: MessageAction; label: string }[] = [
    { value: 'none', label: 'Keep messages' },
    { value: 'delete_reported', label: 'Delete reported message' },
    { value: 'delete_1h', label: 'Delete messages from last hour' },
    { value: 'delete_24h', label: 'Delete messages from last 24 hours' },
    { value: 'delete_7d', label: 'Delete messages from last 7 days' },
    { value: 'delete_30d', label: 'Delete messages from last 30 days' },
    { value: 'delete_all', label: 'Delete all message history' }
  ];

  let me = $state<AdminIdentity | null>(null);
  let overview = $state<Record<string, number>>({});
  let users = $state<User[]>([]);
  let apps = $state<App[]>([]);
  let reports = $state<Report[]>([]);
  let reportDrafts = $state<Record<string, ReportDraft>>({});
  let enforcementDrafts = $state<Record<string, EnforcementDraft>>({});
  let blocks = $state<Block[]>([]);
  let operators = $state<Operator[]>([]);
  let audits = $state<Audit[]>([]);
  let view = $state<View>('overview');
  let identityLoading = $state(true);
  let globalError = $state('');
  let notice = $state('');
  let loading = $state<Record<View, boolean>>({
    overview: false,
    users: false,
    applications: false,
    reports: false,
    instances: false,
    operators: false,
    audit: false
  });
  let loaded = $state<Record<View, boolean>>({
    overview: false,
    users: false,
    applications: false,
    reports: false,
    instances: false,
    operators: false,
    audit: false
  });
  let sectionErrors = $state<Partial<Record<View, string>>>({});
  let busyAction = $state('');
  let userQuery = $state('');
  let blockDomain = $state('');
  let blockLevel = $state<'silence' | 'suspend'>('suspend');
  let blockReason = $state('');
  let blockSubdomains = $state(false);
  let operatorRef = $state('');
  let operatorRole = $state('administrator');
  let reportFilter = $state<'open' | 'closed' | 'all'>('open');

  const activeSection = $derived(sections.find((section) => section.id === view) ?? sections[0]);
  const visibleSections = $derived(
    sections.filter((section) => hasAdminCapability(me?.capabilities, section.capability))
  );
  const visibleReports = $derived(
    reports.filter((report) => {
      const closed = closedReportStatuses.has(report.status);
      return reportFilter === 'all' || (reportFilter === 'closed' ? closed : !closed);
    })
  );
  const openReportCount = $derived(
    reports.filter((report) => !closedReportStatuses.has(report.status)).length
  );

  function can(capability: string): boolean {
    return hasAdminCapability(me?.capabilities, capability);
  }

  function sectionBadge(section: View): number | null {
    if (section === 'reports') return openReportCount;
    if (section === 'instances') return blocks.length;
    return null;
  }

  function showError(caught: unknown, fallback: string): void {
    notice = '';
    globalError = userErrorMessage(caught, fallback);
  }

  function syncReportDrafts(nextReports: Report[]): void {
    reportDrafts = Object.fromEntries(
      nextReports.map((report) => [
        report.id,
        { status: report.status, resolution: report.resolution ?? '' }
      ])
    );
    enforcementDrafts = Object.fromEntries(
      nextReports.map((report) => [
        report.id,
        enforcementDrafts[report.id] ?? {
          account_action: 'none',
          message_action: 'none',
          reason: ''
        }
      ])
    );
  }

  function reportSubjectRef(report: Report): string | null {
    if (report.target_type === 'user') return report.target_ref;
    const key = report.target_type === 'attachment' ? 'uploader_ref' : 'author_ref';
    const value = report.evidence[key];
    return typeof value === 'string' && value ? value : null;
  }

  function localReportSubject(report: Report): string | null {
    const subject = reportSubjectRef(report);
    if (!subject || !me || !subject.endsWith(`@${me.user.origin_domain}`)) return null;
    return subject;
  }

  function attachmentContentType(report: Report): string | null {
    const disclosed = report.evidence.disclosed_content_type;
    const value = typeof disclosed === 'string' ? disclosed : report.evidence.content_type;
    return typeof value === 'string' ? value : null;
  }

  function reportPreviewAttachmentRef(report: Report): string | null {
    const disclosed = report.evidence.disclosed_attachment_ref;
    if (report.encryption_mode === 'e2ee_user_disclosed' && typeof disclosed === 'string') {
      return disclosed;
    }
    const original = report.evidence.attachment_ref;
    return typeof original === 'string' ? original : null;
  }

  function reportAttachments(report: Report): ReportAttachmentMetadata[] {
    const value = report.evidence.attachments;
    if (!Array.isArray(value)) return [];
    return value.filter(
      (item): item is ReportAttachmentMetadata =>
        typeof item === 'object' &&
        item !== null &&
        typeof (item as Record<string, unknown>).attachment_ref === 'string'
    );
  }

  function canPreviewReportAttachment(report: Report): boolean {
    const attachmentRef = reportPreviewAttachmentRef(report);
    const contentType = attachmentContentType(report);
    return Boolean(
      me &&
      attachmentRef &&
      attachmentRef.endsWith(`@${me.user.origin_domain}`) &&
      report.encryption_mode !== 'e2ee_metadata' &&
      contentType &&
      (report.encryption_mode !== 'e2ee_user_disclosed' ||
        report.evidence.disclosed_attachment_scan_status === 'clean') &&
      (contentType.startsWith('image/') || contentType.startsWith('video/'))
    );
  }

  function reportAttachmentPath(
    report: Report,
    variant: 'original' | 'thumbnail_512' | 'poster'
  ): string {
    return `/api/v1/administration/reports/${encodeURIComponent(report.id)}/attachment/${variant}`;
  }

  function hasEnforcementAction(report: Report): boolean {
    const draft = enforcementDrafts[report.id];
    return Boolean(
      draft &&
      draft.reason.trim().length >= 3 &&
      (draft.account_action !== 'none' || draft.message_action !== 'none')
    );
  }

  function userIsSuspended(user: User): boolean {
    return Boolean(
      user.disabled_at ||
      (user.suspended_until && new Date(user.suspended_until).getTime() > Date.now())
    );
  }

  function reportChanged(report: Report): boolean {
    const draft = reportDrafts[report.id];
    return Boolean(
      draft &&
      (draft.status !== report.status ||
        draft.resolution.trim() !== (report.resolution ?? '').trim())
    );
  }

  function photoDnaEvidence(report: Report, key: string): string | null {
    const value = report.evidence[key];
    return typeof value === 'string' && value.trim() ? value : null;
  }

  function photoDnaFlags(report: Report): PhotoDnaFlag[] {
    const value = report.evidence.match_flags;
    if (!Array.isArray(value)) return [];
    return value.flatMap((item) => {
      if (typeof item !== 'object' || item === null || Array.isArray(item)) return [];
      const candidate = item as Record<string, unknown>;
      if (typeof candidate.source !== 'string') return [];
      return [
        {
          source: candidate.source,
          violations: Array.isArray(candidate.violations)
            ? candidate.violations.filter((entry): entry is string => typeof entry === 'string')
            : [],
          match_distance:
            typeof candidate.match_distance === 'number' ? candidate.match_distance : null,
          match_id: typeof candidate.match_id === 'string' ? candidate.match_id : null
        }
      ];
    });
  }

  function clearFeedback(): void {
    globalError = '';
    notice = '';
  }

  async function loadSection(section: View): Promise<void> {
    if (!me) return;
    loading[section] = true;
    sectionErrors[section] = undefined;
    try {
      if (section === 'overview') overview = await api('/administration/overview');
      else if (section === 'users') users = await api('/administration/users');
      else if (section === 'applications') apps = await api('/administration/applications');
      else if (section === 'reports') {
        const nextReports = await api<Report[]>('/administration/reports');
        reports = nextReports;
        syncReportDrafts(nextReports);
      } else if (section === 'instances') blocks = await api('/administration/instances/blocks');
      else if (section === 'operators') operators = await api('/administration/operators');
      else audits = await api('/administration/audit');
      loaded[section] = true;
    } catch (caught) {
      sectionErrors[section] = userErrorMessage(caught, `Could not load ${section}.`);
    } finally {
      loading[section] = false;
    }
  }

  async function loadAdministration(): Promise<void> {
    identityLoading = true;
    globalError = '';
    try {
      me = await api<AdminIdentity>('/administration/@me');
      await Promise.all(
        sections
          .filter((section) => hasAdminCapability(me?.capabilities, section.capability))
          .map((section) => loadSection(section.id))
      );
    } catch (caught) {
      globalError = userErrorMessage(caught, 'Administration is unavailable for this account.');
    } finally {
      identityLoading = false;
    }
  }

  async function selectView(next: View): Promise<void> {
    view = next;
    if (!loaded[next] && !loading[next]) await loadSection(next);
  }

  async function refreshCurrent(): Promise<void> {
    clearFeedback();
    await loadSection(view);
  }

  async function searchUsers(): Promise<void> {
    clearFeedback();
    loading.users = true;
    sectionErrors.users = undefined;
    try {
      users = await api<User[]>(`/administration/users?query=${encodeURIComponent(userQuery)}`);
      loaded.users = true;
    } catch (caught) {
      sectionErrors.users = userErrorMessage(caught, 'Could not search users.');
    } finally {
      loading.users = false;
    }
  }

  async function patchUser(user: User): Promise<void> {
    const disabled = !userIsSuspended(user);
    if (!confirm(`${disabled ? 'Ban' : 'Restore access for'} ${user.username}?`)) return;
    clearFeedback();
    busyAction = `user:${user.id}@${user.origin_domain}`;
    try {
      const updated = await api<User>(`/administration/users/${user.id}@${user.origin_domain}`, {
        method: 'PATCH',
        body: JSON.stringify({ disabled, reason: null })
      });
      users = users.map((entry) =>
        entry.id === user.id && entry.origin_domain === user.origin_domain ? updated : entry
      );
      notice = `${user.username} was ${disabled ? 'banned' : 'restored'}.`;
      await loadSection('overview');
    } catch (caught) {
      showError(caught, 'Could not update the account.');
    } finally {
      busyAction = '';
    }
  }

  async function patchApp(app: App): Promise<void> {
    const status = app.status === 'suspended' ? 'active' : 'suspended';
    if (!confirm(`${status === 'suspended' ? 'Suspend' : 'Activate'} ${app.name}?`)) return;
    clearFeedback();
    busyAction = `app:${app.ref}`;
    try {
      const updated = await api<{ status: string }>(
        `/administration/applications/${encodeURIComponent(app.ref)}`,
        { method: 'PATCH', body: JSON.stringify({ status, reason: null }) }
      );
      apps = apps.map((entry) =>
        entry.ref === app.ref ? { ...entry, status: updated.status } : entry
      );
      notice = `${app.name} is now ${updated.status}.`;
    } catch (caught) {
      showError(caught, 'Could not update the application.');
    } finally {
      busyAction = '';
    }
  }

  async function patchReport(report: Report): Promise<void> {
    clearFeedback();
    busyAction = `report:${report.id}`;
    try {
      const draft = reportDrafts[report.id] ?? {
        status: report.status,
        resolution: report.resolution ?? ''
      };
      const updated = await api<Report>(`/administration/reports/${report.id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          status: draft.status,
          resolution: draft.resolution.trim() || null
        })
      });
      reports = reports.map((entry) => (entry.id === report.id ? updated : entry));
      reportDrafts[report.id] = {
        status: updated.status,
        resolution: updated.resolution ?? ''
      };
      notice = `Report #${report.id} was updated.`;
      await loadSection('overview');
    } catch (caught) {
      showError(caught, 'Could not update the report.');
      await loadSection('reports');
    } finally {
      busyAction = '';
    }
  }

  async function enforceReport(report: Report): Promise<void> {
    const draft = enforcementDrafts[report.id];
    const subject = reportSubjectRef(report);
    if (!draft || !subject || !hasEnforcementAction(report)) return;
    const selected = [
      accountActionOptions.find((option) => option.value === draft.account_action)?.label,
      messageActionOptions.find((option) => option.value === draft.message_action)?.label
    ].filter((label) => label && !label.startsWith('No ') && label !== 'Keep messages');
    if (
      !confirm(
        `Apply to ${subject}?\n\n${selected.join('\n')}${
          draft.message_action === 'none' ? '' : '\n\nMessage deletion cannot be undone.'
        }`
      )
    )
      return;
    clearFeedback();
    busyAction = `enforce:${report.id}`;
    try {
      const result = await api<EnforcementResponse>(
        `/administration/reports/${report.id}/actions`,
        {
          method: 'POST',
          body: JSON.stringify({
            account_action: draft.account_action,
            message_action: draft.message_action,
            reason: draft.reason.trim()
          })
        }
      );
      reports = reports.map((entry) => (entry.id === report.id ? result.report : entry));
      reportDrafts[report.id] = {
        status: result.report.status,
        resolution: result.report.resolution ?? ''
      };
      enforcementDrafts[report.id] = {
        account_action: 'none',
        message_action: 'none',
        reason: ''
      };
      const deleted = result.enforcement.messages_deleted;
      const accountResult = result.enforcement.banned
        ? `Account banned.${result.enforcement.guild_memberships_removed ? ` Removed from ${result.enforcement.guild_memberships_removed} local guild(s).` : ''}`
        : result.enforcement.suspended_until
          ? `Account suspended until ${new Date(result.enforcement.suspended_until).toLocaleString()}.`
          : '';
      const remoteResult = result.enforcement.messages_requiring_remote_action
        ? ` ${result.enforcement.messages_requiring_remote_action} message(s) require remote moderation.`
        : '';
      notice =
        `${accountResult}${deleted ? ` ${deleted} message(s) deleted.` : ''}${remoteResult}`.trim();
      const refreshes = [loadSection('overview'), loadSection('users')];
      if (can('audit.read')) refreshes.push(loadSection('audit'));
      await Promise.all(refreshes);
    } catch (caught) {
      showError(caught, 'Could not apply the report enforcement action.');
    } finally {
      busyAction = '';
    }
  }

  async function addBlock(): Promise<void> {
    if (!blockDomain.trim()) return;
    clearFeedback();
    busyAction = 'block:add';
    try {
      await api('/administration/instances/blocks', {
        method: 'PUT',
        body: JSON.stringify({
          domain: blockDomain.trim(),
          level: blockLevel,
          include_subdomains: blockSubdomains,
          reason: blockReason.trim() || null
        })
      });
      blockDomain = '';
      blockReason = '';
      blockSubdomains = false;
      notice = 'Federation policy updated.';
      await Promise.all([loadSection('instances'), loadSection('overview')]);
    } catch (caught) {
      showError(caught, 'Could not update federation policy.');
    } finally {
      busyAction = '';
    }
  }

  async function removeBlock(domain: string): Promise<void> {
    if (!confirm(`Remove the block for ${domain}?`)) return;
    clearFeedback();
    busyAction = `block:${domain}`;
    try {
      await api(`/administration/instances/blocks/${encodeURIComponent(domain)}`, {
        method: 'DELETE'
      });
      blocks = blocks.filter((entry) => entry.domain !== domain);
      notice = `Federation policy for ${domain} was removed.`;
      await loadSection('overview');
    } catch (caught) {
      showError(caught, 'Could not remove federation policy.');
    } finally {
      busyAction = '';
    }
  }

  async function addOperator(): Promise<void> {
    if (!operatorRef.trim()) return;
    clearFeedback();
    busyAction = 'operator:add';
    try {
      await api('/administration/operators', {
        method: 'POST',
        body: JSON.stringify({ user_ref: operatorRef.trim(), role: operatorRole })
      });
      operatorRef = '';
      notice = 'Administrative role granted.';
      await loadSection('operators');
    } catch (caught) {
      showError(caught, 'Could not grant the role.');
    } finally {
      busyAction = '';
    }
  }

  async function removeOperator(operator: Operator): Promise<void> {
    if (
      operator.role === 'owner' ||
      !confirm(`Revoke ${operator.role.replaceAll('_', ' ')} from ${operator.user.username}?`)
    )
      return;
    clearFeedback();
    busyAction = `operator:${operator.id}`;
    try {
      await api(`/administration/operators/${operator.id}`, { method: 'DELETE' });
      operators = operators.filter((entry) => entry.id !== operator.id);
      notice = `Role revoked from ${operator.user.username}.`;
    } catch (caught) {
      showError(caught, 'Could not revoke the role.');
    } finally {
      busyAction = '';
    }
  }

  onMount(() => {
    void loadAdministration();
    const reportRefresh = window.setInterval(() => {
      if (view === 'reports' && me && !loading.reports && !document.hidden) {
        void loadSection('reports');
      }
    }, 15_000);
    return () => window.clearInterval(reportRefresh);
  });
</script>

<svelte:head><title>Instance Administration · Kaede Chat</title></svelte:head>

<main class="admin-shell">
  <aside class="sidebar">
    <div class="sidebar-top">
      <a class="back-link" href={resolve('/settings')}>
        <Icon name="arrow-left" size={18} />
        <span>User settings</span>
      </a>
      <div class="instance-label">
        <span>Instance control</span>
        <h1>Administration</h1>
      </div>
      {#if me}
        <div class="identity">
          <div class="identity-avatar">
            {(me.user.display_name ?? me.user.username)[0].toUpperCase()}
          </div>
          <div>
            <strong>{me.user.display_name ?? me.user.username}</strong>
            <small>{me.roles.map((role) => role.replaceAll('_', ' ')).join(' · ')}</small>
          </div>
        </div>
      {/if}
    </div>

    <nav aria-label="Administration sections">
      {#each visibleSections as section (section.id)}
        <button
          type="button"
          class:active={view === section.id}
          aria-current={view === section.id ? 'page' : undefined}
          onclick={() => void selectView(section.id)}
        >
          <Icon name={section.icon} size={19} />
          <span>{section.label}</span>
          {#if (sectionBadge(section.id) ?? 0) > 0}
            <small class="nav-badge">{sectionBadge(section.id)}</small>
          {/if}
        </button>
      {/each}
    </nav>

    <div class="sidebar-footer">
      <span class="status-dot"></span>
      <span>Authenticated with your user session</span>
    </div>
  </aside>

  <section class="content">
    <div class="content-inner">
      <header class="page-header">
        <div>
          <span class="eyebrow">Instance control center</span>
          <h2>{activeSection.label}</h2>
          <p>{activeSection.description}</p>
        </div>
        <div class="header-actions">
          <button
            type="button"
            class="icon-button"
            aria-label={`Refresh ${activeSection.label}`}
            title={`Refresh ${activeSection.label}`}
            disabled={loading[view] || identityLoading}
            onclick={() => void refreshCurrent()}>↻</button
          >
          <a class="icon-button" href={resolve('/home')} aria-label="Close administration">×</a>
        </div>
      </header>

      {#if globalError}<div class="feedback error" role="alert">{globalError}</div>{/if}
      {#if notice}<div class="feedback success" role="status">{notice}</div>{/if}

      {#if identityLoading}
        <section class="state-card" aria-live="polite">
          <span class="spinner"></span>
          <div>
            <h3>Loading administration</h3>
            <p>Checking your roles and instance data…</p>
          </div>
        </section>
      {:else if !me}
        <section class="state-card error-state">
          <Icon name="shield" size={30} />
          <div>
            <h3>Administration unavailable</h3>
            <p>{globalError || 'This account does not have administrative access.'}</p>
          </div>
        </section>
      {:else if sectionErrors[view]}
        <section class="state-card error-state" role="alert">
          <Icon name="shield" size={28} />
          <div>
            <h3>This section could not load</h3>
            <p>{sectionErrors[view]}</p>
          </div>
          <button type="button" class="secondary-button" onclick={() => void refreshCurrent()}
            >Try again</button
          >
        </section>
      {:else if loading[view] && !loaded[view]}
        <section class="state-card" aria-live="polite">
          <span class="spinner"></span>
          <div>
            <h3>Loading {activeSection.label.toLowerCase()}</h3>
            <p>Fetching the latest instance data…</p>
          </div>
        </section>
      {:else if view === 'overview'}
        <div class="metrics">
          {#each overviewCards as [key, label, description] (key)}
            <article class:attention={key === 'open_reports' && (overview[key] ?? 0) > 0}>
              <div class="metric-heading">
                <span>{label}</span>{#if key === 'open_reports'}<Icon
                    name="shield"
                    size={18}
                  />{/if}
              </div>
              <strong>{(overview[key] ?? 0).toLocaleString()}</strong>
              <p>{description}</p>
            </article>
          {/each}
        </div>
        <section class="panel access-panel">
          <div class="panel-icon"><Icon name="key" size={24} /></div>
          <div>
            <h3>Administrative access is scoped and auditable</h3>
            <p>
              Your browser uses your normal Kaede session. Owner grants remain CLI-managed, while
              delegated roles can be reviewed and revoked here. The server's static admin token is
              never sent to this page.
            </p>
          </div>
        </section>
      {:else if view === 'users'}
        <section class="panel">
          <form
            class="toolbar search-toolbar"
            onsubmit={(event) => {
              event.preventDefault();
              void searchUsers();
            }}
          >
            <label class="search-field">
              <Icon name="search" size={19} />
              <input
                bind:value={userQuery}
                aria-label="Search local usernames"
                placeholder="Search local usernames"
              />
            </label>
            <button type="submit" class="primary-button" disabled={loading.users}>Search</button>
          </form>
          {#if users.length === 0}
            <div class="empty-state">
              <Icon name="users" size={28} />
              <h3>No local users found</h3>
              <p>Try a different username, or clear the search to list recent accounts.</p>
            </div>
          {:else}
            <div class="data-list">
              {#each users as user (`${user.id}@${user.origin_domain}`)}
                <article class="data-row">
                  <div class="row-avatar">
                    {(user.display_name ?? user.username)[0].toUpperCase()}
                  </div>
                  <div class="row-main">
                    <strong>{user.display_name ?? user.username}</strong>
                    <small
                      >@{user.username} · {user.id}@{user.origin_domain} · {user.account_type ??
                        'human'}</small
                    >
                  </div>
                  <span class:danger-badge={userIsSuspended(user)} class="badge"
                    >{user.disabled_at
                      ? 'Banned'
                      : userIsSuspended(user)
                        ? `Suspended until ${new Date(user.suspended_until!).toLocaleString()}`
                        : 'Active'}</span
                  >
                  {#if can('users.manage') && user.account_type !== 'bot'}
                    <button
                      type="button"
                      class:danger-button={!userIsSuspended(user)}
                      class:secondary-button={userIsSuspended(user)}
                      disabled={busyAction === `user:${user.id}@${user.origin_domain}`}
                      onclick={() => void patchUser(user)}
                      >{userIsSuspended(user) ? 'Restore access' : 'Ban permanently'}</button
                    >
                  {/if}
                </article>
              {/each}
            </div>
          {/if}
        </section>
      {:else if view === 'applications'}
        <section class="panel">
          <div class="panel-intro">
            <Icon name="sparkles" size={22} />
            <p>
              Suspending an application immediately suspends active installations and advances its
              credential revocation generation.
            </p>
          </div>
          {#if apps.length === 0}
            <div class="empty-state">
              <Icon name="sparkles" size={28} />
              <h3>No bot applications</h3>
              <p>
                Applications registered on this instance or learned through federation will appear
                here.
              </p>
            </div>
          {:else}
            <div class="data-list">
              {#each apps as app (app.ref)}
                <article class="data-row">
                  <div class="row-avatar app-avatar"><Icon name="sparkles" size={20} /></div>
                  <div class="row-main">
                    <strong>{app.name}</strong><small
                      >{app.ref} · team {app.team_ref} · updated {new Date(
                        app.updated_at
                      ).toLocaleString()}</small
                    >
                  </div>
                  <span class:danger-badge={app.status === 'suspended'} class="badge"
                    >{app.status}</span
                  >
                  {#if can('bots.manage')}
                    <button
                      type="button"
                      class:danger-button={app.status !== 'suspended'}
                      class:secondary-button={app.status === 'suspended'}
                      disabled={busyAction === `app:${app.ref}`}
                      onclick={() => void patchApp(app)}
                      >{app.status === 'suspended' ? 'Activate' : 'Suspend'}</button
                    >
                  {/if}
                </article>
              {/each}
            </div>
          {/if}
        </section>
      {:else if view === 'reports'}
        <div class="report-controls">
          <div class="segmented" aria-label="Report filter">
            <button
              type="button"
              class:active={reportFilter === 'open'}
              onclick={() => (reportFilter = 'open')}>Open <span>{openReportCount}</span></button
            >
            <button
              type="button"
              class:active={reportFilter === 'closed'}
              onclick={() => (reportFilter = 'closed')}>Closed</button
            >
            <button
              type="button"
              class:active={reportFilter === 'all'}
              onclick={() => (reportFilter = 'all')}>All</button
            >
          </div>
          <p>
            Encrypted-message evidence is shown only when the reporter explicitly disclosed
            decrypted text. Keys are never included.
          </p>
        </div>
        {#if visibleReports.length === 0}
          <section class="empty-state panel">
            <Icon name="shield" size={30} />
            <h3>No {reportFilter} reports</h3>
            <p>
              {reportFilter === 'open'
                ? 'There are no safety cases waiting for review.'
                : 'Reports matching this filter will appear here.'}
            </p>
          </section>
        {:else}
          <div class="report-list">
            {#each visibleReports as report (report.id)}
              <article class="report-card" class:automated={report.source === 'photodna'}>
                <header>
                  <div class="report-title">
                    <span class:danger-badge={report.source === 'photodna'} class="badge"
                      >{report.source === 'photodna'
                        ? 'High-priority child-safety match'
                        : report.category.replaceAll('_', ' ')}</span
                    >
                    {#if report.severity}<span class="badge danger-badge">{report.severity}</span
                      >{/if}
                    <h3>Report #{report.id}</h3>
                  </div>
                  <time datetime={report.created_at}
                    >{new Date(report.created_at).toLocaleString()}</time
                  >
                </header>
                <div class="report-grid">
                  <dl>
                    <div>
                      <dt>Target</dt>
                      <dd>{report.target_type} · {report.target_ref}</dd>
                    </div>
                    <div>
                      <dt>Reporter</dt>
                      <dd>
                        {report.reporter_ref ??
                          (report.source === 'photodna' ? 'PhotoDNA scanner' : 'Unknown')}
                      </dd>
                    </div>
                    <div>
                      <dt>Evidence mode</dt>
                      <dd>{report.encryption_mode?.replaceAll('_', ' ') ?? 'metadata only'}</dd>
                    </div>
                    {#if report.assigned_admin_ref}<div>
                        <dt>Assigned</dt>
                        <dd>{report.assigned_admin_ref}</dd>
                      </div>{/if}
                  </dl>
                  <div class="report-body">
                    <p>{report.description ?? 'No reporter note was provided.'}</p>
                    {#if (report.target_type === 'attachment' || reportAttachments(report).length) && report.source !== 'photodna'}
                      <div class="safety-note attachment-evidence-note">
                        <Icon name="image" size={19} />
                        <span>
                          This report covers the complete message and its attachments. Verified
                          attachment metadata and, when an attachment was highlighted, a restricted
                          preview appear below.
                        </span>
                      </div>
                      {#if reportAttachments(report).length}
                        {#each reportAttachments(report) as attachment (attachment.attachment_ref)}
                          <dl class="match-metadata">
                            {#each [['Attachment', attachment.attachment_ref], ['Uploader', attachment.uploader_ref], ['Filename', attachment.filename], ['Content type', attachment.content_type], ['Size', attachment.size], ['Encryption', attachment.encryption_mode]] as [label, value] (label)}
                              {#if typeof value === 'string' || typeof value === 'number'}
                                <div>
                                  <dt>{label}</dt>
                                  <dd>
                                    {typeof value === 'number' ? value.toLocaleString() : value}
                                  </dd>
                                </div>
                              {/if}
                            {/each}
                          </dl>
                        {/each}
                      {/if}
                      {#if reportPreviewAttachmentRef(report)}
                        {#if canPreviewReportAttachment(report)}
                          {@const contentType = attachmentContentType(report) as string}
                          <div class="report-attachment-preview">
                            {#if contentType.startsWith('image/')}
                              <img
                                use:authenticatedMedia={{
                                  path: reportAttachmentPath(report, 'thumbnail_512'),
                                  contentType
                                }}
                                alt="Reported attachment preview"
                              />
                            {:else}
                              <video
                                use:authenticatedMedia={{
                                  path: reportAttachmentPath(report, 'original'),
                                  contentType
                                }}
                                controls
                                preload="metadata"
                              >
                                <track kind="captions" />
                              </video>
                            {/if}
                            <small
                              >Restricted {report.encryption_mode === 'e2ee_user_disclosed'
                                ? 'reporter-disclosed plaintext evidence'
                                : 'preview'} · successful access is recorded in the audit log.</small
                            >
                          </div>
                        {:else if report.encryption_mode === 'e2ee_user_disclosed'}
                          <small class="disclosure-note">
                            {report.evidence.disclosed_attachment_scan_status === 'quarantined'
                              ? 'The disclosed copy was quarantined after a safety match and cannot be rendered.'
                              : report.evidence.disclosed_attachment_scan_status === 'infected' ||
                                  report.evidence.disclosed_attachment_scan_status === 'rejected'
                                ? 'The disclosed copy failed safety validation and cannot be rendered.'
                                : report.evidence.disclosed_attachment_scan_status === 'failed'
                                  ? 'The disclosed copy could not be processed. Follow the media-processing incident procedure.'
                                  : report.evidence.disclosed_attachment_scan_status === 'clean'
                                    ? 'The disclosed evidence type is not previewable inline.'
                                    : 'The disclosed evidence is being scanned. Reload this queue shortly.'}
                          </small>
                        {:else if report.encryption_mode !== 'e2ee_metadata'}
                          <small class="disclosure-note">
                            Preview unavailable here. Remote attachments must be reviewed by their
                            home instance.
                          </small>
                        {/if}
                        <dl class="match-metadata">
                          {#each [['Attachment', report.evidence.attachment_ref], ['Uploader', report.evidence.uploader_ref], ['Stored filename', report.evidence.filename], ['Stored content type', report.evidence.content_type], ['Stored size', report.evidence.size], ['Encryption', report.evidence.attachment_encryption_mode], ['Disclosed evidence', report.evidence.disclosed_attachment_ref], ['Disclosed filename', report.evidence.disclosed_filename], ['Disclosed content type', report.evidence.disclosed_content_type], ['Disclosed size', report.evidence.disclosed_size], ['Disclosed scan', report.evidence.disclosed_attachment_scan_status]] as [label, value] (label)}
                            {#if typeof value === 'string' || typeof value === 'number'}
                              <div>
                                <dt>{label}</dt>
                                <dd>
                                  {typeof value === 'number' ? value.toLocaleString() : value}
                                </dd>
                              </div>
                            {/if}
                          {/each}
                        </dl>
                        {#if report.encryption_mode === 'e2ee_metadata'}
                          <small class="disclosure-note">
                            Encrypted attachment metadata only; no decrypted file, filename, or key
                            was disclosed.
                          </small>
                        {:else if report.encryption_mode === 'e2ee_user_disclosed'}
                          <small class="disclosure-note">
                            The reporter explicitly decrypted and uploaded this attachment. The
                            plaintext evidence is reporter-supplied; the server can scan the
                            uploaded copy but cannot prove it matches the original ciphertext.
                          </small>
                        {/if}
                        {#if typeof report.evidence.photodna === 'object' && report.evidence.photodna !== null}
                          <div class="safety-note">
                            <Icon name="shield" size={19} />
                            <span>
                              The disclosed evidence produced a critical PhotoDNA safety match. Its
                              bytes were quarantined and are not renderable from this queue.
                            </span>
                          </div>
                        {/if}
                      {/if}
                    {/if}
                    {#if typeof report.evidence.content === 'string'}
                      <blockquote>
                        <small
                          >Message by {report.evidence.author_ref ?? 'unknown'} in {report.evidence
                            .channel_ref ?? 'unknown'}</small
                        >
                        <p>
                          {report.evidence.content ||
                            '(No message text; this was an attachment-only encrypted message.)'}
                        </p>
                        {#if report.evidence.disclosure}<small class="disclosure-note"
                            >Reporter-disclosed E2EE evidence; the server could not verify the
                            plaintext.</small
                          >{/if}
                      </blockquote>
                    {:else if report.source === 'photodna'}
                      <div class="safety-note">
                        <Icon name="image" size={19} /><span
                          >Matched-image content is not rendered in the queue. Review the restricted
                          match metadata and follow your incident procedure.</span
                        >
                      </div>
                      <dl class="match-metadata">
                        {#if photoDnaEvidence(report, 'attachment_ref')}<div>
                            <dt>Attachment</dt>
                            <dd>{photoDnaEvidence(report, 'attachment_ref')}</dd>
                          </div>{/if}
                        {#if photoDnaEvidence(report, 'uploader_ref')}<div>
                            <dt>Uploader</dt>
                            <dd>{photoDnaEvidence(report, 'uploader_ref')}</dd>
                          </div>{/if}
                        {#if photoDnaEvidence(report, 'detected_content_type')}<div>
                            <dt>Detected type</dt>
                            <dd>{photoDnaEvidence(report, 'detected_content_type')}</dd>
                          </div>{/if}
                        {#if photoDnaEvidence(report, 'provider_tracking_id')}<div>
                            <dt>Provider tracking ID</dt>
                            <dd>{photoDnaEvidence(report, 'provider_tracking_id')}</dd>
                          </div>{/if}
                        {#each photoDnaFlags(report) as flag, index (`${flag.source}:${flag.match_id ?? index}`)}
                          <div>
                            <dt>Match source</dt>
                            <dd>
                              {flag.source}{flag.violations.length
                                ? ` · ${flag.violations.join(', ')}`
                                : ''}{flag.match_distance !== null
                                ? ` · distance ${flag.match_distance}`
                                : ''}{flag.match_id ? ` · match ${flag.match_id}` : ''}
                            </dd>
                          </div>
                        {/each}
                      </dl>
                    {/if}
                  </div>
                </div>
                {#if can('reports.manage')}
                  {@const subjectRef = reportSubjectRef(report)}
                  {@const localSubjectRef = localReportSubject(report)}
                  {#if subjectRef}
                    <section class="enforcement-panel" aria-label="Report enforcement">
                      <div class="enforcement-heading">
                        <div class="panel-icon danger-icon"><Icon name="shield" size={20} /></div>
                        <div>
                          <h4>Enforce against {subjectRef}</h4>
                          <p>
                            {localSubjectRef
                              ? 'Suspend creation access or ban login, and remove messages in rooms this instance controls.'
                              : 'Suspend this remote user across locally hosted guilds, or ban and remove them from those guilds.'}
                            Every action is written to the audit log.
                          </p>
                        </div>
                      </div>
                      <div class="enforcement-fields">
                        <label>
                          <span>Account action</span>
                          <select
                            bind:value={enforcementDrafts[report.id].account_action}
                            disabled={!can('users.manage')}
                          >
                            {#each accountActionOptions as option (option.value)}
                              <option value={option.value}>{option.label}</option>
                            {/each}
                          </select>
                        </label>
                        <label>
                          <span>Message action</span>
                          <select bind:value={enforcementDrafts[report.id].message_action}>
                            {#each messageActionOptions as option (option.value)}
                              <option
                                value={option.value}
                                disabled={option.value === 'delete_reported' && !report.message_ref}
                                >{option.label}</option
                              >
                            {/each}
                          </select>
                        </label>
                        <label class="enforcement-reason">
                          <span>Enforcement reason</span>
                          <textarea
                            bind:value={enforcementDrafts[report.id].reason}
                            rows="2"
                            maxlength="500"
                            placeholder="Required; visible in the administrative audit trail"
                          ></textarea>
                        </label>
                        <button
                          type="button"
                          class="danger-button"
                          disabled={busyAction === `enforce:${report.id}` ||
                            !hasEnforcementAction(report)}
                          onclick={() => void enforceReport(report)}
                          >{busyAction === `enforce:${report.id}`
                            ? 'Applying…'
                            : 'Apply punishment'}</button
                        >
                      </div>
                      <small>
                        “All messages” means all active messages stored in locally authoritative
                        rooms. A remote ban also prevents the user from joining any guild hosted
                        here.
                      </small>
                    </section>
                  {:else}
                    <div class="enforcement-unavailable">
                      <Icon name="globe" size={18} />
                      <span>
                        This report does not identify a user account that can receive an account or
                        message-history punishment.
                      </span>
                    </div>
                  {/if}
                {/if}
                <footer class="case-actions">
                  {#if can('reports.manage')}
                    <label
                      ><span>Status</span><select bind:value={reportDrafts[report.id].status}
                        >{#each reportStatuses as status (status)}<option value={status}
                            >{status.replaceAll('_', ' ')}</option
                          >{/each}</select
                      ></label
                    >
                    <label class="resolution-field"
                      ><span>Internal resolution note</span><textarea
                        bind:value={reportDrafts[report.id].resolution}
                        rows="2"
                        maxlength="2000"
                        placeholder="Record what was reviewed and any action taken"
                      ></textarea></label
                    >
                    <button
                      type="button"
                      class="primary-button"
                      disabled={busyAction === `report:${report.id}` || !reportChanged(report)}
                      onclick={() => void patchReport(report)}
                      >{busyAction === `report:${report.id}` ? 'Saving…' : 'Save case'}</button
                    >
                  {:else}
                    <span class="badge">{report.status.replaceAll('_', ' ')}</span>
                  {/if}
                </footer>
              </article>
            {/each}
          </div>
        {/if}
      {:else if view === 'instances'}
        <section class="panel">
          <div class="panel-intro">
            <Icon name="globe" size={22} />
            <p>
              <strong>Silence</strong> stops inbound delivery and reports. <strong>Suspend</strong> also
              disables shared replicas and outbound contact. Exact-domain rules are safest.
            </p>
          </div>
          {#if can('instances.manage')}
            <form
              class="policy-form"
              onsubmit={(event) => {
                event.preventDefault();
                void addBlock();
              }}
            >
              <label
                ><span>Instance domain</span><input
                  bind:value={blockDomain}
                  required
                  placeholder="instance.example"
                /></label
              >
              <label
                ><span>Policy</span><select bind:value={blockLevel}
                  ><option value="silence">Silence</option><option value="suspend">Suspend</option
                  ></select
                ></label
              >
              <label class="reason-field"
                ><span>Reason <small>(optional)</small></span><input
                  bind:value={blockReason}
                  maxlength="500"
                  placeholder="Internal reason for this policy"
                /></label
              >
              <label class="checkbox-field"
                ><input type="checkbox" bind:checked={blockSubdomains} /><span
                  >Include subdomains</span
                ></label
              >
              <button type="submit" class="primary-button" disabled={busyAction === 'block:add'}
                >Apply policy</button
              >
            </form>
          {/if}
          {#if blocks.length === 0}
            <div class="empty-state">
              <Icon name="globe" size={28} />
              <h3>No federation restrictions</h3>
              <p>This instance currently accepts federation from every known peer.</p>
            </div>
          {:else}
            <div class="data-list">
              {#each blocks as block (block.domain)}
                <article class="data-row">
                  <div class="row-avatar"><Icon name="globe" size={20} /></div>
                  <div class="row-main">
                    <strong>{block.domain}</strong><small
                      >{block.reason ?? 'No internal reason'}{block.include_subdomains
                        ? ' · includes subdomains'
                        : ' · exact domain only'}</small
                    >
                  </div>
                  <span class="badge danger-badge">{block.level}</span>
                  {#if can('instances.manage')}<button
                      type="button"
                      class="secondary-button"
                      disabled={busyAction === `block:${block.domain}`}
                      onclick={() => void removeBlock(block.domain)}>Remove</button
                    >{/if}
                </article>
              {/each}
            </div>
          {/if}
        </section>
      {:else if view === 'operators'}
        <section class="panel">
          <div class="panel-intro">
            <Icon name="key" size={22} />
            <p>
              Owner access is CLI-only. Owners may delegate fixed operational roles to local human
              accounts; every change is written to the audit log.
            </p>
          </div>
          {#if me.roles.includes('owner')}
            <form
              class="operator-form"
              onsubmit={(event) => {
                event.preventDefault();
                void addOperator();
              }}
            >
              <label
                ><span>Local user reference</span><input
                  bind:value={operatorRef}
                  required
                  placeholder="123456789@this.instance"
                /></label
              >
              <label
                ><span>Role</span><select bind:value={operatorRole}
                  >{#each roleOptions as role (role)}<option value={role}
                      >{role.replaceAll('_', ' ')}</option
                    >{/each}</select
                ></label
              >
              <button type="submit" class="primary-button" disabled={busyAction === 'operator:add'}
                >Grant role</button
              >
            </form>
          {/if}
          {#if operators.length === 0}
            <div class="empty-state">
              <Icon name="key" size={28} />
              <h3>No active operator grants</h3>
              <p>Grant a scoped role to a local user, or use the CLI to create an owner.</p>
            </div>
          {:else}
            <div class="data-list">
              {#each operators as operator (operator.id)}
                <article class="data-row">
                  <div class="row-avatar">
                    {(operator.user.display_name ?? operator.user.username)[0].toUpperCase()}
                  </div>
                  <div class="row-main">
                    <strong>{operator.user.display_name ?? operator.user.username}</strong><small
                      >@{operator.user.username} · {operator.user.id}@{operator.user.origin_domain} ·
                      granted {new Date(operator.created_at).toLocaleDateString()}</small
                    >
                  </div>
                  <span class="badge">{operator.role.replaceAll('_', ' ')}</span>
                  {#if me.roles.includes('owner') && operator.role !== 'owner'}<button
                      type="button"
                      class="secondary-button"
                      disabled={busyAction === `operator:${operator.id}`}
                      onclick={() => void removeOperator(operator)}>Revoke</button
                    >{/if}
                </article>
              {/each}
            </div>
          {/if}
        </section>
      {:else if view === 'audit'}
        <section class="panel">
          {#if audits.length === 0}
            <div class="empty-state">
              <Icon name="clock" size={28} />
              <h3>No audit events</h3>
              <p>
                Administrative changes will appear here with their actor, target, and safe metadata.
              </p>
            </div>
          {:else}
            <div class="audit-list">
              {#each audits as event (event.id)}
                <article>
                  <div class="audit-marker"><Icon name="clock" size={18} /></div>
                  <div class="row-main">
                    <strong>{event.action.replaceAll('.', ' › ')}</strong><small
                      >{event.actor_ref ?? event.actor_kind} · {new Date(
                        event.created_at
                      ).toLocaleString()}</small
                    >
                    <p>{event.target_type}: <code>{event.target_ref}</code></p>
                    {#if Object.keys(event.metadata).length > 0}<details>
                        <summary>Safe metadata</summary>
                        <pre>{JSON.stringify(event.metadata, null, 2)}</pre>
                      </details>{/if}
                  </div>
                </article>
              {/each}
            </div>
          {/if}
        </section>
      {/if}
    </div>
  </section>
</main>

<style>
  :global(body) {
    overflow: auto;
  }

  .admin-shell {
    min-height: 100dvh;
    display: grid;
    grid-template-columns: minmax(250px, 286px) minmax(0, 1fr);
    color: var(--text);
    background: var(--app-bg);
  }

  .sidebar {
    position: sticky;
    top: 0;
    z-index: 2;
    height: 100dvh;
    display: flex;
    flex-direction: column;
    border-right: 1px solid var(--line);
    padding: 1.4rem 1rem 1rem;
    background: var(--sidebar);
  }

  .back-link,
  .identity,
  nav button,
  .panel-intro,
  .state-card,
  .access-panel,
  .metric-heading,
  .header-actions,
  .search-field,
  .checkbox-field,
  .safety-note {
    display: flex;
    align-items: center;
  }

  .attachment-evidence-note {
    color: var(--text-soft);
    background: var(--surface-subtle);
  }

  .report-attachment-preview {
    display: grid;
    gap: 0.45rem;
    width: min(100%, 680px);
    margin-top: 0.75rem;
  }

  .report-attachment-preview img,
  .report-attachment-preview video {
    width: 100%;
    max-height: 420px;
    display: block;
    border: 1px solid var(--line-soft);
    border-radius: 10px;
    background: #08090d;
    object-fit: contain;
  }

  .report-attachment-preview small {
    color: var(--text-muted);
  }

  .back-link {
    gap: 0.55rem;
    width: fit-content;
    color: var(--text-muted);
    text-decoration: none;
  }

  .back-link:hover {
    color: var(--text);
  }

  .instance-label {
    margin: 2.25rem 0 1.1rem;
  }

  .instance-label span,
  .eyebrow {
    color: var(--accent-text);
    font-size: 0.72rem;
    font-weight: 850;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .instance-label h1 {
    margin-top: 0.25rem;
    font-size: clamp(1.65rem, 2.2vw, 2.15rem);
    overflow-wrap: normal;
  }

  .identity {
    gap: 0.7rem;
    border: 1px solid var(--line-soft);
    border-radius: var(--radius-md);
    padding: 0.7rem;
    background: var(--surface-subtle);
  }

  .identity-avatar,
  .row-avatar {
    width: 2.35rem;
    height: 2.35rem;
    flex: 0 0 auto;
    display: grid;
    place-items: center;
    border-radius: 10px;
    color: var(--on-pine);
    background: var(--pine);
    font-weight: 850;
  }

  .identity > div:last-child {
    min-width: 0;
    display: grid;
  }

  .identity small,
  .row-main small {
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }

  nav {
    display: grid;
    gap: 0.25rem;
    margin-top: 1.25rem;
  }

  nav button {
    width: 100%;
    gap: 0.7rem;
    border: 0;
    border-radius: 10px;
    padding: 0.72rem 0.8rem;
    color: var(--text-muted);
    background: transparent;
    font: inherit;
    font-weight: 700;
    text-align: left;
    cursor: pointer;
  }

  nav button:hover {
    color: var(--text);
    background: var(--surface-hover);
  }

  nav button.active {
    color: var(--accent-text);
    background: var(--accent-soft);
  }

  nav button > span {
    flex: 1;
  }

  .nav-badge {
    min-width: 1.4rem;
    border-radius: 999px;
    padding: 0.1rem 0.35rem;
    color: var(--on-danger);
    background: var(--danger);
    text-align: center;
  }

  .sidebar-footer {
    display: flex;
    align-items: flex-start;
    gap: 0.55rem;
    margin-top: auto;
    padding: 0.8rem;
    color: var(--text-muted);
    font-size: 0.72rem;
  }

  .status-dot {
    width: 0.55rem;
    height: 0.55rem;
    flex: 0 0 auto;
    border-radius: 50%;
    margin-top: 0.25rem;
    background: var(--success, #3fb979);
  }

  .content {
    min-width: 0;
    padding: clamp(1.25rem, 3vw, 3rem);
  }

  .content-inner {
    width: min(100%, 1440px);
    margin-inline: auto;
  }

  .page-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1.5rem;
    margin-bottom: 1.5rem;
  }

  .page-header h2 {
    margin: 0.25rem 0 0.15rem;
    font-size: clamp(2rem, 4vw, 3.25rem);
    overflow-wrap: normal;
  }

  .page-header p,
  .panel p,
  .metric-heading + strong + p,
  .report-controls p,
  .empty-state p,
  .state-card p {
    color: var(--text-muted);
  }

  .header-actions {
    gap: 0.45rem;
  }

  .icon-button {
    width: 2.55rem;
    height: 2.55rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 0;
    color: var(--text-muted);
    background: var(--surface);
    font: inherit;
    font-size: 1.45rem;
    line-height: 1;
    text-decoration: none;
    cursor: pointer;
  }

  .icon-button:hover:not(:disabled) {
    color: var(--text);
    background: var(--surface-hover);
  }

  .metrics {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 0.85rem;
  }

  .metrics article,
  .panel,
  .report-card,
  .state-card,
  .feedback,
  .report-controls {
    border: 1px solid var(--line);
    border-radius: var(--radius-lg);
    background: var(--surface);
    box-shadow: var(--shadow-sm);
  }

  .metrics article {
    min-height: 150px;
    padding: 1.15rem;
  }

  .metrics article.attention {
    border-color: color-mix(in srgb, var(--danger) 55%, var(--line));
    background: color-mix(in srgb, var(--danger-soft) 32%, var(--surface));
  }

  .metric-heading {
    justify-content: space-between;
    gap: 0.7rem;
    color: var(--text-soft);
    font-weight: 750;
  }

  .metrics strong {
    display: block;
    margin: 0.45rem 0 0.15rem;
    font-family: var(--font-display);
    font-size: 2.25rem;
    line-height: 1;
  }

  .metrics p {
    font-size: 0.78rem;
  }

  .panel,
  .report-controls {
    margin-top: 1rem;
    padding: clamp(1rem, 2vw, 1.35rem);
  }

  .access-panel,
  .panel-intro,
  .state-card {
    gap: 0.9rem;
  }

  .panel-icon {
    width: 2.8rem;
    height: 2.8rem;
    flex: 0 0 auto;
    display: grid;
    place-items: center;
    border-radius: 12px;
    color: var(--accent-text);
    background: var(--accent-soft);
  }

  .access-panel h3,
  .state-card h3,
  .empty-state h3 {
    margin: 0 0 0.2rem;
  }

  .panel-intro {
    align-items: flex-start;
    border-bottom: 1px solid var(--line-soft);
    padding-bottom: 1rem;
  }

  .panel-intro > :global(svg) {
    flex: 0 0 auto;
    margin-top: 0.1rem;
    color: var(--accent-text);
  }

  .feedback {
    margin-bottom: 1rem;
    padding: 0.85rem 1rem;
  }

  .feedback.error,
  .error-state {
    border-color: color-mix(in srgb, var(--danger) 55%, var(--line));
    color: var(--danger);
    background: color-mix(in srgb, var(--danger-soft) 30%, var(--surface));
  }

  .feedback.success {
    border-color: color-mix(in srgb, var(--pine) 55%, var(--line));
    color: var(--pine);
  }

  .state-card {
    min-height: 110px;
    padding: 1.2rem;
  }

  .state-card > div {
    flex: 1;
  }

  .spinner {
    width: 1.5rem;
    height: 1.5rem;
    flex: 0 0 auto;
    border: 3px solid var(--line);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  button,
  input,
  textarea,
  select {
    font: inherit;
  }

  input,
  textarea,
  select {
    width: 100%;
    min-width: 0;
    border: 1px solid var(--control-border, var(--line));
    border-radius: 9px;
    padding: 0.68rem 0.75rem;
    color: var(--text);
    background: var(--control-surface, var(--surface-raised));
  }

  textarea {
    resize: vertical;
  }

  button:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }

  .primary-button,
  .secondary-button,
  .danger-button {
    border: 1px solid transparent;
    border-radius: 9px;
    padding: 0.68rem 0.9rem;
    font-weight: 800;
    white-space: nowrap;
    cursor: pointer;
  }

  .primary-button {
    color: var(--on-accent);
    background: var(--accent);
  }

  .primary-button:hover:not(:disabled) {
    background: var(--accent-hover);
  }

  .secondary-button {
    border-color: var(--line);
    color: var(--text);
    background: var(--surface-raised);
  }

  .secondary-button:hover:not(:disabled) {
    background: var(--surface-hover);
  }

  .danger-button {
    color: var(--on-danger);
    background: var(--danger);
  }

  .toolbar,
  .policy-form,
  .operator-form,
  .case-actions {
    display: grid;
    gap: 0.75rem;
  }

  .search-toolbar {
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .search-field {
    gap: 0.6rem;
    border: 1px solid var(--control-border, var(--line));
    border-radius: 9px;
    padding-left: 0.75rem;
    color: var(--text-muted);
    background: var(--control-surface, var(--surface-raised));
  }

  .search-field input {
    border: 0;
    padding-left: 0;
    background: transparent;
  }

  .data-list {
    display: grid;
    margin-top: 1rem;
  }

  .data-row {
    display: flex;
    align-items: center;
    gap: 0.8rem;
    border-top: 1px solid var(--line-soft);
    padding: 0.85rem 0;
  }

  .data-row:first-child {
    border-top: 0;
  }

  .row-main {
    min-width: 0;
    flex: 1;
    display: grid;
    gap: 0.12rem;
  }

  .app-avatar {
    color: var(--on-purple);
    background: var(--purple);
  }

  .badge {
    display: inline-flex;
    align-items: center;
    width: fit-content;
    border-radius: 999px;
    padding: 0.23rem 0.55rem;
    color: var(--pine);
    background: var(--pine-soft);
    font-size: 0.72rem;
    font-weight: 800;
    line-height: 1.3;
    text-transform: capitalize;
    white-space: nowrap;
  }

  .danger-badge {
    color: var(--danger);
    background: var(--danger-soft);
  }

  .empty-state {
    display: grid;
    place-items: center;
    min-height: 240px;
    padding: 2rem;
    color: var(--text-muted);
    text-align: center;
  }

  .empty-state h3 {
    color: var(--text);
  }

  .report-controls {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }

  .report-controls > p {
    max-width: 660px;
    font-size: 0.82rem;
    text-align: right;
  }

  .segmented {
    display: inline-flex;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 0.2rem;
    background: var(--surface-subtle);
  }

  .segmented button {
    border: 0;
    border-radius: 7px;
    padding: 0.45rem 0.7rem;
    color: var(--text-muted);
    background: transparent;
    font-weight: 750;
    cursor: pointer;
  }

  .segmented button.active {
    color: var(--text);
    background: var(--surface-raised);
    box-shadow: var(--shadow-sm);
  }

  .segmented button span {
    margin-left: 0.25rem;
    color: var(--danger);
  }

  .report-list {
    display: grid;
    gap: 1rem;
    margin-top: 1rem;
  }

  .report-card {
    overflow: hidden;
  }

  .report-card.automated {
    border-color: color-mix(in srgb, var(--danger) 55%, var(--line));
  }

  .report-card > header,
  .case-actions {
    padding: 1rem 1.15rem;
    background: var(--surface-subtle);
  }

  .report-card > header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--line-soft);
  }

  .report-title {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  .report-title h3 {
    width: 100%;
    margin-top: 0.2rem;
  }

  .report-card time,
  .report-card dt,
  .report-card small {
    color: var(--text-muted);
    font-size: 0.75rem;
  }

  .report-grid {
    display: grid;
    grid-template-columns: minmax(220px, 0.65fr) minmax(0, 1.35fr);
    gap: 1.25rem;
    padding: 1.15rem;
  }

  .report-card dl {
    display: grid;
    align-content: start;
    gap: 0.7rem;
    margin: 0;
  }

  .report-card dl > div {
    display: grid;
    gap: 0.12rem;
  }

  .report-card dt {
    font-weight: 800;
    text-transform: uppercase;
  }

  .report-card dd {
    margin: 0;
    overflow-wrap: anywhere;
  }

  .report-body > p {
    color: var(--text-soft);
  }

  blockquote,
  .safety-note {
    margin: 0.8rem 0 0;
    border: 1px solid var(--line-soft);
    border-radius: 10px;
    padding: 0.85rem;
    background: var(--surface-subtle);
  }

  blockquote p {
    margin-top: 0.45rem;
    white-space: pre-wrap;
  }

  .disclosure-note {
    display: block;
    margin-top: 0.65rem;
    color: var(--warning) !important;
  }

  .enforcement-panel {
    display: grid;
    gap: 0.9rem;
    margin: 0 1.15rem 1.15rem;
    border: 1px solid color-mix(in srgb, var(--danger) 42%, var(--line));
    border-radius: 12px;
    padding: 1rem;
    background: color-mix(in srgb, var(--danger-soft) 55%, var(--surface-raised));
  }

  .enforcement-heading {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
  }

  .enforcement-heading h4,
  .enforcement-heading p {
    margin: 0;
  }

  .enforcement-heading p,
  .enforcement-panel > small {
    margin-top: 0.2rem;
    color: var(--text-muted);
    font-size: 0.78rem;
  }

  .danger-icon {
    flex: 0 0 auto;
    color: var(--danger);
    background: var(--danger-soft);
  }

  .enforcement-fields {
    display: grid;
    grid-template-columns: minmax(170px, 0.55fr) minmax(190px, 0.7fr) minmax(250px, 1fr) auto;
    align-items: end;
    gap: 0.75rem;
  }

  .enforcement-fields label {
    min-width: 0;
    display: grid;
    gap: 0.35rem;
    color: var(--text-soft);
    font-size: 0.78rem;
    font-weight: 750;
  }

  .enforcement-fields textarea {
    min-height: 2.75rem;
    resize: vertical;
  }

  .enforcement-unavailable {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    margin: 0 1.15rem 1.15rem;
    border: 1px solid var(--line-soft);
    border-radius: 10px;
    padding: 0.75rem 0.85rem;
    color: var(--text-muted);
    background: var(--surface-subtle);
    font-size: 0.82rem;
  }

  .safety-note {
    align-items: flex-start;
    gap: 0.65rem;
    color: var(--danger);
    background: var(--danger-soft);
  }

  .match-metadata {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    margin-top: 0.75rem !important;
    border: 1px solid var(--line-soft);
    border-radius: 10px;
    padding: 0.85rem;
    background: var(--surface-subtle);
  }

  .case-actions {
    grid-template-columns: minmax(150px, 0.35fr) minmax(260px, 1fr) auto;
    align-items: end;
    border-top: 1px solid var(--line-soft);
  }

  .case-actions label,
  .policy-form label,
  .operator-form label {
    min-width: 0;
    display: grid;
    gap: 0.35rem;
    color: var(--text-soft);
    font-size: 0.76rem;
    font-weight: 800;
  }

  .policy-form {
    grid-template-columns: 1fr 180px 1.25fr auto;
    align-items: end;
    margin-top: 1rem;
    border-bottom: 1px solid var(--line-soft);
    padding-bottom: 1rem;
  }

  .policy-form .checkbox-field {
    grid-column: 1 / -2;
  }

  .checkbox-field {
    grid-template-columns: auto 1fr !important;
    justify-self: start;
    gap: 0.5rem !important;
  }

  .checkbox-field input {
    width: 1rem;
    height: 1rem;
  }

  .operator-form {
    grid-template-columns: minmax(0, 1fr) minmax(180px, 0.4fr) auto;
    align-items: end;
    margin-top: 1rem;
    border-bottom: 1px solid var(--line-soft);
    padding-bottom: 1rem;
  }

  .audit-list {
    display: grid;
  }

  .audit-list article {
    display: flex;
    align-items: flex-start;
    gap: 0.8rem;
    border-top: 1px solid var(--line-soft);
    padding: 0.9rem 0;
  }

  .audit-list article:first-child {
    border-top: 0;
  }

  .audit-marker {
    width: 2.2rem;
    height: 2.2rem;
    flex: 0 0 auto;
    display: grid;
    place-items: center;
    border-radius: 50%;
    color: var(--purple);
    background: var(--purple-soft);
  }

  .audit-list p {
    margin-top: 0.35rem;
  }

  code,
  pre {
    font-family: var(--font-mono);
    overflow-wrap: anywhere;
  }

  details {
    margin-top: 0.45rem;
  }

  summary {
    width: fit-content;
    color: var(--text-muted);
    cursor: pointer;
  }

  pre {
    max-height: 260px;
    overflow: auto;
    border-radius: 8px;
    padding: 0.7rem;
    background: var(--surface-subtle);
    font-size: 0.72rem;
    white-space: pre-wrap;
  }

  @media (max-width: 1080px) {
    .metrics {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .policy-form {
      grid-template-columns: 1fr 180px;
    }

    .reason-field {
      grid-column: 1 / -1;
    }

    .policy-form .checkbox-field {
      grid-column: auto;
    }

    .report-grid {
      grid-template-columns: 1fr;
    }

    .enforcement-fields {
      grid-template-columns: 1fr 1fr;
    }

    .enforcement-reason {
      grid-column: 1 / -1;
    }

    .enforcement-fields button {
      grid-column: 1 / -1;
      justify-self: end;
    }

    .case-actions {
      grid-template-columns: 1fr 2fr;
    }

    .case-actions button {
      grid-column: 1 / -1;
      justify-self: end;
    }
  }

  @media (max-width: 760px) {
    .admin-shell {
      display: block;
    }

    .sidebar {
      position: sticky;
      height: auto;
      max-height: none;
      border-right: 0;
      border-bottom: 1px solid var(--line);
      padding: 0.8rem;
    }

    .instance-label,
    .identity,
    .sidebar-footer {
      display: none;
    }

    nav {
      display: flex;
      overflow-x: auto;
      margin-top: 0.7rem;
      padding-bottom: 0.15rem;
      scrollbar-width: thin;
    }

    nav button {
      width: auto;
      flex: 0 0 auto;
      padding: 0.55rem 0.7rem;
    }

    nav button > span {
      flex: none;
    }

    .content {
      padding: 1rem;
    }

    .page-header h2 {
      font-size: 2rem;
    }

    .metrics {
      grid-template-columns: 1fr 1fr;
    }

    .metrics article {
      min-height: 135px;
    }

    .data-row {
      align-items: flex-start;
      flex-wrap: wrap;
    }

    .data-row .row-main {
      flex-basis: calc(100% - 3.2rem);
    }

    .report-controls {
      align-items: flex-start;
      flex-direction: column;
    }

    .report-controls > p {
      text-align: left;
    }

    .report-card > header {
      flex-direction: column;
    }

    .case-actions,
    .enforcement-fields,
    .operator-form,
    .policy-form {
      grid-template-columns: 1fr;
    }

    .reason-field,
    .enforcement-reason,
    .policy-form .checkbox-field,
    .case-actions button {
      grid-column: auto;
    }

    .case-actions button,
    .enforcement-fields button,
    .operator-form button,
    .policy-form button {
      width: 100%;
      justify-self: stretch;
    }

    .enforcement-panel,
    .enforcement-unavailable {
      margin-inline: 0.75rem;
    }
  }

  @media (max-width: 480px) {
    .metrics {
      grid-template-columns: 1fr;
    }

    .search-toolbar {
      grid-template-columns: 1fr;
    }

    .access-panel {
      align-items: flex-start;
    }

    .segmented {
      width: 100%;
    }

    .segmented button {
      flex: 1;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .spinner {
      animation: none;
    }
  }
</style>
