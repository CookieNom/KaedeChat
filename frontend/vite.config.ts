import adapter from '@sveltejs/adapter-static';
import { fileURLToPath } from 'node:url';
import { mediaUploadConnectSources } from './scripts/media-csp.mjs';
import { sveltekit, type Config } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';
import {
  normalizeLandingVariant,
  resolveOperatorLegalConfig
} from './src/lib/branding/public-build-config.ts';

// Build-time selection of the public landing page. The default landing page is
// the project's public face for anyone self-hosting, so an unrecognized value
// (including an unset variable) falls back to it. Only the exact value
// `custom` opts a host into the operator-facing landing page.
type CspDirectives = NonNullable<NonNullable<Config['csp']>['directives']>;
const mediaUploadOrigins = mediaUploadConnectSources() as NonNullable<CspDirectives['connect-src']>;
const landingPage = normalizeLandingVariant(process.env.KAEDE_LANDING_PAGE);
const operatorLegalConfig = resolveOperatorLegalConfig(process.env, landingPage);

export default defineConfig({
  plugins: [
    sveltekit({
      adapter: adapter({ fallback: 'index.html' }),
      // The static SPA fallback contains a small, build-specific inline bootstrap.
      // Hash mode lets SvelteKit place the exact script hash in a CSP meta tag
      // instead of weakening script-src with unsafe-inline. Caddy supplies the
      // header-only frame-ancestors directive at the production edge.
      csp: {
        mode: 'hash',
        directives: {
          'default-src': ['self'],
          'base-uri': ['none'],
          'object-src': ['none'],
          'form-action': ['self'],
          // OpenMLS loads its audited WebAssembly module at runtime. This CSP
          // keyword permits WebAssembly compilation without enabling JavaScript
          // string evaluation (`unsafe-eval`).
          'script-src': ['self', 'wasm-unsafe-eval', 'https://challenges.cloudflare.com'],
          'style-src': ['self'],
          // SvelteKit's accessibility announcer has one framework-owned static
          // style attribute. Authorize only that exact value rather than all
          // inline styles.
          'style-src-attr': [
            'unsafe-hashes',
            'sha256-S8qMpvofolR8Mpjy4kQvEm7m1q8clzU4dfDH0AmvZjo='
          ],
          'img-src': ['self', 'data:', 'blob:', 'https:'],
          'media-src': ['self', 'blob:', 'https:'],
          // Voice rooms are authoritative on the guild's home instance. A
          // federated member therefore connects directly to that instance's
          // secure LiveKit signaling endpoint after receiving a signed grant.
          // Federated soundboard capabilities can point at any guild authority's
          // exact media.<authority> host. Runtime validation narrows each fetch.
          'connect-src': [
            'self',
            'wss:',
            'https:',
            'https://challenges.cloudflare.com',
            ...mediaUploadOrigins
          ],
          // Directory videos use YouTube's privacy-enhanced player. Product-page
          // code constructs this origin from a validated 11-character video ID;
          // arbitrary publisher iframe origins are never accepted.
          'frame-src': [
            'https://challenges.cloudflare.com',
            'https://www.youtube-nocookie.com',
            'https://open.spotify.com'
          ],
          'font-src': ['self', 'data:']
        }
      }
    })
  ],
  resolve: {
    alias: { $lib: fileURLToPath(new URL('./src/lib', import.meta.url)) },
    ...(process.env.VITEST ? { conditions: ['browser'] } : {})
  },
  define: {
    'import.meta.env.KAEDE_LANDING_PAGE': JSON.stringify(landingPage),
    'import.meta.env.KAEDE_OPERATOR_LEGAL_CONFIG': JSON.stringify(operatorLegalConfig)
  },
  test: {
    include: ['src/**/*.test.ts'],
    // Unit tests inspect embeds without contacting third-party iframe servers.
    environmentOptions: { happyDOM: { settings: { disableIframePageLoading: true } } }
  }
});
