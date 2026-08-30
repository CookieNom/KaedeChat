<script lang="ts">
  import { resolve } from '$app/paths';
  import { operatorLegalConfig } from '$lib/branding/landing';
  import LegalPage from '$lib/components/landing/LegalPage.svelte';
  import UnconfiguredLegalPage from '$lib/components/landing/UnconfiguredLegalPage.svelte';
</script>

{#if operatorLegalConfig}
  <LegalPage title="Terms of Service" updated={operatorLegalConfig.effectiveDate}>
    <section>
      <h2>1. About these terms</h2>
      <p>
        These terms of service (the “Terms”) apply when you use {operatorLegalConfig.instanceName}
        (the “Instance”), the chat service provided by {operatorLegalConfig.operatorName} (“we”, “us”,
        or “our”). By creating an account or using the Instance, you agree to these Terms. If you do not
        agree, please do not use the service.
      </p>
      <p>
        Kaede is the software that powers the Instance. These Terms govern your use of this
        Instance, not your use of the Kaede source code or third-party components under their
        separate terms.
      </p>
    </section>

    <section>
      <h2>2. Accounts</h2>
      <ul>
        <li>
          You are responsible for the accuracy of the information you provide and for your account.
        </li>
        <li>
          You must be at least {operatorLegalConfig.minimumAge} years old to create an account.
        </li>
        <li>
          Keep your credentials private. We cannot verify every message, so never share your
          password or codes.
        </li>
        <li>
          This Instance secures your account and provides the domain used in your federated handle.
        </li>
      </ul>
    </section>

    <section>
      <h2>3. Acceptable use</h2>
      <p>When you use the Instance, you agree not to:</p>
      <ul>
        <li>Break the law, or ask someone else to break the law.</li>
        <li>Harass, threaten, or abuse other members. See our moderation notes in Section 6.</li>
        <li>Upload or share malware, phishing, or other harmful content.</li>
        <li>Share someone else’s private information without their consent.</li>
        <li>Send unsolicited bulk messages or spam.</li>
        <li>Interfere with the Instance, including attempting to overload or disrupt it.</li>
        <li>Impersonate another person or misrepresent your affiliation with us.</li>
      </ul>
    </section>

    <section>
      <h2>4. Your content</h2>
      <p>
        You keep the rights to what you post. By posting, you give us a limited, non-exclusive
        licence to store, process, display, and transmit your content as needed to operate the
        Instance. This includes sending content and related account information to other Kaede
        instances when you use federated communities or conversations. You also agree that the
        content you share is something you have the right to share.
      </p>
      <p>
        You are responsible for your own content. We may remove content that violates these Terms,
        and we may act without prior notice when the law or safety requires it.
      </p>
    </section>

    <section>
      <h2>5. Storage, encryption, and third parties</h2>
      <p>
        The Instance stores account information, messages, media, and service metadata so Kaede can
        work. Supported rooms may use end-to-end encryption, but account, membership, delivery, and
        moderation metadata are not end-to-end encrypted. Optional features may use infrastructure
        or integration providers. See our <a href={resolve('/privacy')}>privacy policy</a> for the data
        flows and choices that apply.
      </p>
      <p>
        If you use end-to-end encryption, keep any recovery material safe. We cannot guarantee that
        encrypted content or keys can be recovered after you lose your devices or credentials.
      </p>
    </section>

    <section>
      <h2>6. Moderation</h2>
      <p>
        Moderators may delete content, remove members, restrict accounts, and act on reports in line
        with these Terms and any community-specific rules. Moderation of a federated interaction may
        also involve the operator of another instance. We may act without advance notice.
      </p>
    </section>

    <section>
      <h2>7. Service and changes</h2>
      <p>
        The Instance is provided “as is” and “as available.” We work to keep it reliable, but we do
        not guarantee it will be uninterrupted or error-free. We may change, suspend, or discontinue
        parts of the service, and we may update these Terms. The effective date at the top
        identifies the published version; check it for changes.
      </p>
    </section>

    <section>
      <h2>8. Termination</h2>
      <p>
        To ask us to disable your account or discuss a data request, email
        <a href={`mailto:${operatorLegalConfig.contactEmail}`}>{operatorLegalConfig.contactEmail}</a
        >. A request does not remove copies already delivered to another instance, content retained
        by other members, or records we must keep. We may suspend or disable an account that
        breaches these Terms. Provisions that by their nature continue after access ends remain in
        effect.
      </p>
    </section>

    <section>
      <h2>9. Intellectual property</h2>
      <p>
        The Instance’s branding and content we own remain ours. Kaede and third-party components are
        governed by their separate terms. You may not use our name or marks in a way that suggests
        we endorse you without our permission.
      </p>
    </section>

    <section>
      <h2>10. Disclaimers and limitation of liability</h2>
      <p>
        To the maximum extent permitted by law, we disclaim implied warranties and are not liable
        for indirect, incidental, special, consequential, or punitive damages, or for lost data
        arising from use of the Instance. Nothing in these Terms excludes or limits liability that
        applicable law does not allow us to exclude or limit.
      </p>
    </section>

    <section>
      <h2>11. Governing law</h2>
      <p>
        These Terms are governed by the laws of {operatorLegalConfig.jurisdiction}. Any dispute will
        be handled in the courts of {operatorLegalConfig.jurisdiction}, unless another forum is
        required by law.
      </p>
    </section>

    <section>
      <h2>12. Contact</h2>
      <p>
        Questions about these Terms? Reach us at
        <a href={`mailto:${operatorLegalConfig.contactEmail}`}>{operatorLegalConfig.contactEmail}</a
        >.
      </p>
    </section>
  </LegalPage>
{:else}
  <UnconfiguredLegalPage title="Terms of Service" policy="terms of service" />
{/if}
