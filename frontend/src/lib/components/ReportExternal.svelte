<script lang="ts">
  import { t } from '$lib/ui/locale';
  import { api, userErrorMessage } from '$lib/api/client';

  interface SafetyReport {
    id: string;
    target_ref: string;
    target_type?: string;
    message_ref?: string | null;
    evidence: Record<string, unknown>;
  }
  let { report, canManage }: { report: SafetyReport; canManage: boolean } = $props();
  let destination = $state('');
  let reference = $state('');
  let submittedAt = $state('');
  let notes = $state('');
  let busy = $state(false);
  let feedback = $state('');
  let submissions = $state<unknown[] | null>(null);
  const fields = $derived([
    [$t('safety_reporting_348fd43781'), 'origin_domain'],
    [$t('safety_reporting_e552d3f167'), 'source_url'],
    [$t('safety_reporting_12c92d03e8'), 'observed_at'],
    [$t('safety_reporting_ce35b8ddaa'), 'content_sha256'],
    [$t('safety_reporting_aa880ddd97'), 'remote_variant'],
    [$t('safety_reporting_4e160c3973'), 'size_bytes'],
    [$t('ui_conversation_ccca1817'), 'conversation_ref'],
    [$t('safety_reporting_5e25eed2bd'), 'identity_source'],
    [$t('safety_reporting_2bee927665'), 'bytes_retained'],
    [$t('safety_reporting_3ec531cb1f'), 'photodna_hash_retained']
  ]);

  async function download() {
    busy = true;
    feedback = '';
    try {
      const evidence = await api<unknown>(`/administration/reports/${report.id}/export`);
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(evidence, null, 2)], { type: 'application/json' })
      );
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `kaede-safety-report-${report.id}.json`;
      try {
        anchor.click();
      } finally {
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
      feedback = $t('safety_reporting_d52f379813');
    } catch (error) {
      feedback = userErrorMessage(error, $t('safety_reporting_c49c41ea93'));
    } finally {
      busy = false;
    }
  }

  async function recordSubmission(event: SubmitEvent) {
    event.preventDefault();
    busy = true;
    feedback = '';
    try {
      const updated = await api<SafetyReport>(
        `/administration/reports/${report.id}/external-submissions`,
        {
          method: 'POST',
          body: JSON.stringify({
            destination,
            reference,
            submitted_at: new Date(submittedAt).toISOString(),
            notes: notes || null
          })
        }
      );
      submissions = updated.evidence.external_submissions as unknown[];
      destination = reference = submittedAt = notes = '';
      feedback = $t('safety_reporting_15b8699545');
    } catch (error) {
      feedback = userErrorMessage(error, $t('safety_reporting_d46684800e'));
    } finally {
      busy = false;
    }
  }
</script>

<section aria-label={$t('safety_reporting_d0234b0432')}>
  <h4>{$t('safety_reporting_d0234b0432')}</h4>
  <p>{$t('safety_reporting_86126d1a33')}</p>
  <dl>
    <div>
      <dt>{$t('safety_reporting_cb2c0c6a4e')}</dt>
      <dd>
        {(typeof report.evidence.attachment_ref === 'string'
          ? report.evidence.attachment_ref.split('@')[1]
          : report.target_type === 'attachment'
            ? report.target_ref.split('@')[1]
            : null) ?? $t('safety_reporting_ca18449697')}
      </dd>
    </div>
    <div>
      <dt>{$t('safety_reporting_715acc3fb2')}</dt>
      <dd>{report.message_ref ?? $t('safety_reporting_ca18449697')}</dd>
    </div>
    {#each fields as [label, key] (key)}
      <div>
        <dt>{label}</dt>
        <dd>
          {report.evidence[key] == null
            ? $t('safety_reporting_ca18449697')
            : String(report.evidence[key])}
        </dd>
      </div>
    {/each}
  </dl>
  <p>{$t('safety_reporting_88a34a41af')}</p>
  <details>
    <summary>{$t('safety_reporting_f6ffbdc030')}</summary>
    <pre>{JSON.stringify(report.evidence, null, 2)}</pre>
  </details>
  {#if canManage}
    <button type="button" disabled={busy} onclick={download}
      >{$t('safety_reporting_9c6f5d4c96')}</button
    >
    <form onsubmit={recordSubmission}>
      <h5>{$t('safety_reporting_908aedef63')}</h5>
      <label
        >{$t('safety_reporting_b8800e73ff')}<input
          bind:value={destination}
          required
          maxlength="300"
        /></label
      >
      <label
        >{$t('safety_reporting_27a7b422f2')}<input
          bind:value={reference}
          required
          maxlength="300"
        /></label
      >
      <label
        >{$t('safety_reporting_b0fca05e31')}<input
          type="datetime-local"
          bind:value={submittedAt}
          required
        /></label
      >
      <label
        >{$t('safety_reporting_8d8d16e164')}<textarea bind:value={notes} maxlength="2000"
        ></textarea></label
      >
      <button disabled={busy}>{$t('safety_reporting_1ca641b99f')}</button>
    </form>
  {/if}
  <div role="status">{feedback}</div>
  <details>
    <summary>{$t('safety_reporting_aa4a1bdd7e')}</summary>
    <pre>{JSON.stringify(submissions ?? report.evidence.external_submissions ?? [], null, 2)}</pre>
  </details>
</section>

<style>
  section {
    padding: 1rem;
    border-top: 1px solid var(--border);
  }
  dl div {
    display: grid;
    grid-template-columns: minmax(10rem, 1fr) 2fr;
    gap: 1rem;
    margin: 0.5rem 0;
  }
  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 24rem;
    overflow: auto;
  }
  form,
  label {
    display: grid;
    gap: 0.5rem;
  }
  form {
    margin-top: 1rem;
  }
  input,
  textarea {
    width: 100%;
    box-sizing: border-box;
  }
  button {
    margin-top: 0.75rem;
  }
</style>
