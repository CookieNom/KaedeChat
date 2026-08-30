<script lang="ts">
  import { resolve } from '$app/paths';
  import { operatorLegalConfig } from '$lib/branding/landing';
  import LegalPage from '$lib/components/landing/LegalPage.svelte';
  import UnconfiguredLegalPage from '$lib/components/landing/UnconfiguredLegalPage.svelte';
</script>

{#if operatorLegalConfig}
  <LegalPage title="Privacy Policy" updated={operatorLegalConfig.effectiveDate}>
    <section>
      <h2>1. Short version</h2>
      <p>
        We run {operatorLegalConfig.instanceName} (the “Instance”), a chat service built with Kaede. Kaede
        stores account data, the content you provide, and technical metadata. Federation and optional
        features can send some of that data to other instance operators and service providers. This page
        explains those data flows and the controls Kaede provides today.
      </p>
    </section>

    <section>
      <h2>2. Who we are</h2>
      <p>
        {operatorLegalConfig.operatorName} (“we”, “us”, or “our”) operates the Instance. You can reach
        us at
        <a href={`mailto:${operatorLegalConfig.contactEmail}`}>{operatorLegalConfig.contactEmail}</a
        >. This policy applies to this Instance, not every server or service with which it connects.
      </p>
    </section>

    <section>
      <h2>3. What we collect</h2>
      <ul>
        <li>
          <strong>Account details.</strong> Your username or handle, your email address (if you provide
          one), profile, settings, password hash, sessions, and device or encryption-key records. We do
          not store your password in plain text.
        </li>
        <li>
          <strong>Content you share.</strong> Messages, media, files, and reactions you post, stored so
          the service works. In a supported end-to-end encrypted room, participating clients encrypt supported
          message content and media before upload. Account, room, membership, delivery, moderation, and
          other service metadata remain visible to the service.
        </li>
        <li>
          <strong>Service and operational data.</strong> Guild and conversation membership, contacts,
          permissions, calls, reports, moderation actions, notifications, searches, federation delivery,
          IP-derived security data, device and client information, and server logs. Exact logs depend
          on how the Instance is operated.
        </li>
      </ul>
    </section>

    <section>
      <h2>4. How we use it</h2>
      <ul>
        <li>To provide the chat service and keep it working.</li>
        <li>To secure the Instance and investigate abuse, spam, or harm.</li>
        <li>
          To enforce the <a href={resolve('/terms')}>terms of service</a> and community rules.
        </li>
        <li>To respond to support and moderation requests.</li>
      </ul>
    </section>

    <section>
      <h2>5. Federation and other instances</h2>
      <p>
        Kaede federates. When you interact with members on other instances, your messages and
        profile, membership, reactions, media, and related metadata may be shared with and stored by
        those other instances. Their data practices are set by their operators, not by us. Deleting
        something here sends supported deletion events but cannot guarantee that every remote copy,
        quotation, download, notification, or backup is removed.
      </p>
    </section>

    <section>
      <h2>6. Service providers and external requests</h2>
      <p>
        The Instance may use separate providers for hosting, object storage, search, email, voice
        and video, push delivery, abuse prevention, malware or image matching, and GIF search.
        Features such as link previews and embedded media can also contact the destination or embed
        provider. Those services receive the data technically needed for the request, which may
        include an IP address, account or device reference, email address, uploaded file, search
        text, or delivery metadata. Which optional services are enabled can change with Instance
        configuration.
      </p>
    </section>

    <section>
      <h2>7. Retention</h2>
      <p>
        Account records and content normally remain until they are deleted, removed by moderation,
        or affected by an operator-configured retention rule. Security, audit, federation, backup,
        and remote-instance copies can follow different schedules. Contact us for the current
        deployment’s retention details.
      </p>
    </section>

    <section>
      <h2>8. Your choices</h2>
      <ul>
        <li>You may update supported profile and account settings.</li>
        <li>
          You may delete supported messages and account media, subject to permissions and federation
          limits.
        </li>
        <li>
          You may export a protected recovery bundle for restoring your end-to-end encryption state.
        </li>
        <li>
          To make a privacy request or ask us to disable your account, email
          <a href={`mailto:${operatorLegalConfig.contactEmail}`}
            >{operatorLegalConfig.contactEmail}</a
          >.
        </li>
        <li>You may turn off available notification and privacy options in settings.</li>
      </ul>
    </section>

    <section>
      <h2>9. Security</h2>
      <p>
        Kaede supports encrypted network connections, password hashing, access controls, and
        optional end-to-end encryption for supported rooms. Actual security also depends on this
        deployment, its providers, other participants’ devices, and federated instances. No system
        is perfectly secure, so protect your credentials and encryption recovery material.
      </p>
    </section>

    <section>
      <h2>10. Browser storage and external content</h2>
      <p>
        The web app uses HTTP-only authentication cookies to keep you signed in. It also uses local
        storage and IndexedDB for preferences, navigation state, notification state, drafts, cached
        application data, and end-to-end encryption state or keys. Optional anti-abuse challenges,
        GIF search, and embedded external media can connect your browser or the Instance to their
        providers. Kaede’s web client does not include a general analytics or advertising SDK.
      </p>
    </section>

    <section>
      <h2>11. Children</h2>
      <p>
        The Instance is not directed at children under {operatorLegalConfig.minimumAge}. If you
        believe a child has provided personal data contrary to this policy, contact us.
      </p>
    </section>

    <section>
      <h2>12. Changes to this policy</h2>
      <p>
        We may update this policy as the service or its configuration changes. The date at the top
        of this page identifies the published version. Check this page for changes.
      </p>
    </section>

    <section>
      <h2>13. Contact</h2>
      <p>
        For privacy questions, requests, or to exercise your rights, email
        <a href={`mailto:${operatorLegalConfig.contactEmail}`}>{operatorLegalConfig.contactEmail}</a
        >. What rights apply depends on your location and applicable law.
      </p>
    </section>
  </LegalPage>
{:else}
  <UnconfiguredLegalPage title="Privacy Policy" policy="privacy policy" />
{/if}
