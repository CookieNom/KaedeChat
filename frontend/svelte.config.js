import adapter from '@sveltejs/adapter-static';
import { mediaUploadConnectSources } from './scripts/media-csp.mjs';

const mediaUploadOrigins = mediaUploadConnectSources();

/** @type {import('@sveltejs/kit').Config} */
const config = {
  kit: {
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
        'script-src': ['self'],
        'style-src': ['self'],
        // SvelteKit's accessibility announcer has one framework-owned static
        // style attribute. Authorize only that exact value rather than all
        // inline styles.
        'style-src-attr': ['unsafe-hashes', 'sha256-S8qMpvofolR8Mpjy4kQvEm7m1q8clzU4dfDH0AmvZjo='],
        'img-src': ['self', 'data:', 'blob:', 'https:'],
        'media-src': ['self', 'blob:', 'https:'],
        'connect-src': ['self', ...mediaUploadOrigins],
        'font-src': ['self', 'data:']
      }
    }
  }
};

export default config;
