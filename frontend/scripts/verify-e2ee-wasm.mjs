import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

import init, { KaedeMlsClient } from '../src/lib/e2ee/wasm/kaede_e2ee.js';

const moduleBytes = await readFile(
  new URL('../src/lib/e2ee/wasm/kaede_e2ee_bg.wasm', import.meta.url)
);
await init({ module_or_path: moduleBytes });

const credential = new TextEncoder().encode('kaede-e2ee-wasm-artifact-check');
let client;
let peer;
let pending;
let keyPackage;
let identity;
let inspectedCredential;
let signatureKey;
try {
  client = new KaedeMlsClient(credential);
  peer = new KaedeMlsClient(new TextEncoder().encode('kaede-wasm-peer'));
  const group = new TextEncoder().encode('kaede-wasm-call');
  client.createGroup(group);
  pending = client.addMembers(group, [peer.generateKeyPackage()]);
  client.mergePendingCommit(group);
  assert.deepEqual(peer.joinGroup(pending.welcome), group);
  assert.equal(client.groupEpoch(group), 1n);
  assert.equal(peer.groupEpoch(group), client.groupEpoch(group));
  assert.deepEqual(
    peer.exportEpochSecret(group, 'kaede livekit v1', group, 32),
    client.exportEpochSecret(group, 'kaede livekit v1', group, 32)
  );
  assert.throws(() => client.signServerChallenge(group));
  keyPackage = client.generateKeyPackage();
  identity = client.inspectKeyPackage(keyPackage);
  inspectedCredential = identity.credential;
  signatureKey = identity.signatureKey;
  if (
    inspectedCredential.length !== credential.length ||
    !inspectedCredential.every((byte, index) => byte === credential[index]) ||
    signatureKey.length !== 32
  ) {
    throw new Error('checked-in OpenMLS WebAssembly bindings do not match the Rust source API');
  }
} finally {
  credential.fill(0);
  keyPackage?.fill(0);
  inspectedCredential?.fill(0);
  signatureKey?.fill(0);
  identity?.free();
  pending?.free();
  peer?.free();
  client?.free();
}

process.stdout.write('OpenMLS WebAssembly artifact verification passed\n');
