import { createRequire } from 'node:module';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const sdkRoot = dirname(dirname(require.resolve('livekit-client')));
const variantCheck = fileURLToPath(
  new URL('./scripts/check-video-variant-crypto.mjs', import.meta.url)
);
export default {
  resolve: { alias: { '@livekit-e2ee': `${sdkRoot}/src/e2ee` } },
  esbuild: { tsconfigRaw: { compilerOptions: { useDefineForClassFields: false } } },
  test: {
    root: sdkRoot,
    include: [
      'src/e2ee/worker/av1Frame.test.ts',
      'src/e2ee/worker/FrameCryptor.test.ts',
      variantCheck
    ],
    environment: 'node'
  }
};
