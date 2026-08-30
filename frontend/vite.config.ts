import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';
import {
  normalizeLandingVariant,
  resolveOperatorLegalConfig
} from './src/lib/branding/public-build-config';

// Build-time selection of the public landing page. The default landing page is
// the project's public face for anyone self-hosting, so an unrecognized value
// (including an unset variable) falls back to it. Only the exact value
// `custom` opts a host into the operator-facing landing page.
const landingPage = normalizeLandingVariant(process.env.KAEDE_LANDING_PAGE);
const operatorLegalConfig = resolveOperatorLegalConfig(process.env, landingPage);

export default defineConfig({
  plugins: [sveltekit()],
  define: {
    'import.meta.env.KAEDE_LANDING_PAGE': JSON.stringify(landingPage),
    'import.meta.env.KAEDE_OPERATOR_LEGAL_CONFIG': JSON.stringify(operatorLegalConfig)
  },
  test: { include: ['src/**/*.test.ts'] }
});
