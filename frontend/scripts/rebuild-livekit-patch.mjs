// Rebuild a pnpm patch workspace, then run pnpm patch-commit on that directory.
import { createRequire } from 'node:module';
import { realpathSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
const require = createRequire(import.meta.url);
const { build } = createRequire(require.resolve('vite'))('esbuild');

const root = resolve(process.argv[2] ?? 'node_modules/livekit-client');
const dependencies = dirname(dirname(realpathSync(require.resolve('livekit-client'))));
const common = {
  bundle: true,
  platform: 'browser',
  target: 'es2020',
  minify: true,
  nodePaths: [resolve(dependencies, '..')],
  tsconfigRaw: { compilerOptions: { useDefineForClassFields: false } }
};
for (const [format, suffix] of [
  ['esm', 'mjs'],
  ['iife', 'js']
]) {
  await build({
    ...common,
    entryPoints: [`${root}/src/e2ee/worker/e2ee.worker.ts`],
    outfile: `${root}/dist/livekit-client.e2ee.worker.${suffix}`,
    format
  });
}
await build({
  ...common,
  entryPoints: [`${root}/src/index.ts`],
  outfile: `${root}/dist/livekit-client.esm.mjs`,
  format: 'esm'
});
await build({
  ...common,
  entryPoints: [`${root}/src/index.ts`],
  outfile: `${root}/dist/livekit-client.umd.js`,
  format: 'iife',
  globalName: 'LivekitClient',
  footer: {
    js: 'if (typeof module !== "undefined" && module.exports) module.exports = LivekitClient; else if (typeof define === "function" && define.amd) define([], function () { return LivekitClient; });'
  }
});
