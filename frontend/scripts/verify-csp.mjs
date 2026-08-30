import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { mediaUploadConnectSources } from './media-csp.mjs';

const html = await readFile(new URL('../build/index.html', import.meta.url), 'utf8');
const meta = html.match(
  /<meta\s+http-equiv="content-security-policy"\s+content="([^"]+)"\s*\/?\s*>/i
);

if (!meta) throw new Error('static SPA fallback is missing its generated CSP meta policy');

const policy = meta[1];
if (!policy.includes('script-src')) throw new Error('generated CSP has no script-src directive');
const normalizedPolicy = policy.replaceAll('&#39;', "'");
const scriptPolicy = normalizedPolicy
  .split(';')
  .map((directive) => directive.trim())
  .find((directive) => directive === 'script-src' || directive.startsWith('script-src '));
const scriptTokens = new Set(scriptPolicy?.split(/\s+/).slice(1) ?? []);
if (!scriptTokens.has("'wasm-unsafe-eval'")) {
  throw new Error('generated script-src does not permit the OpenMLS WebAssembly runtime');
}
if (scriptTokens.has("'unsafe-eval'")) {
  throw new Error('generated script-src broadly permits JavaScript unsafe-eval');
}
if (!policy.includes('base-uri &#39;none&#39;') && !policy.includes("base-uri 'none'")) {
  throw new Error('generated CSP does not disable base URI injection');
}
if (/script-src[^;]*unsafe-inline/.test(policy)) {
  throw new Error('generated script-src unexpectedly permits unsafe-inline');
}
if (!policy.includes('https://challenges.cloudflare.com')) {
  throw new Error('generated CSP does not authorize Cloudflare Turnstile');
}
const framePolicy = normalizedPolicy
  .split(';')
  .map((directive) => directive.trim())
  .find((directive) => directive === 'frame-src' || directive.startsWith('frame-src '));
const frameTokens = new Set(framePolicy?.split(/\s+/).slice(1) ?? []);
if (!frameTokens.has('https://www.youtube-nocookie.com')) {
  throw new Error('generated frame-src does not authorize the privacy-enhanced YouTube player');
}
if (frameTokens.has('https:') || frameTokens.has('*') || frameTokens.has('https://youtube.com')) {
  throw new Error('generated frame-src authorizes an overly broad video origin');
}
if (/style-src(?:-attr)?[^;]*unsafe-inline/.test(policy)) {
  throw new Error('generated style policy unexpectedly permits unsafe-inline');
}
if (
  !policy.includes('style-src-attr') ||
  !policy.includes('unsafe-hashes') ||
  !policy.includes('sha256-S8qMpvofolR8Mpjy4kQvEm7m1q8clzU4dfDH0AmvZjo=')
) {
  throw new Error('generated CSP does not narrowly authorize the SvelteKit announcer style');
}

const configuredMediaOrigins = mediaUploadConnectSources();
for (const origin of configuredMediaOrigins) {
  if (!policy.includes(origin)) {
    throw new Error(`generated connect-src does not authorize media upload origin ${origin}`);
  }
}

const pathStyleVector = mediaUploadConnectSources({
  KAEDE_MEDIA_PUBLIC_BASE_URL: 'https://media.chat.example',
  KAEDE_MEDIA_S3_ADDRESSING_STYLE: 'path'
});
const virtualStyleVector = mediaUploadConnectSources({
  KAEDE_MEDIA_PUBLIC_BASE_URL: 'https://s3.us-west-004.backblazeb2.com',
  KAEDE_MEDIA_S3_ADDRESSING_STYLE: 'virtual',
  KAEDE_MEDIA_ATTACHMENTS_BUCKET: 'kaede-attachments'
});
const explicitOriginsVector = mediaUploadConnectSources({
  KAEDE_MEDIA_PUBLIC_BASE_URL: 'https://ignored.example',
  KAEDE_MEDIA_UPLOAD_ORIGINS:
    'https://alpha-media.chat.example, https://beta-media.chat.example https://alpha-media.chat.example'
});
if (pathStyleVector[0] !== 'https://media.chat.example') {
  throw new Error('path-style media CSP vector drifted');
}
if (virtualStyleVector[0] !== 'https://kaede-attachments.s3.us-west-004.backblazeb2.com') {
  throw new Error('virtual-hosted media CSP vector drifted');
}
if (
  explicitOriginsVector.join(' ') !==
  'https://alpha-media.chat.example https://beta-media.chat.example'
) {
  throw new Error('explicit media CSP origins vector drifted');
}

const inlineScripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)]
  .map((match) => match[1])
  .filter((source) => source.trim().length > 0);

if (inlineScripts.length === 0) {
  throw new Error('expected the SvelteKit SPA fallback to contain an inline bootstrap');
}
if (html.indexOf(meta[0]) > html.indexOf('<script')) {
  throw new Error('generated CSP meta policy appears after executable content');
}

for (const source of inlineScripts) {
  const hash = createHash('sha256').update(source).digest('base64');
  if (!policy.includes(`sha256-${hash}`)) {
    throw new Error(`generated CSP does not authorize inline script sha256-${hash}`);
  }
}

const caddy = await readFile(new URL('../../deploy/Caddyfile', import.meta.url), 'utf8');
const edgePolicy = caddy.match(/^\s*Content-Security-Policy\s+"([^"]+)"/m)?.[1];
if (!edgePolicy || !edgePolicy.includes("frame-ancestors 'none'")) {
  throw new Error('Caddy is missing its header-only frame-ancestors policy');
}
if (/\b(?:default-src|script-src|style-src)\b/.test(edgePolicy)) {
  throw new Error('Caddy resource CSP would override SvelteKit build-specific hashes');
}

const tauriConfig = JSON.parse(
  await readFile(new URL('../../desktop/tauri/src-tauri/tauri.conf.json', import.meta.url), 'utf8')
);
const tauriPolicy = tauriConfig?.app?.security?.csp;
if (typeof tauriPolicy !== 'string') {
  throw new Error('Tauri is missing its content security policy');
}
const tauriScriptPolicy = tauriPolicy
  .split(';')
  .map((directive) => directive.trim())
  .find((directive) => directive === 'script-src' || directive.startsWith('script-src '));
const tauriScriptTokens = new Set(tauriScriptPolicy?.split(/\s+/).slice(1) ?? []);
if (!tauriScriptTokens.has("'wasm-unsafe-eval'")) {
  throw new Error('Tauri script-src does not permit the OpenMLS WebAssembly runtime');
}
if (tauriScriptTokens.has("'unsafe-eval'")) {
  throw new Error('Tauri script-src broadly permits JavaScript unsafe-eval');
}
const tauriFramePolicy = tauriPolicy
  .split(';')
  .map((directive) => directive.trim())
  .find((directive) => directive === 'frame-src' || directive.startsWith('frame-src '));
const tauriFrameTokens = new Set(tauriFramePolicy?.split(/\s+/).slice(1) ?? []);
if (tauriFrameTokens.size !== 1 || !tauriFrameTokens.has('https://www.youtube-nocookie.com')) {
  throw new Error('Tauri frame-src must allow only the privacy-enhanced YouTube player');
}

process.stdout.write(`CSP verification passed (${inlineScripts.length} inline script hash)\n`);
