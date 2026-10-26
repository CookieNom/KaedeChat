<script lang="ts">
  import { onMount } from 'svelte';
  import { api, userErrorMessage } from '$lib/api/client';
  import { hasAdminCapability } from './capabilities';
  import { downloadAuthenticatedMedia } from '$lib/media/authenticated';

  interface SafetyCase {
    id: string;
    report_id: string;
    status: 'pending' | 'confirmed' | 'dismissed';
    removal_completed_at?: string | null;
    removal_pending?: boolean;
    source: string;
    reason: string;
    created_at: string;
    message_ref: string | null;
    attachment_refs: string[];
    subject: Record<string, unknown> | null;
    evidence: {
      id: string;
      filename: string;
      content_type: string;
      size: number;
      sha256: string;
      preserved_at: string | null;
    }[];
    submissions: {
      reference: string;
      submitted_at: string;
      notes?: string;
      evidence_ids?: string[];
    }[];
    holds: { id: string; reason: string; reference?: string; released_at: string | null }[];
    retain_until: string | null;
    closed_at: string | null;
    can_delete_evidence: boolean;
    can_suspend?: boolean;
    provenance_verified?: boolean;
  }

  let { capabilities }: { capabilities: string[] } = $props();
  const canManage = $derived(hasAdminCapability(capabilities, 'safety.manage'));
  const canAccessEvidence = $derived(hasAdminCapability(capabilities, 'safety.evidence'));
  let cases = $state<SafetyCase[]>([]);
  let selected = $state<SafetyCase | null>(null);
  let loading = $state(true);
  let busy = $state('');
  let error = $state('');
  let notice = $state('');
  let filter = $state('open');
  let reason = $state('');
  let reference = $state('');
  let submissionTime = $state('');
  let submissionNotes = $state('');
  let evidenceIds = $state<string[]>([]);
  let retainUntil = $state('');
  let holdReason = $state('');
  let holdReference = $state('');
  let deleteConfirmed = $state(false);
  let suspendAccount = $state(false);
  let sourceVerified = $state(false);
  const requiresSourceVerification = $derived(
    Boolean(selected?.message_ref) && !selected?.provenance_verified
  );
  const canSuspend = $derived(
    Boolean(selected?.can_suspend) && hasAdminCapability(capabilities, 'users.manage')
  );
  const visibleCases = $derived(cases.filter((item) => filter === 'all' || !item.closed_at));
  const activeHolds = $derived(selected?.holds.filter((hold) => !hold.released_at) ?? []);
  const validReason = $derived(reason.trim().length >= 3);
  const validRetention = $derived(
    Boolean(retainUntil && new Date(retainUntil).getTime() > Date.now())
  );

  async function load() {
    if (busy) return;
    loading = true;
    error = '';
    try {
      const response = await api<{ cases: SafetyCase[] }>('/administration/child-safety');
      cases = response.cases;
      if (selected) selected = cases.find((item) => item.id === selected?.id) ?? null;
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not load child safety cases.');
    } finally {
      loading = false;
    }
  }

  function choose(item: SafetyCase) {
    if (busy) return;
    selected = item;
    reason =
      reference =
      submissionTime =
      submissionNotes =
      retainUntil =
      holdReason =
      holdReference =
        '';
    evidenceIds = [];
    deleteConfirmed = false;
    suspendAccount = false;
    sourceVerified = false;
    error = notice = '';
  }

  async function action(
    path: string,
    body: Record<string, unknown>,
    success: string,
    method = 'POST'
  ) {
    if (!selected || busy || !canManage) return;
    busy = path;
    error = notice = '';
    try {
      await api(`/administration/child-safety/${encodeURIComponent(selected.id)}/${path}`, {
        method,
        body: JSON.stringify(body)
      });
      const updated = await api<SafetyCase>(
        `/administration/child-safety/${encodeURIComponent(selected.id)}`
      );
      cases = cases.map((item) => (item.id === updated.id ? updated : item));
      selected = updated;
      notice = success;
      deleteConfirmed = false;
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not update this case. Reload and try again.');
    } finally {
      busy = '';
    }
  }

  async function evidenceDownload(item: SafetyCase['evidence'][number]) {
    if (!selected || busy || !canAccessEvidence) return;
    busy = `evidence:${item.id}`;
    error = '';
    try {
      await downloadAuthenticatedMedia(
        {
          path: `/api/v1/administration/child-safety/${encodeURIComponent(selected.id)}/evidence/${encodeURIComponent(item.id)}`,
          contentType: item.content_type
        },
        item.filename
      );
      notice = 'Evidence download requested. Access is recorded in the audit log.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not retrieve this evidence.');
    } finally {
      busy = '';
    }
  }

  onMount(() => {
    void load();
  });
</script>

<section class="child-safety" aria-label="Child safety cases">
  <header class="queue-heading">
    <div>
      <h2>Child safety</h2>
      <p>Restricted reports and preserved evidence. Evidence is never previewed automatically.</p>
    </div>
    <button type="button" onclick={() => void load()} disabled={loading || Boolean(busy)}
      >{loading ? 'Loading…' : 'Refresh cases'}</button
    >
  </header>
  {#if error}<p class="feedback error" role="alert">{error}</p>{/if}
  {#if notice}<p class="feedback" role="status">{notice}</p>{/if}
  <label class="filter"
    >Show<select bind:value={filter} disabled={Boolean(busy)}
      ><option value="open">Open cases</option><option value="all">All cases</option></select
    ></label
  >
  <div class="queue-layout">
    <nav class="case-list" aria-label="Select a child safety case">
      {#each visibleCases as item (item.id)}
        <button
          type="button"
          class:chosen={selected?.id === item.id}
          aria-pressed={selected?.id === item.id}
          disabled={Boolean(busy)}
          onclick={() => choose(item)}
        >
          <strong>{item.source === 'photodna' ? 'PhotoDNA match' : 'User report'}</strong>
          <span
            >{item.status === 'pending'
              ? 'Awaiting review'
              : item.status === 'confirmed'
                ? item.removal_completed_at
                  ? 'Confirmed'
                  : 'Confirmed · Removal pending'
                : 'Dismissed'}{item.closed_at ? ' · Closed' : ''}</span
          >
          <small>{new Date(item.created_at).toLocaleString()}</small>
        </button>
      {:else}<p>{loading ? 'Loading cases…' : 'No cases in this view.'}</p>{/each}
    </nav>
    {#if selected}
      <article class="case-detail" aria-label="Selected child safety case">
        <header>
          <h3>{selected.source === 'photodna' ? 'PhotoDNA match' : 'Child safety report'}</h3>
          <span class="badge"
            >{selected.closed_at
              ? 'Closed'
              : selected.status === 'pending'
                ? 'Awaiting review'
                : selected.status}</span
          >
        </header>
        <dl>
          <div>
            <dt>Case</dt>
            <dd>{selected.id}</dd>
          </div>
          <div>
            <dt>Received</dt>
            <dd>{new Date(selected.created_at).toLocaleString()}</dd>
          </div>
          <div>
            <dt>Message</dt>
            <dd>{selected.message_ref ?? 'Not available'}</dd>
          </div>
          <div>
            <dt>Retention ends</dt>
            <dd>
              {selected.retain_until ? new Date(selected.retain_until).toLocaleString() : 'Not set'}
            </dd>
          </div>
        </dl>
        {#if selected.status === 'confirmed'}
          <p class="feedback" role="status">
            {selected.removal_completed_at
              ? 'Storage removal verified for this instance. Restricted evidence is preserved separately.'
              : 'Removal pending. New download links are blocked. Storage deletion is retried automatically; existing links may still work until deletion succeeds or they expire.'}
          </p>
          <p>
            Client cleanup requires a supported client to reconnect. Copies saved outside the app
            cannot be erased remotely.
          </p>
        {/if}
        {#if selected.reason}<p>{selected.reason}</p>{/if}
        {#if selected.status === 'pending'}<p class="feedback">
            Content remains unavailable until this case is reviewed.
          </p>{/if}
        {#if selected.subject}<details>
            <summary>Account information</summary>
            <pre>{JSON.stringify(selected.subject, null, 2)}</pre>
          </details>{/if}

        <section class="case-section">
          <h4>Preserved evidence</h4>
          {#each selected.evidence as item (item.id)}
            <div class="evidence-row">
              <div>
                <strong>{item.filename}</strong><small
                  >{item.content_type} · {Math.ceil(item.size / 1024)} KB</small
                >
                <details><summary>Evidence identifier</summary><code>{item.sha256}</code></details>
              </div>
              {#if canAccessEvidence}<button
                  type="button"
                  disabled={Boolean(busy)}
                  onclick={() => void evidenceDownload(item)}
                  >{busy === `evidence:${item.id}` ? 'Preparing…' : 'Download evidence'}</button
                >{/if}
            </div>
          {:else}<p>No preserved files are available.</p>{/each}
          {#if !canAccessEvidence && selected.evidence.length}<p class="help">
              Your role can read this case. Evidence access requires a separate permission.
            </p>{/if}
        </section>

        {#if canManage}
          <section class="case-section">
            <h4>Review decision</h4>
            <label
              >Reason<textarea
                bind:value={reason}
                maxlength="2000"
                placeholder="Record why this decision is appropriate"
                disabled={Boolean(busy)}></textarea></label
            >
            {#if selected.status === 'pending' && !selected.closed_at}
              {#if requiresSourceVerification}<label class="check"
                  ><input type="checkbox" bind:checked={sourceVerified} disabled={Boolean(busy)} />I
                  verified that the evidence belongs to the reported message.</label
                >{/if}
              {#if canSuspend}<label class="check"
                  ><input
                    type="checkbox"
                    bind:checked={suspendAccount}
                    disabled={Boolean(busy)}
                  />Suspend the account for 7 days when confirming</label
                >{/if}
              <div class="actions">
                <button
                  class="danger"
                  type="button"
                  disabled={Boolean(busy) ||
                    !validReason ||
                    (requiresSourceVerification && !sourceVerified)}
                  onclick={() =>
                    validReason &&
                    (!requiresSourceVerification || sourceVerified) &&
                    void action(
                      'review',
                      {
                        decision: 'confirmed',
                        reason: reason.trim(),
                        source_verified: sourceVerified,
                        ...(canSuspend && suspendAccount ? { suspend_account: true } : {})
                      },
                      'Confirmed. Removal has been queued for affected clients.'
                    )}>Confirm and remove content</button
                ><button
                  type="button"
                  disabled={Boolean(busy) || !validReason}
                  onclick={() =>
                    validReason &&
                    void action(
                      'review',
                      { decision: 'dismissed', reason: reason.trim() },
                      'Case dismissed.'
                    )}>Dismiss report</button
                >
              </div>
            {:else}<p class="help">This case has already been reviewed.</p>{/if}
          </section>
          <section class="case-section">
            <h4>External submission</h4>
            <p class="help">
              Record a receipt after submitting through the reporting destination. This form does
              not send evidence externally.
            </p>
            <form
              onsubmit={(event) => {
                event.preventDefault();
                if (reference.trim() && submissionTime)
                  void action(
                    'submissions',
                    {
                      reference: reference.trim(),
                      submitted_at: new Date(submissionTime).toISOString(),
                      notes: submissionNotes.trim() || null,
                      evidence_ids: evidenceIds
                    },
                    'Submission receipt recorded.'
                  );
              }}
            >
              <fieldset disabled={Boolean(busy) || Boolean(selected.closed_at)}>
                <label
                  >Submission reference<input
                    bind:value={reference}
                    required
                    maxlength="300"
                  /></label
                ><label
                  >Submitted at<input
                    type="datetime-local"
                    bind:value={submissionTime}
                    required
                  /></label
                ><label
                  >Notes<textarea bind:value={submissionNotes} maxlength="2000"></textarea></label
                >
                {#if selected.evidence.length}<span>Evidence included</span
                  >{#each selected.evidence as item (item.id)}<label class="check"
                      ><input
                        type="checkbox"
                        bind:group={evidenceIds}
                        value={item.id}
                      />{item.filename}</label
                    >{/each}{/if}
                <button type="submit" disabled={!reference.trim() || !submissionTime}
                  >Record submission</button
                >
              </fieldset>
            </form>
          </section>
          <section class="case-section">
            <h4>Retention and holds</h4>
            <form
              onsubmit={(event) => {
                event.preventDefault();
                if (validRetention && validReason)
                  void action(
                    'retention',
                    { retain_until: new Date(retainUntil).toISOString(), reason: reason.trim() },
                    'Retention updated.'
                  );
              }}
            >
              <fieldset disabled={Boolean(busy)}>
                <label
                  >Keep evidence until<input
                    type="datetime-local"
                    bind:value={retainUntil}
                    required
                  /></label
                ><button disabled={!validRetention || !validReason}>Set retention</button>
              </fieldset>
            </form>
            <p class="help">
              Enter the reason in Review decision before changing retention or releasing a hold.
            </p>
            {#each activeHolds as hold (hold.id)}<div class="hold">
                <p>
                  <strong>Active hold</strong> · {hold.reason}{hold.reference
                    ? ` · ${hold.reference}`
                    : ''}
                </p>
                <button
                  type="button"
                  disabled={Boolean(busy) || !validReason}
                  onclick={() =>
                    validReason &&
                    void action(
                      `holds/${encodeURIComponent(hold.id)}/release`,
                      { reason: reason.trim() },
                      'Hold released.'
                    )}>Release hold</button
                >
              </div>{/each}
            <form
              onsubmit={(event) => {
                event.preventDefault();
                if (holdReason.trim().length >= 3)
                  void action(
                    'holds',
                    { reason: holdReason.trim(), reference: holdReference.trim() || null },
                    'Evidence hold added.'
                  );
              }}
            >
              <fieldset disabled={Boolean(busy)}>
                <label
                  >New hold reason<input
                    bind:value={holdReason}
                    required
                    minlength="3"
                    maxlength="2000"
                  /></label
                ><label
                  >Hold reference (optional)<input
                    bind:value={holdReference}
                    maxlength="300"
                  /></label
                ><button disabled={holdReason.trim().length < 3}>Add evidence hold</button>
              </fieldset>
            </form>
          </section>
          <section class="case-section">
            <h4>Close and delete</h4>
            <button
              type="button"
              disabled={Boolean(busy) ||
                !validReason ||
                selected.status === 'pending' ||
                Boolean(selected.closed_at)}
              onclick={() =>
                validReason &&
                selected?.status !== 'pending' &&
                !selected?.closed_at &&
                void action('close', { reason: reason.trim() }, 'Case closed.')}
              >{selected.closed_at ? 'Case closed' : 'Close case'}</button
            >
            {#if canAccessEvidence && selected.can_delete_evidence && selected.evidence.length}
              <label class="check"
                ><input type="checkbox" bind:checked={deleteConfirmed} disabled={Boolean(busy)} />I
                confirm permanent deletion of the eligible preserved evidence.</label
              ><button
                type="button"
                class="danger"
                disabled={Boolean(busy) || !deleteConfirmed || !validReason}
                onclick={() =>
                  deleteConfirmed &&
                  validReason &&
                  selected?.can_delete_evidence &&
                  void action(
                    'evidence',
                    { reason: reason.trim() },
                    'Eligible evidence deleted.',
                    'DELETE'
                  )}>Delete preserved evidence</button
              >
            {:else}<p class="help">
                Evidence deletion is unavailable until storage removal, retention, holds, and case
                requirements permit it.
              </p>{/if}
          </section>
        {/if}
        {#if selected.submissions.length}<section class="case-section">
            <h4>Submission history</h4>
            {#each selected.submissions as submission, index (`${submission.reference}:${submission.submitted_at}:${index}`)}<p
              >
                <strong>{submission.reference}</strong> · {new Date(
                  submission.submitted_at
                ).toLocaleString()}
              </p>{/each}
          </section>{/if}
      </article>
    {:else}<p class="empty-detail">Select a case to review its details.</p>{/if}
  </div>
</section>

<style>
  .child-safety {
    display: grid;
    gap: 1.25rem;
  }
  .queue-heading,
  .case-detail > header,
  .evidence-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }
  h2,
  h3,
  h4,
  p {
    margin: 0;
  }
  h4 {
    margin-bottom: 0.75rem;
  }
  .queue-heading p,
  .help,
  small {
    color: var(--text-muted);
  }
  .queue-heading p {
    margin-top: 0.5rem;
  }
  .queue-layout {
    display: grid;
    grid-template-columns: minmax(13rem, 1fr) minmax(0, 3fr);
    gap: 1.25rem;
    align-items: start;
  }
  .case-list,
  .case-detail,
  form,
  fieldset,
  label {
    display: grid;
    gap: 0.75rem;
  }
  .case-list button {
    display: grid;
    gap: 0.4rem;
    text-align: left;
    width: 100%;
  }
  .case-list .chosen {
    border-color: var(--accent);
    background: var(--bg-hover);
  }
  .case-detail {
    padding: 1.25rem;
    border: 1px solid var(--border);
    border-radius: 0.75rem;
    min-width: 0;
    gap: 1rem;
  }
  .case-section {
    border-top: 1px solid var(--border);
    padding-top: 1rem;
    display: grid;
    gap: 0.75rem;
  }
  .filter {
    max-width: 15rem;
  }
  button {
    min-height: 2.75rem;
    padding: 0.65rem 0.9rem;
    border: 1px solid var(--border);
    border-radius: 0.5rem;
    background: var(--bg-secondary);
    color: var(--text-primary);
    cursor: pointer;
  }
  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .danger {
    border-color: var(--danger, #ba5454);
  }
  input,
  select,
  textarea {
    width: 100%;
    box-sizing: border-box;
    min-height: 2.75rem;
    padding: 0.65rem;
    color: var(--text-primary);
    background: var(--bg-primary);
    border: 1px solid var(--border);
    border-radius: 0.4rem;
  }
  textarea {
    min-height: 5rem;
    resize: vertical;
  }
  fieldset {
    border: 0;
    padding: 0;
    margin: 0;
    min-width: 0;
  }
  .check {
    display: flex;
    align-items: center;
    gap: 0.65rem;
  }
  .check input {
    width: 1.2rem;
    min-height: 1.2rem;
    flex-shrink: 0;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
  }
  dl {
    display: grid;
    gap: 0.6rem;
    margin: 0;
  }
  dl div {
    display: grid;
    grid-template-columns: 7rem minmax(0, 1fr);
    gap: 0.75rem;
  }
  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  pre,
  code {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .evidence-row {
    padding: 0.8rem 0;
  }
  .evidence-row > div {
    min-width: 0;
    display: grid;
    gap: 0.35rem;
    overflow-wrap: anywhere;
  }
  .feedback {
    padding: 0.9rem;
    background: var(--bg-secondary);
    border-radius: 0.5rem;
  }
  .error {
    border: 1px solid var(--danger, #ba5454);
  }
  .badge {
    border: 1px solid var(--border);
    border-radius: 2rem;
    padding: 0.3rem 0.65rem;
    text-transform: capitalize;
  }
  .hold {
    display: grid;
    gap: 0.5rem;
    padding: 0.8rem;
    border: 1px solid var(--border);
    border-radius: 0.5rem;
  }
  .empty-detail {
    padding: 1rem;
  }
  @media (max-width: 700px) {
    .queue-layout {
      grid-template-columns: 1fr;
    }
    .queue-heading,
    .evidence-row {
      align-items: stretch;
      flex-direction: column;
    }
    .case-detail {
      padding: 1rem;
    }
    .case-list {
      max-height: 18rem;
      overflow: auto;
    }
    dl div {
      grid-template-columns: 1fr;
      gap: 0.2rem;
    }
    .actions button {
      flex: 1;
    }
  }
</style>
