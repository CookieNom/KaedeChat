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
        Kaede is the open source software that powers the Instance. These Terms govern our operation
        of that software, not the software itself, which is distributed under its own licence.
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
        <li>You may create one account per person. One person, one account.</li>
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
        licence to store, display, and transmit your content on the Instance and to federate it to
        other Kaede instances where that is permitted. You also agree that the content you share is
        something you have the right to share.
      </p>
      <p>
        You are responsible for your own content. We may remove content that violates these Terms,
        and we may act without prior notice when the law or safety requires it.
      </p>
    </section>

    <section>
      <h2>5. What we store, and what we do not</h2>
      <p>
        We store the content of your messages and media so the service can work. We do not sell your
        data, and we do not run advertising on the Instance. For the full picture of what we collect
        and why, see our <a href={resolve('/privacy')}>privacy policy</a>.
      </p>
    </section>

    <section>
      <h2>6. Moderation</h2>
      <p>
        Moderators on the Instance may move, edit, delete, or report content and members in line
        with these Terms and any channel- or community-specific rules. Where we suspend or remove an
        account, we aim to explain why when it is safe to do so.
      </p>
    </section>

    <section>
      <h2>7. Service and changes</h2>
      <p>
        The Instance is provided “as is” and “as available.” We work to keep it reliable, but we do
        not guarantee it will be uninterrupted or error-free. We may change, suspend, or discontinue
        parts of the service, and we may update these Terms. If a change is material, we will give
        notice in the Instance before it takes effect.
      </p>
    </section>

    <section>
      <h2>8. Termination</h2>
      <p>
        To request account closure, email
        <a href={`mailto:${operatorLegalConfig.contactEmail}`}>{operatorLegalConfig.contactEmail}</a
        >. We may suspend or terminate an account that breaches these Terms. Sections that by their
        nature should survive termination — including Sections 4, 5, and 11 — remain in effect after
        an account ends.
      </p>
    </section>

    <section>
      <h2>9. Intellectual property</h2>
      <p>
        The Instance, its branding, and content we own are ours. Kaede is open source software and
        is not affected by this section. You may not use our name or marks in a way that suggests we
        endorse you without our permission.
      </p>
    </section>

    <section>
      <h2>10. Disclaimers and limitation of liability</h2>
      <p>
        To the extent allowed by law, we are not liable for indirect, incidental, or consequential
        damages, or for any loss of data, that result from your use of the Instance. Our total
        liability to you for any claim is limited to the greatest amount the law allows. Nothing in
        these Terms limits liability that cannot be limited by law, including for death, personal
        injury, or fraud.
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
        >. We read the messages and we reply.
      </p>
    </section>
  </LegalPage>
{:else}
  <UnconfiguredLegalPage title="Terms of Service" policy="terms of service" />
{/if}
