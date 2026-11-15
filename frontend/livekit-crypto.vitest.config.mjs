import { createRequire } from 'node:module';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const sdkRoot = dirname(dirname(require.resolve('livekit-client')));
const checks = ['check-video-variant-crypto.mjs', 'check-video-codec-options.mjs'].map((name) =>
  fileURLToPath(new URL(`./scripts/${name}`, import.meta.url))
);
export default {
  resolve: { alias: { '@livekit-e2ee': `${sdkRoot}/src/e2ee`, '@livekit-sdk': `${sdkRoot}/src` } },
  oxc: {
    assumptions: { setPublicClassFields: true },
    typescript: { removeClassFieldsWithoutInitializer: true }
  },
  test: {
    root: sdkRoot,
    include: [
      'src/e2ee/worker/av1Frame.test.ts',
      'src/e2ee/worker/FrameCryptor.test.ts',
      'src/e2ee/worker/naluUtils.test.ts',
      ...checks
    ],
    environment: 'node'
  }
};
