# End-to-end encryption protocol and rollout

This document is the security contract for Kaede E2EE. A UI lock or an opaque
`Message.e2ee` object is not, by itself, evidence that this contract is active.

## Scope and threat model

Against honest-but-curious servers and network attackers, an active room keeps
new message text, filenames, attachment plaintext, microphone audio, camera
video, and screen-share media confidential. MLS 1.0 (RFC 9420) is the room key
agreement protocol. Every approved account encryption identity is a distinct
MLS leaf. Signed-in clients for one account unlock and synchronize that same
identity through a password-encrypted account vault. The mandatory suite is
`MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519`.

The server still learns routing metadata: room and sender references,
participating users and homes, timestamps, ciphertext sizes, delivery and
download activity, and voice track kinds. E2EE cannot prevent a recipient,
compromised endpoint, or malicious browser code delivered by an instance from
recording plaintext.

Instance federation signatures authenticate servers and transport. They do not
authenticate user devices and must never substitute for MLS credentials.

Account identities are trust-on-first-use until participants compare the room
safety number through a separate trusted channel. Package self-signatures and
participant-home metadata reject inconsistent substitutions, but an actively
malicious authority can present a new, internally consistent first-seen
identity and join the room as an apparent participant. That permits an active
server to read content until the substitution is detected. Safety-number
comparison is the authentication step that detects that attack; it must be
repeated after a membership or identity change. Before that comparison the room
is encrypted, but participant identities—and therefore protection from an
active server—are unverified.

## Account identity and device access

An account has one active portable encryption identity. Registration uses an
Ed25519 proof of possession over a one-use server challenge bound to:

- the local account and current login session;
- the 32-byte device identity public key; and
- the SHA-256 digest of the bounded MLS credential.

The server stores public credentials, one-use MLS KeyPackages, and a bounded
AES-GCM ciphertext containing the portable MLS state. Its AES key is derived on
the client from the account password and is never sent to the server. Password
KDF protocol version 2 prepends the following client-controlled UTF-8 context
to each 16-byte server salt before PBKDF2-SHA256 (600,000 iterations):

`kaede-password-kdf-v2\0{auth|vault}\0{canonical locally selected home domain}\0`

The home domain must come from browser navigation or the locally configured
native client, never from a KDF response. This binds a captured authentication
secret to one home and makes the authentication and vault keys distinct even
if a malicious server repeats or relays salts. Authentication receives only
the `auth` output; the non-extractable AES-GCM `vault` key remains on the
client. Canonical deployments must redirect alternate hostnames before login
so every client derives against the account's advertised home domain. The
server never receives an identity private key, MLS group secret, recovery
secret, attachment key, or LiveKit frame-encryption key.

Vault mutations acquire a short per-account lease and use monotonic revision
compare-and-swap. This is a security requirement: two clients may not advance
the same MLS state concurrently. The lease protects only ordering and never
contains a key or plaintext. Explicit logout and expired authentication clear
local vault keys and MLS state; another signed-in client can download and
decrypt the latest ciphertext again.

Vault format 2 also encrypts a monotonic sequence and its parent-chain
commitment inside the portable state, and binds that same sequence into AES-GCM
authenticated data. The server retains only an append-only 32-byte digest for
each opaque envelope revision. Clients extend those digests with the domain-
separated chain `R_n = SHA256(R_(n-1) || u64BE(n) || D_n)` and require the
latest decrypted state to authenticate the exact computed `R_(n-1)`. This
detects a higher-numbered stale fork as well as a lower revision, changed digest
at the same revision, or relabeled sequence.

Each native client keeps its last confirmed revision, digest, and chain root in
platform-protected storage under a hashed account label. The non-secret compact
checkpoint deliberately survives logout, expired authentication, and ordinary
local MLS-state deletion; secret vault keys, MLS state, and plaintext caches do
not. A brand-new client has no prior checkpoint and necessarily treats its
first complete chain as trust-on-first-use. Comparing safety numbers or
restoring from an already trusted client remains the defense against a
malicious account home on first contact. Only a successful authenticated
password/E2EE reset may clear the checkpoint. Recovery import first verifies
the backup locally, then performs that authenticated reset, and only then
reseals the trusted recovered state at sequence 1 with the zero parent.
An E2EE reset also creates a five-minute, one-time recovery authorization whose
raw value is returned only to the initiating login session; the server stores
only its hash. Device enrollment consumes the bearer, but the durable session
fence remains until both the replacement identity and its revision-one vault
are committed. Either transaction may finish first, so a crash between them
cannot let another signed-in session publish an old identity or repopulate an
old vault. Until both artifacts exist, other sessions cannot acquire or write
the opaque account vault or supersede the reset. The initiating session may
repeat a response-lost reset, and an expired fence may be replaced by another
explicit authenticated reset.

The synchronized vault keeps at most 2,000 recently decrypted messages and an
8 MiB serialized plaintext-cache budget, whichever limit is reached first.
This leaves fixed headroom for MLS state and recovery journals inside the 32
MiB vault limit. Ciphertext remains in the conversation, but plaintext that has
aged out of every trusted client and recovery backup may no longer be
recoverable on a newly signed-in client. A future chunked encrypted-history
store can remove this bounded-cache tradeoff without exposing content to the
server.

Rust and native-client bindings zero mutable secret and plaintext buffers when
their ownership ends, and browser callers explicitly clear mutable typed
arrays. Browser JavaScript engines can still create immutable strings or
garbage-collected internal copies that application code cannot reliably erase.
Kaede therefore bounds their lifetime and never persists or logs them, but does
not claim forensic erasure from a compromised endpoint's RAM. The endpoint and
malicious-client limitations in this threat model still apply.

Login sessions remain independently revocable. Rotating the shared encryption
identity is an explicit destructive operation: it revokes all prior identity
records, pauses affected rooms for rekey, and abandons encrypted history that is
not present in the synchronized vault or a recovery backup. Identity-list
generations are monotonic and propagated with federated profiles; an older
profile can never roll the generation backward.

KeyPackage uploads are signed by the device identity and bind the device, suite,
expiry, order, and digest of every package. Claims must be atomic and one-use.
This proof establishes possession of the package's embedded key; it does not by
itself establish who owns a first-seen key.

## Room policy

Each channel carries a monotonic `encryption_policy_generation` and explicit
state:

`plaintext -> proposed -> activating -> active <-> rekeying`

`failed` is terminal for that proposal. `legacy` identifies pre-protocol opaque
transport and must never be described as MLS E2EE.

Once a generation becomes active, it cannot return to plaintext. A user who
wants an unencrypted conversation creates a new room. Every participating home
must validate the exact mode, state, generation, protocol, suite, group ID, and
epoch. Missing capability reduces availability; it never causes downgrade.

Messages record the immutable policy generation and epoch under which the body
was admitted. This preserves honest pre-activation history. The database itself
rejects encrypted bodies in plaintext rooms, plaintext bodies in encrypted
rooms, and stale generation/epoch writes. Deletes remain possible for older
history. Application checks cover attachments and return stable client errors.

Membership changes are MLS control operations. Removing a member or rotating an
account identity pauses new application messages in `rekeying` until an authority-ordered
commit excludes it. A new member receives future history by default. Plaintext
system notices are not inserted into an encrypted room.

## Message envelope

The version-1 JSON object remains legacy opaque transport. The MLS application
envelope uses version 2 and binds, in authenticated data:

- protocol and cipher suite;
- canonical room reference and MLS group ID;
- sender device (the MLS BasicCredential independently binds the author account);
- policy generation and MLS epoch;
- operation (`create`, `edit`, or a control operation);
- operation and edit target, when applicable; and
- encrypted attachment-manifest digest.

Servers validate the bounded public context and relay the MLS wire message as
opaque bytes. Clients reject any mismatch before attempting decryption.

## Reports and voluntary disclosure

Reporting never uploads a room key. If a reporter chooses to share an encrypted
message, the client sends only the selected decrypted text and minimal message
context after explicit confirmation. The server stores:

- the reporter-supplied plaintext;
- a SHA-256 fingerprint of the exact stored ciphertext envelope; and
- `server_verified=false` disclosure metadata.

Moderators must see the evidence as user-supplied and non-repudiation must not be
claimed. Reporting other encrypted messages requires a separate explicit
selection; surrounding history is not silently included.

## Files and previews

Web, packaged desktop, Android, and iOS clients implement `e2ee-media/1`.
Attachment sends in an encrypted room fail closed if local cryptography is
unavailable; no fallback plaintext upload is allowed.

Encrypted files use a versioned, chunked AEAD format. True filename, MIME type,
dimensions, duration, plaintext hashes, and preview relationships live in the
encrypted manifest. Server-visible metadata contains only opaque blob IDs,
ciphertext sizes and digest, protocol version, and availability state. Sender
devices do not upload plaintext thumbnails or posters. Current clients show an
explicit download action; any future preview implementation must separately
encrypt preview bytes and describe their relationship only inside the manifest.

Servers authorize, quota, store, federate, cache, and delete ciphertext. They do
not MIME-sniff, decode, derive previews, or claim to malware-scan it. Clients say
“Encrypted—not scanned by Kaede” and warn before opening risky files. Automatic
server link previews are disabled for decrypted message text; a future manual
preview action must disclose that it reveals the URL to the instance.

## Voice and video

LiveKit media E2EE follows the same channel-scoped E2EE policy as messages and
files; it is not a separate per-call choice or downgrade. Encrypted text,
announcement, and DM channels protect their new messages and files, and any
supported call associated with that channel. Encrypted voice channels protect
mic, camera, screen video, and screen audio at endpoints. Media E2EE does not
hide signaling, participants, timing, track types, or traffic metadata.

Every call binds a monotonic media-policy generation, fresh media session ID,
crypto epoch, protocol, and suite in the authority response, federation state,
token metadata, and client state. These fields are downgrade-sensitive. Keys
never appear in LiveKit JWTs, SQL, Redis, federation JSON, logs, or telemetry.
The media session ID binds the channel, concrete LiveKit room, MLS group and
generation, epoch, protocol, and suite. Authorities require a non-revoked,
proof-of-possession device before minting an encrypted grant; remote homes sign
that device attestation. Admission webhooks compare every field with current
room policy, so a grant from before activation, rekey, or membership removal is
rejected. Activation, rekey, and encrypted group membership changes close the
old LiveKit room before publishing the new epoch. Clients install authenticated
device sender keys and enable frame encryption before connecting or publishing.
A missing key, old client, encryption failure, or mixed-mode move blocks media
instead of falling back. Users must join encrypted voice themselves; a moderator
cannot force-move a client without its device-bound media setup.

## Product disclosures

The full activation confirmation says that protection applies only to future
content; old history remains readable to servers; server search, automatic
previews, traditional webhooks, and ordinary bots stop; notifications may be
generic; encrypted files are not server-scanned; losing the synchronized vault,
all trusted local state, and the recovery backup loses history; unsupported
clients cannot use the room; metadata remains visible; and removed members
retain content already received. It also says the encrypted roster is
trust-on-first-use until participants compare the safety number through a
separate trusted channel, and that the comparison must be repeated after roster
or identity changes. Recovery settings also disclose that synchronized
decrypted history is bounded to the newest 2,000 messages or 8 MiB, so older
plaintext must remain on a trusted client or in a recovery backup.

After activation, clients use a persistent status affordance and contextual
explanations on disabled features. Repeat the full warning only for activation,
encrypted-room join, recovery, and integration changes. “Encrypted” and
“identity verified” are separate states.

## Release gate

`KAEDE_E2EE_ACTIVATION_ENABLED=true` is the default. Operators deploying clients
that have not passed the applicable release gates must explicitly set it to
`false`; doing so hides new activation and makes the API reject new proposals:

1. External review of the protocol, bindings, recovery, and metadata policy.
2. Cross-language RFC vectors for web, desktop, and mobile.
3. Three-home ordering tests for proposals, commits, welcomes, application
   messages, history, removal, revocation, and replay.
4. Ingress-matrix tests for every API, webhook, bot, federation, replica, and
   history path plus the database admission guard.
5. At-rest and forensic tests proving keys/plaintext do not enter server logs,
   databases, caches, crash reports, or native ciphertext caches, plus
   best-effort lifetime/zeroization tests for mutable client buffers. These
   tests must account for the browser-engine erasure limitation above rather
   than claiming universal RAM erasure.
6. Encrypted file tamper/chunk/manifest tests and cross-client LiveKit media
   tests, including packet loss and key rotation.

The repository's deterministic tests cover three-home authority binding,
membership and device-change fanout, durable MLS control catch-up and history
boundaries, retry after an unreachable room authority, three-member MLS exporter
rotation and removal, and stale media-grant rejection in web and mobile clients.
They do not emulate a real LiveKit SFU or inspect the frame counters maintained
inside LiveKit's web worker and native SDKs. Before media E2EE is released, the
remaining external gate must run web, desktop, Android, and iOS participants
against a real SFU while exercising microphone, camera, screen video, and screen
audio under loss, duplication, reordering, reconnect, and epoch rotation. It
must confirm that stale/replayed frames fail closed and that no client silently
downgrades to plaintext.

The deployment kill switch prevents only new proposals. Rekey, recovery,
reporting with explicit disclosure, and ordinary encrypted-room operation stay
available, and the switch can never authorize plaintext in an encrypted room.
