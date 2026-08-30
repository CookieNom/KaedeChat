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
        We run {operatorLegalConfig.instanceName} (the “Instance”), a chat service built on the open source
        Kaede software. We collect the minimum we need to run and secure a chat service. We do not sell
        your data, and we do not run advertising. This page explains what we collect, why, and the choices
        you have.
      </p>
    </section>

    <section>
      <h2>2. Who we are</h2>
      <p>
        {operatorLegalConfig.operatorName} (“we”, “us”, or “our”) operates the Instance. You can reach
        us at
        <a href={`mailto:${operatorLegalConfig.contactEmail}`}>{operatorLegalConfig.contactEmail}</a
        >. If you have a question about this policy, send it there and a person will answer.
      </p>
    </section>

    <section>
      <h2>3. What we collect</h2>
      <ul>
        <li>
          <strong>Account details.</strong> Your username or handle, your email address (if you provide
          one), and a securely derived key for your password. We do not store your password in plain text.
        </li>
        <li>
          <strong>Content you share.</strong> Messages, media, files, and reactions you post, stored so
          the service works. Where you enable an end-to-end encrypted room, the content is encrypted and
          we cannot read it.
        </li>
        <li>
          <strong>Operational data.</strong> Information we need to run and protect the service, such
          as connection details, device and client information, and basic logs used for security and moderation.
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
      <p>We do not use your data to build advertising profiles, and we do not sell it to anyone.</p>
    </section>

    <section>
      <h2>5. Federation and other instances</h2>
      <p>
        Kaede federates. When you interact with members on other instances, your messages and
        profile may be shared with, and stored by, those other instances. Their data practices are
        set by the operators of those instances, not by us. Before you post sensitive information in
        a federated context, consider who on the other side will be able to see and keep it.
      </p>
    </section>

    <section>
      <h2>6. Retention</h2>
      <p>
        We keep account and content data for as long as your account is active and as needed to run
        and secure the service. When you ask us to close your account, we delete or anonymise your
        personal data, except where we are required to keep some records for legal or security
        reasons.
      </p>
    </section>

    <section>
      <h2>7. Your choices</h2>
      <ul>
        <li>You may update your account details from your settings at any time.</li>
        <li>You may request a copy of the personal data we hold about you.</li>
        <li>
          To request account closure and deletion of your data, email
          <a href={`mailto:${operatorLegalConfig.contactEmail}`}
            >{operatorLegalConfig.contactEmail}</a
          >.
        </li>
        <li>You may opt out of optional features such as email notifications where available.</li>
      </ul>
    </section>

    <section>
      <h2>8. Security</h2>
      <p>
        We use encryption in transit, password hashing, and end-to-end encryption for supported
        rooms, along with ordinary operational safeguards. No system is perfectly secure, so please
        protect your credentials and use a strong, unique password.
      </p>
    </section>

    <section>
      <h2>9. Cookies and local storage</h2>
      <p>
        The Instance uses your browser’s local storage to keep you signed in and to remember your
        preferences. We do not use third-party advertising trackers.
      </p>
    </section>

    <section>
      <h2>10. Children</h2>
      <p>
        The Instance is not directed at children under {operatorLegalConfig.minimumAge}. If you
        believe a child has provided us personal data without consent, contact us and we will delete
        it.
      </p>
    </section>

    <section>
      <h2>11. Changes to this policy</h2>
      <p>
        We may update this policy as the service changes. If we make a material change, we will
        notify you in the Instance before it takes effect. The date at the top of this page reflects
        the latest revision.
      </p>
    </section>

    <section>
      <h2>12. Contact</h2>
      <p>
        For privacy questions, requests, or to exercise your rights, email
        <a href={`mailto:${operatorLegalConfig.contactEmail}`}>{operatorLegalConfig.contactEmail}</a
        >. We aim to respond promptly.
      </p>
    </section>
  </LegalPage>
{:else}
  <UnconfiguredLegalPage title="Privacy Policy" policy="privacy policy" />
{/if}
