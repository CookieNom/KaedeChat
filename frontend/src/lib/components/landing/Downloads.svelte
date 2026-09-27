<script lang="ts">
  import { onMount } from 'svelte';
  import { resolve } from '$app/paths';
  import Icon from '$lib/components/Icon.svelte';
  import {
    loadDownloads,
    releasesUrl,
    type Downloads,
    type DownloadPlatform
  } from '$lib/branding/downloads';

  let downloads: Downloads = $state({});
  let loading = $state(true);
  let failed = $state(false);

  onMount(() => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    void loadDownloads(controller.signal).then((result) => {
      downloads = result.downloads;
      failed = result.failed;
      loading = false;
      clearTimeout(timeout);
    });
    return () => {
      clearTimeout(timeout);
      controller.abort();
    };
  });
</script>

{#snippet deviceIcon(mobile = false)}
  <svg
    width="26"
    height="26"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    {#if mobile}
      <rect x="6" y="2" width="12" height="20" rx="3" />
      <path d="M10 5h4m-3 14h2" />
    {:else}
      <rect x="2" y="3" width="20" height="14" rx="2" />
      <path d="M8 21h8m-4-4v4" />
    {/if}
  </svg>
{/snippet}

<!-- eslint-disable svelte/no-navigation-without-resolve -- Download and release links are validated external GitHub URLs. -->

{#snippet download(platform: DownloadPlatform, label: string, secondary = false)}
  {@const asset = downloads[platform]}
  <div class="download-option">
    {#if asset}
      <a class:secondary-button={secondary} class:primary-button={!secondary} href={asset.url}>
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.8"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"><path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" /></svg
        >
        {label}
      </a>
      <small class="download-meta">
        {asset.version} · {Math.max(1, Math.round(asset.size / 1024 / 1024))} MB
        {#if asset.checksum}<a href={asset.checksum} aria-label={`${label} SHA-256 checksum`}
            >SHA-256</a
          >{/if}
      </small>
    {:else}
      <button class="secondary-button" disabled>{label}</button>
      <small class="download-meta"
        >{loading
          ? 'Checking releases…'
          : failed
            ? 'Could not check availability'
            : 'Not yet available'}</small
      >
    {/if}
  </div>
{/snippet}

<section class="cl-downloads" id="downloads" aria-labelledby="downloads-title">
  <div class="download-heading">
    <span class="download-eyebrow">DOWNLOAD KAEDE</span>
    <h2 id="downloads-title">Your conversations.<br />On every screen.</h2>
    <p>
      Make yourself at home on desktop or take your communities with you. Choose the app for your
      device.
    </p>
  </div>
  <p class="download-status" role="status">
    {#if loading}Finding the latest downloads…
    {:else if failed}Some downloads could not be checked. <a href={releasesUrl}
        >Browse GitHub releases</a
      > to find your app.
    {:else}The latest available release for each platform. <a href={releasesUrl}
        >View release notes</a
      >
    {/if}
  </p>
  <div class="download-grid">
    <article>
      <span class="download-icon">{@render deviceIcon()}</span>
      <h3>Windows</h3>
      <p>
        A dedicated home for your communities, with chat, calls, and notifications on your desktop.
      </p>
      {@render download('windows', 'Download for Windows')}
      <p class="download-note">64-bit Windows</p>
    </article>
    <article>
      <span class="download-icon">{@render deviceIcon()}</span>
      <h3>macOS</h3>
      <p>Keep Kaede close on your Mac. Choose the download that matches your Mac’s processor.</p>
      <div class="download-options">
        {@render download('macArm', 'Apple silicon')}
        {@render download('macIntel', 'Intel Mac', true)}
      </div>
      <p class="download-note">macOS 11 or later</p>
    </article>
    <article>
      <span class="download-icon"><Icon name="server" size={26} /></span>
      <h3>Linux</h3>
      <p>
        Pick the package for your distribution. All Linux downloads are for 64-bit Intel and AMD
        computers.
      </p>
      {@render download('deb', 'Download .deb')}
      <p class="download-note">Ubuntu 24.04+, Debian 13+, and compatible distributions</p>
      <div class="download-options">
        <div>
          {@render download('rpm', 'Download .rpm', true)}
          <p class="download-note">RHEL 10 / AlmaLinux 10 / Rocky Linux 10 · EPEL required</p>
        </div>
        <div>
          {@render download('appimage', 'AppImage', true)}
          <p class="download-note">Ubuntu 24.04+ and compatible distributions</p>
        </div>
      </div>
      <details>
        <summary>Linux installation help</summary>
        <p>
          For RPM, enable EPEL and your distribution’s CodeReady Builder (CRB) repository first,
          then install the downloaded file with <code>sudo dnf install ./Kaede-Chat-*.rpm</code>.
        </p>
        <p>
          For AppImage, open the file’s Properties, allow it to run as a program, then double-click
          it.
        </p>
      </details>
    </article>
    <article>
      <span class="download-icon"><Icon name="globe" size={26} /></span>
      <h3>Web app</h3>
      <p>
        Jump into the conversation from your browser. No download needed, on your computer, phone,
        or tablet.
      </p>
      <a class="primary-button" href={resolve('/login')}
        >Open web app <Icon name="chevron-right" size={18} /></a
      >
    </article>
  </div>

  <div class="download-mobile">
    <div class="download-heading">
      <span class="download-eyebrow">MOBILE</span>
      <h2>Android &amp; iOS</h2>
      <p>
        Your communities, wherever you are. Download a mobile app, or open Kaede in your phone’s
        browser.
      </p>
    </div>
    <div class="download-grid">
      <article>
        <span class="download-icon">{@render deviceIcon(true)}</span>
        <h3>Android</h3>
        <p>Take your conversations with you. Get Kaede on Google Play for automatic app updates.</p>
        <a
          class="primary-button"
          href="https://play.google.com/store/apps/details?id=chat.kaede.mobile"
          >Get it on Google Play <Icon name="chevron-right" size={18} /></a
        >
        <details>
          <summary>Prefer to download the APK?</summary>
          {@render download('android', 'Download Android APK', true)}
          <p><strong>How to install it</strong></p>
          <ol>
            <li>Download the APK, then open the file when it finishes.</li>
            <li>
              If prompted, allow your browser or file manager to install apps from this source.
            </li>
            <li>Follow the installation prompts. You can turn that permission off afterwards.</li>
          </ol>
          <p>Return here to download future updates.</p>
        </details>
      </article>
      <article>
        <span class="download-icon">{@render deviceIcon(true)}</span>
        <h3>iOS</h3>
        <p>
          Use Kaede on your iPhone or iPad in the browser. Add it to your Home Screen for easy
          access.
        </p>
        <a class="primary-button" href={resolve('/login')}
          >Open Kaede on iOS <Icon name="chevron-right" size={18} /></a
        >
        <details>
          <summary>Looking for the iOS app file?</summary>
          <p>
            The IPA is for manual installation with compatible Apple signing and distribution tools.
            It cannot be installed by simply opening it on your iPhone.
          </p>
          {@render download('ios', 'Download iOS IPA', true)}
        </details>
      </article>
    </div>
  </div>
  <a class="secondary-button download-web" href={resolve('/login')}
    ><Icon name="globe" size={18} /> Use Kaede on your phone (web)</a
  >
</section>

<style>
  .cl-downloads {
    width: min(1180px, calc(100% - 40px));
    margin: 0 auto;
    padding: 4rem 0;
    scroll-margin-top: 1.5rem;
  }
  .download-heading {
    max-width: 660px;
  }
  .download-eyebrow {
    color: var(--accent-text);
    font-size: 0.78rem;
    font-weight: 800;
    letter-spacing: 0.18em;
  }
  h2 {
    margin-top: 1rem;
    font-family: var(--font-display);
    font-size: clamp(2rem, 4vw, 3.5rem);
    font-weight: 800;
    line-height: 1.06;
    letter-spacing: -0.04em;
  }
  p {
    color: var(--text-soft);
    line-height: 1.7;
  }
  .download-heading p {
    margin-top: 1.3rem;
    font-size: 1.05rem;
  }
  .download-status {
    margin: 2rem 0 1.1rem;
    font-size: 0.85rem;
  }
  .download-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    border-top: 1px solid var(--line);
  }
  article {
    min-width: 0;
    padding: 2rem 2rem 2.2rem 0;
    border-bottom: 1px solid var(--line);
  }
  article:nth-child(even) {
    padding-left: 2rem;
    padding-right: 0;
    border-left: 1px solid var(--line);
  }
  .download-icon {
    display: inline-flex;
    color: var(--accent-text);
    margin-bottom: 1.2rem;
  }
  h3 {
    font-size: 1.2rem;
    margin-bottom: 0.85rem;
  }
  article > p:not(.download-note) {
    margin-bottom: 1.6rem;
  }
  .download-options {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1rem;
  }
  .download-option {
    min-width: 0;
  }
  .download-option :is(a.primary-button, a.secondary-button, button) {
    display: flex;
    width: 100%;
    min-height: 48px;
    gap: 0.55rem;
    text-align: center;
    text-decoration: none;
  }
  article > a.primary-button {
    display: inline-flex;
    min-height: 48px;
    gap: 0.55rem;
  }
  button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }
  .download-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem 0.8rem;
    margin-top: 0.65rem;
    color: var(--text-muted);
    font-size: 0.76rem;
  }
  .download-note {
    margin: 0.5rem 0 1rem;
    font-size: 0.8rem;
  }
  .download-mobile {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 2fr);
    gap: 2.5rem;
    margin-top: 4rem;
    padding-top: 4rem;
    border-top: 1px solid var(--line);
  }
  .download-mobile h2 {
    font-size: clamp(2rem, 3.2vw, 2.7rem);
  }
  .download-mobile article {
    padding-right: 1.5rem;
  }
  .download-mobile article:nth-child(even) {
    padding-left: 1.5rem;
    padding-right: 0;
  }
  details {
    margin-top: 1.5rem;
    padding-top: 1rem;
    border-top: 1px solid var(--line);
  }
  summary {
    min-height: 44px;
    padding: 0.5rem 0;
    cursor: pointer;
    color: var(--accent-text);
    font-size: 0.85rem;
    font-weight: 700;
  }
  details p,
  ol {
    margin: 0.7rem 0;
    color: var(--text-soft);
    font-size: 0.85rem;
    line-height: 1.7;
  }
  ol {
    padding-left: 1.25rem;
  }
  li + li {
    margin-top: 0.5rem;
  }
  code {
    overflow-wrap: anywhere;
  }
  .download-web {
    display: flex;
    width: fit-content;
    min-height: 48px;
    gap: 0.6rem;
    margin: 2rem 0 0 auto;
  }
  @media (max-width: 1000px) {
    .download-mobile {
      grid-template-columns: 1fr;
      gap: 2rem;
    }
  }
  @media (max-width: 640px) {
    .cl-downloads {
      padding: 3rem 0;
    }
    .download-grid {
      grid-template-columns: 1fr;
    }
    article,
    article:nth-child(even),
    .download-mobile article,
    .download-mobile article:nth-child(even) {
      border-left: 0;
      padding: 1.8rem 0;
    }
    .download-options {
      grid-template-columns: 1fr;
    }
    .download-mobile {
      margin-top: 2.5rem;
      padding-top: 2.5rem;
    }
    .download-web {
      width: 100%;
      margin-top: 1.5rem;
      text-align: center;
    }
  }
</style>
