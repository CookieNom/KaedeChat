/* tslint:disable */
/* eslint-disable */

export class KaedeMlsClient {
  free(): void;
  [Symbol.dispose](): void;
  addMembers(group_id: Uint8Array, key_packages: Array<any>): KaedeMlsPendingCommit;
  createGroup(group_id: Uint8Array): void;
  encrypt(group_id: Uint8Array, plaintext: Uint8Array, aad: Uint8Array): Uint8Array;
  exportEpochSecret(
    group_id: Uint8Array,
    label: string,
    context: Uint8Array,
    length: number
  ): Uint8Array;
  exportState(): Uint8Array;
  generateKeyPackage(): Uint8Array;
  hasGroup(group_id: Uint8Array): boolean;
  inspectKeyPackage(key_package: Uint8Array): KaedeMlsKeyPackageIdentity;
  joinGroup(welcome: Uint8Array): Uint8Array;
  memberRoster(group_id: Uint8Array): Uint8Array;
  mergePendingCommit(group_id: Uint8Array): void;
  constructor(credential: Uint8Array);
  process(group_id: Uint8Array, wire_message: Uint8Array): KaedeMlsProcessedMessage;
  publicIdentityKey(): Uint8Array;
  removeAccounts(group_id: Uint8Array, accounts: Array<any>): KaedeMlsPendingCommit;
  static restoreState(state: Uint8Array): KaedeMlsClient;
  signServerChallenge(challenge: Uint8Array): Uint8Array;
}

export class KaedeMlsKeyPackageIdentity {
  private constructor();
  free(): void;
  [Symbol.dispose](): void;
  readonly credential: Uint8Array;
  readonly signatureKey: Uint8Array;
}

export class KaedeMlsPendingCommit {
  private constructor();
  free(): void;
  [Symbol.dispose](): void;
  readonly commit: Uint8Array;
  readonly welcome: Uint8Array;
}

export class KaedeMlsProcessedMessage {
  private constructor();
  free(): void;
  [Symbol.dispose](): void;
  readonly aad: Uint8Array | undefined;
  readonly application: Uint8Array | undefined;
  readonly credential: Uint8Array | undefined;
  readonly kind: string;
}

export type InitInput = RequestInfo | URL | Response | BufferSource | WebAssembly.Module;

export interface InitOutput {
  readonly memory: WebAssembly.Memory;
  readonly __wbg_kaedemlsclient_free: (a: number, b: number) => void;
  readonly __wbg_kaedemlskeypackageidentity_free: (a: number, b: number) => void;
  readonly __wbg_kaedemlspendingcommit_free: (a: number, b: number) => void;
  readonly __wbg_kaedemlsprocessedmessage_free: (a: number, b: number) => void;
  readonly kaedemlsclient_addMembers: (
    a: number,
    b: number,
    c: number,
    d: number,
    e: number
  ) => void;
  readonly kaedemlsclient_createGroup: (a: number, b: number, c: number, d: number) => void;
  readonly kaedemlsclient_encrypt: (
    a: number,
    b: number,
    c: number,
    d: number,
    e: number,
    f: number,
    g: number,
    h: number
  ) => void;
  readonly kaedemlsclient_exportEpochSecret: (
    a: number,
    b: number,
    c: number,
    d: number,
    e: number,
    f: number,
    g: number,
    h: number,
    i: number
  ) => void;
  readonly kaedemlsclient_exportState: (a: number, b: number) => void;
  readonly kaedemlsclient_generateKeyPackage: (a: number, b: number) => void;
  readonly kaedemlsclient_hasGroup: (a: number, b: number, c: number, d: number) => void;
  readonly kaedemlsclient_inspectKeyPackage: (a: number, b: number, c: number, d: number) => void;
  readonly kaedemlsclient_joinGroup: (a: number, b: number, c: number, d: number) => void;
  readonly kaedemlsclient_memberRoster: (a: number, b: number, c: number, d: number) => void;
  readonly kaedemlsclient_mergePendingCommit: (a: number, b: number, c: number, d: number) => void;
  readonly kaedemlsclient_new: (a: number, b: number, c: number) => void;
  readonly kaedemlsclient_process: (
    a: number,
    b: number,
    c: number,
    d: number,
    e: number,
    f: number
  ) => void;
  readonly kaedemlsclient_publicIdentityKey: (a: number, b: number) => void;
  readonly kaedemlsclient_removeAccounts: (
    a: number,
    b: number,
    c: number,
    d: number,
    e: number
  ) => void;
  readonly kaedemlsclient_restoreState: (a: number, b: number, c: number) => void;
  readonly kaedemlsclient_signServerChallenge: (a: number, b: number, c: number, d: number) => void;
  readonly kaedemlskeypackageidentity_credential: (a: number, b: number) => void;
  readonly kaedemlskeypackageidentity_signatureKey: (a: number, b: number) => void;
  readonly kaedemlspendingcommit_commit: (a: number, b: number) => void;
  readonly kaedemlspendingcommit_welcome: (a: number, b: number) => void;
  readonly kaedemlsprocessedmessage_aad: (a: number, b: number) => void;
  readonly kaedemlsprocessedmessage_application: (a: number, b: number) => void;
  readonly kaedemlsprocessedmessage_credential: (a: number, b: number) => void;
  readonly kaedemlsprocessedmessage_kind: (a: number, b: number) => void;
  readonly __wbindgen_export: (a: number, b: number) => number;
  readonly __wbindgen_export2: (a: number, b: number, c: number, d: number) => number;
  readonly __wbindgen_export3: (a: number) => void;
  readonly __wbindgen_add_to_stack_pointer: (a: number) => number;
  readonly __wbindgen_export4: (a: number, b: number, c: number) => void;
}

export type SyncInitInput = BufferSource | WebAssembly.Module;

/**
 * Instantiates the given `module`, which can either be bytes or
 * a precompiled `WebAssembly.Module`.
 *
 * @param {{ module: SyncInitInput }} module - Passing `SyncInitInput` directly is deprecated.
 *
 * @returns {InitOutput}
 */
export function initSync(module: { module: SyncInitInput } | SyncInitInput): InitOutput;

/**
 * If `module_or_path` is {RequestInfo} or {URL}, makes a request and
 * for everything else, calls `WebAssembly.instantiate` directly.
 *
 * @param {{ module_or_path: InitInput | Promise<InitInput> }} module_or_path - Passing `InitInput` directly is deprecated.
 *
 * @returns {Promise<InitOutput>}
 */
export default function __wbg_init(
  module_or_path?:
    { module_or_path: InitInput | Promise<InitInput> } | InitInput | Promise<InitInput>
): Promise<InitOutput>;
