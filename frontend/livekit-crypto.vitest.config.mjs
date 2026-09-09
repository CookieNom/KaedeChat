import { createRequire } from 'node:module';
import { dirname } from 'node:path';
const require = createRequire(import.meta.url);
export default {
  esbuild: { tsconfigRaw: { compilerOptions: { useDefineForClassFields: false } } },
  test: {
    root: dirname(dirname(require.resolve('livekit-client'))),
    include: ['src/e2ee/worker/av1Frame.test.ts', 'src/e2ee/worker/FrameCryptor.test.ts'],
    environment: 'node'
  }
};
