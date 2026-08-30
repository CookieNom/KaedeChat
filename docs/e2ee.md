# End-to-end encryption protocol and rollout

This document is the security contract for Kaede E2EE. A UI lock or an opaque
`Message.e2ee` object is not, by itself, evidence that this contract is active.

## Scope and threat model

MLS 1.0 (RFC 9420) is the room key agreement protocol, with
`MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519` as the mandatory suite. In an
active room, new message text, filenames, attachment plaintext, microphone
audio, camera video, and screen-share media stay confidential against
honest-but-curious servers and network attackers. Every approved account
encryption identity is a distinct MLS leaf, and a user's signed-in clients
unlock and synchronize that one identity through a password-encrypted account
vault.

The server still learns routing metadata: room and sender references,
participating users and homes, timestamps, ciphertext sizes, delivery and
download activity, and voice track kinds. E2EE also can't prevent a recipient,
a compromised endpoint, or malicious browser code delivered by an instance from
recording plaintext.

Instance federation signatures authenticate servers and transport. They don't
authenticate user devices and must never substitute for MLS credentials.

Account identities are trust-on-first-use until participants compare the room
safety number through a separate trusted channel. Package self-signatures and
participant-home metadata reject inconsistent substitutions, but an actively
malicious authority can still present a new, internally consistent first-seen
identity and join the room as an apparent participant, reading content until
the substitution is detected. Comparing safety numbers is the authentication
step that catches this attack, and it has to be repeated after any membership
or identity change. Before that comparison the room is encrypted, but
participant identities are unverified, and so is the protection against an
active server.

## Account identity and device access

An account has one active portable encryption identity. Registration uses an
Ed25519 proof of possession over a one-use server challenge bound to:

- the local account and current login session;
- the 32-byte device identity public key; and
- the SHA-256 digest of the bounded MLS credential.

The server stores public credentials, one-use MLS KeyPackages, and a bounded
AES-GCM ciphertext holding the portable MLS state. The AES key for that
ciphertext is derived on the client from the account password and is never sent
to the server. Password KDF protocol version 2 prepends the following
client-controlled UTF-8 context to each 16-byte server salt before
PBKDF2-SHA256 (600,000 iterations):

`kaede-password-kdf-v2\0{auth|vault}\0{canonical locally selected home domain}\0`

The home domain must come from browser navigation or the locally configured
native client, never from a KDF response. That binds a captured authentication
secret to one home and keeps the authentication and vault keys distinct even if
a malicious server repeats or relays salts. Authentication receives only the
`auth` output; the non-extractable AES-GCM `vault` key stays on the client.
Canonical deployments must redirect alternate hostnames before login so every
client derives against the account's advertised home domain.

Version 2 is mandatory. Clients and servers reject a missing, unknown, or
legacy KDF version and never send a literal account password as a
compatibility fallback. A legacy credential can only be replaced through
password recovery; there is no same-password login upgrade, because that would
disclose both the password and the vault salt to the home instance. The server
never receives an identity private key, MLS group secret, recovery secret,
attachment key, or LiveKit frame-encryption key.

Vault mutations acquire a short per-account lease and use monotonic revision
compare-and-swap. This is a security requirement, not a convenience: two
clients may not advance the same MLS state concurrently. The lease protects
only ordering and never contains a key or plaintext. Explicit logout and
expired authentication clear local vault keys and MLS state; another signed-in
client can download and decrypt the latest ciphertext again.

Vault format 2 also encrypts a monotonic sequence and its parent-chain
commitment inside the portable state, and binds that same sequence into the
AES-GCM authenticated data. The server keeps only an append-only 32-byte digest
for each opaque envelope revision. Clients extend those digests with the
domain-separated chain `R_n = SHA256(R_(n-1) || u64BE(n) || D_n)` and require
the latest decrypted state to authenticate the exact computed `R_(n-1)`. That
detects a higher-numbered stale fork, a lower revision, a changed digest at the
same revision, and a relabeled sequence.

Each native client keeps its last confirmed revision, digest, and chain root in
platform-protected storage under a hashed account label. This compact
checkpoint holds no secrets, and it survives logout, expired authentication,
and ordinary local MLS-state deletion on purpose; secret vault keys, MLS state,
and plaintext caches do not. A brand-new client has no prior checkpoint, so it
treats its first complete chain as trust-on-first-use. Comparing safety
numbers, or restoring from an already trusted client, remains the defense
against a malicious account home on first contact.

Only a successful authenticated password/E2EE reset may clear the checkpoint.
Recovery import verifies the backup locally first, then performs that
authenticated reset, and only then reseals the trusted recovered state at
sequence 1 with the zero parent.

An E2EE reset also creates a five-minute, one-time recovery authorization whose
raw value is returned only to the initiating login session; the server stores
only its hash. Device enrollment consumes the bearer, but the durable session
fence remains until both the replacement identity and its revision-one vault
are committed. Either transaction may finish first, so a crash between them
can't let another signed-in session publish an old identity or repopulate an
old vault. Until both artifacts exist, other sessions can't acquire or write
the opaque account vault or supersede the reset. The initiating session may
repeat a response-lost reset, and an expired fence may be replaced by another
explicit authenticated reset.

The synchronized vault keeps at most 2,000 recently decrypted messages and an
8 MiB serialized plaintext-cache budget, whichever limit is hit first. That
leaves fixed headroom for MLS state and recovery journals inside the 32 MiB
vault limit. Ciphertext stays in the conversation, but plaintext that has aged
out of every trusted client and recovery backup may no longer be recoverable on
a newly signed-in client. A future chunked encrypted-history store could remove
this bounded-cache tradeoff without exposing content to the server.

Rust and native-client bindings zero mutable secret and plaintext buffers when
their ownership ends, and browser callers clear mutable typed arrays. Browser
JavaScript engines can still create immutable strings or garbage-collected
internal copies that application code can't reliably erase. Kaede bounds their
lifetime and never persists or logs them, but it doesn't claim forensic erasure
from a compromised endpoint's RAM. The endpoint and malicious-client
limitations in the threat model still apply.

Login sessions stay independently revocable. Rotating the shared encryption
identity is an explicit destructive operation: it revokes all prior identity
records, pauses affected rooms for rekey, and abandons any encrypted history
that isn't in the synchronized vault or a recovery backup. Identity-list
generations are monotonic and travel with federated profiles; an older profile
can never roll the generation backward.

KeyPackage uploads are signed by the device identity and bind the device,
suite, expiry, order, and digest of every package. Claims must be atomic and
one-use. The proof establishes possession of the package's embedded key; it
doesn't by itself establish who owns a first-seen key.

## Room policy

Each channel carries a monotonic `encryption_policy_generation` and explicit
state:

`plaintext -> proposed -> activating -> active <-> rekeying`

`failed` is terminal for that proposal. `legacy` identifies pre-protocol opaque
transport and must never be described as MLS E2EE.

Once a generation becomes active, it can't return to plaintext. A user who
wants an unencrypted conversation creates a new room. Every participating home
must validate the exact mode, state, generation, protocol, suite, group ID, and
epoch. Missing capability reduces availability; it never causes downgrade.

Messages record the immutable policy generation and epoch under which the body
was admitted, which preserves honest pre-activation history. The database
itself rejects encrypted bodies in plaintext rooms, plaintext bodies in
encrypted rooms, and stale generation/epoch writes. Deletes remain possible for
older history. Application checks cover attachments and return stable client
errors.

Membership changes are MLS control operations. Removing a member or rotating an
account identity pauses new application messages in `rekeying` until an
authority-ordered commit excludes it. A new member receives future history by
default. Plaintext system notices are never inserted into an encrypted room.

Threads are independent MLS rooms; a forum parent stores policy only and never
shares a content key with its posts. Thread titles, tags, archive/lock state,
membership counts, authorship, and other routing metadata remain plaintext.
Activating an ordinary thread follows the same irreversible, future-only flow
as any other channel.

An E2EE-required forum applies that flow to every new post without a plaintext
starter exception. Post creation commits only a child-thread shell and a
durable, nonce-bound starter reservation. The reservation is bound to the
creating human, or to the exact application worker/webhook automation device
and installation lineage. The client activates the child MLS room and then
atomically claims the reservation with exactly one rich-v2 encrypted starter;
that starter reuses the thread snowflake. Other sends, a different claimant,
and a different retry body fail closed. Activation failure leaves only the
metadata shell and a visible retry action, never server-readable post content.

Creating a thread from an existing message in an active E2EE parent likewise
creates an independent required-E2EE child shell. The child keeps Discord's
source-message snowflake and reference, but the source body remains in the
parent MLS group and is never copied or decrypted by the server. After child
activation, replies use the child group. Native `/thread` may also create an
empty child shell, but it cannot submit a plaintext or parent-group starter as
part of that operation.

Bots receive thread/forum metadata without cryptographic admission. A
participant-mode bot may receive encrypted child-room content only through a
verified worker-owned MLS device with an active, administrator-approved
participation row and future-only history floor. E2EE-required replies and
writes to an active child fail closed while that admission or its rekey is
incomplete.

## Message envelope

The version-1 JSON object remains legacy opaque transport. The MLS application
envelope uses version 2 and binds, in authenticated data:

- protocol and cipher suite;
- canonical room reference and MLS group ID;
- sender device (the MLS BasicCredential independently binds the author account);
- policy generation and MLS epoch;
- operation (`create`, `edit`, or a control operation), message revision, and
  edit target when applicable;
- exact author, application/installation lineage, reply reference, delivery
  flags, and resolved mention-routing projection;
- encrypted attachment-manifest, sticker, and custom-emoji references;
- the sanitized interaction-routing contract digest for components and opaque
  poll answer identities; and
- canonical rich-body and forward-projection digests.

The encrypted rich body carries content, embeds, Components V1/V2, polls,
stickers, TTS/voice markers, attachment manifests, flags, allowed-mention
intent, and an optional author-free forward snapshot. Authorities retain only
the public authenticated routing projection: attachment/expression references,
resolved mention recipients, component callback metadata, opaque poll answer
identities and counts, expiry, application attribution, and revisions. They do
not receive poll labels, questions, message text, embed text, or component
display text.

Servers validate the bounded public context and relay the MLS wire message as
opaque bytes. Clients reject any mismatch before attempting decryption.

Poll votes and finalization use only deterministic answer IDs, counts, voter
state, and expiry at the authority. Manual or scheduled finalization also
creates the idempotent Discord-style type-46 `POLL_RESULT` message. Its public
`poll_result` projection is label-free and binds the source poll reference,
source encryption mode, exact answer counts, total selections, and optional
unique victor ID/count. For an encrypted poll, clients show the question and
winning answer label only after decrypting and verifying the referenced source
poll locally; a missing or unverifiable source renders those labels unavailable.
Conversation authorities relay vote/finalization and type-46 state to every DM
participant home without exposing question, answer, or emoji text.

### Secure forwarding

Forwarding never asks an authority to decrypt an MLS message and never copies
MLS ciphertext between rooms. A human first calls
`POST /api/v1/channels/{source}/messages/{message}/forward/prepare`; a bot calls
`POST /api/v1/bots/channels/{source}/messages/{message}/forward-authorize`.
The source authority rechecks the requester's live channel installation or
membership, message-history/content and attachment grants, and (for an E2EE
source) the participant device and history floor. It returns a short-lived,
signed authorization bound to the requester, application/device when present,
source message and channel, destination channel and encryption mode, client
nonce, source type/flags/timestamps, age context, attachment references, and
the source rich-v2 forward-projection digest. It also carries only the canonical
sticker items and custom-emoji routing tokens already approved by the source
authority, so a remote destination can recheck its external-expression
permissions without seeing message text. An encrypted-source proof contains no
decrypted body.

The client builds the author-free, depth-one snapshot and verifies its canonical
digest against that proof. Plaintext-to-E2EE and E2EE-to-E2EE forwards encrypt a
fresh rich-v2 snapshot for the destination MLS group. E2EE-to-plaintext requires
an explicit disclosure confirmation and sends the disclosed snapshot only to
the plaintext destination. Attachments are downloaded only with the source
grant, then uploaded again for the destination; the authenticated
`plaintext_sha256`, filename, MIME type, size, and voice metadata must remain
identical while attachment IDs, domains, file IDs, keys, and ciphertext hashes
are rebound. Existing nested snapshots are rebound the same way, so prior-room
keys and URLs never leak.

The destination verifies the source signature and every proof binding, current
destination grants, the exact snapshot metadata, attachment count/integrity,
and the destination MLS AAD before admission. Cross-authority requests use the
same contract; neither a replica nor an application home may compose grants
from different installations. A queued federated write must have been signed
while the 90-second proof was live; retries validate against that signed outer
event time and the nonce remains idempotent. Legacy encrypted envelopes without
the v2 commitment fail closed. Discord-nonforwardable polls, calls, activities,
unsupported message types, and snapshots deeper than one remain rejected.

Announcement following is different from an explicit user-mediated forward:
publication is an unattended fan-out into independently keyed rooms. Kaede
therefore rejects E2EE activation for announcement channels and active follower
targets, and rejects encrypted follow creation. A future encrypted announcement
bridge would have to be an explicitly consented MLS participant in every source
and destination room and re-encrypt each publication; ciphertext is never
copied and plaintext fallback is forbidden.

## Reports and voluntary disclosure

Reporting never uploads a room key. If a reporter chooses to share an encrypted
message, the client sends only the selected decrypted text and minimal message
context, after explicit confirmation. The server stores:

- the reporter-supplied plaintext;
- a SHA-256 fingerprint of the exact stored ciphertext envelope; and
- `server_verified=false` disclosure metadata.

Moderators must treat the evidence as user-supplied; non-repudiation must not
be claimed. Reporting other encrypted messages requires a separate explicit
selection, and surrounding history is never silently included.

## Files and previews

Web, packaged desktop, Android, and iOS clients implement `e2ee-media/1`. If
local cryptography is unavailable, attachment sends in an encrypted room fail
closed; there is no fallback plaintext upload.

Encrypted files use a versioned, chunked AEAD format. The true filename, MIME
type, dimensions, duration, plaintext hashes, and preview relationships live in
the encrypted manifest. Server-visible metadata is limited to opaque blob IDs,
ciphertext sizes and digest, protocol version, and availability state. Sender
devices don't upload plaintext thumbnails or posters. Current clients show an
explicit download action; any future preview implementation must encrypt
preview bytes separately and describe their relationship only inside the
manifest.

Servers authorize, quota, store, federate, cache, and delete ciphertext. They
don't MIME-sniff, decode, derive previews, or claim to malware-scan it. Clients
say "Encrypted — not scanned by Kaede" and warn before opening risky files.
Automatic server link previews are disabled for decrypted message text; a
future manual preview action must disclose that it reveals the URL to the
instance.

## Voice and video

LiveKit media E2EE follows the same channel-scoped E2EE policy as messages and
files; it isn't a separate per-call choice or a downgrade path. Encrypted text,
announcement, and DM channels protect their new messages, their files, and any
supported call associated with the channel. Encrypted voice channels protect
microphone, camera, screen video, and screen audio at the endpoints. Media E2EE
doesn't hide signaling, participants, timing, track types, or traffic metadata.

Every call binds a monotonic media-policy generation, a fresh media session ID,
a crypto epoch, the protocol, and the suite in the authority response,
federation state, token metadata, and client state. These fields are
downgrade-sensitive. Keys never appear in LiveKit JWTs, SQL, Redis, federation
JSON, logs, or telemetry. The media session ID binds the channel, the concrete
LiveKit room, the MLS group and generation, the epoch, the protocol, and the
suite.

Authorities require a non-revoked, proof-of-possession device before minting an
encrypted grant, and remote homes sign that device attestation. Admission
webhooks compare every field against current room policy, so a grant minted
before activation, a rekey, or a membership removal is rejected. Activation,
rekey, and encrypted group membership changes close the old LiveKit room before
publishing the new epoch.

Clients install authenticated device sender keys and enable frame encryption
before connecting or publishing. A missing key, an old client, an encryption
failure, or a mixed-mode move blocks media instead of falling back. Users must
join encrypted voice themselves; a moderator can't force-move a client without
its device-bound media setup.

Every client requires an explicit encryption-mode bit and compares it in both
directions with the selected channel policy before connecting or opening media
capture. Packaged desktop clients carry the complete validated policy across
the UI/native boundary and compare it with the independently fetched native
grant. The connected-session indicator is derived from that validated policy,
not from channel metadata alone. A move across plaintext/E2EE policy
boundaries disconnects and requires a fresh, user-initiated join.

## Product disclosures

The full activation confirmation spells out what changes:

- Protection applies only to future content; old history remains readable to
  servers.
- Server search, automatic previews, traditional webhooks, and bots that are
  not explicitly admitted as verified MLS participants stop working, and
  notifications may become generic.
- Encrypted files are not server-scanned.
- Losing the synchronized vault, all trusted local state, and the recovery
  backup loses history, and unsupported clients can't use the room.
- Metadata stays visible, and removed members keep content they already
  received.
- The encrypted roster is trust-on-first-use until participants compare the
  safety number through a separate trusted channel, and the comparison must be
  repeated after roster or identity changes.

Recovery settings also disclose that synchronized decrypted history is bounded
to the newest 2,000 messages or 8 MiB, so older plaintext must remain on a
trusted client or in a recovery backup.

After activation, clients use a persistent status affordance and contextual
explanations on disabled features. The full warning is repeated only for
activation, encrypted-room join, recovery, and integration changes.
"Encrypted" and "identity verified" are separate states.

## Release gate

`KAEDE_E2EE_ACTIVATION_ENABLED=true` is the default. Operators deploying
clients that haven't passed the applicable release gates must set it to
`false`, which hides new activation and makes the API reject new proposals:

1. External review of the protocol, bindings, recovery, and metadata policy.
2. Cross-language RFC vectors for web, desktop, and mobile.
3. Three-home ordering tests for proposals, commits, welcomes, application
   messages, history, removal, revocation, and replay.
4. Ingress-matrix tests for every API, webhook, bot, federation, replica, and
   history path plus the database admission guard.
5. At-rest and forensic tests proving keys and plaintext do not enter server
   logs, databases, caches, crash reports, or native ciphertext caches, plus
   best-effort lifetime/zeroization tests for mutable client buffers. These
   tests must account for the browser-engine erasure limitation above rather
   than claiming universal RAM erasure.
6. Encrypted file tamper/chunk/manifest tests and cross-client LiveKit media
   tests, including packet loss and key rotation.

The repository's deterministic tests cover three-home authority binding,
membership and device-change fanout, durable MLS control catch-up and history
boundaries, retry after an unreachable room authority, three-member MLS
exporter rotation and removal, and stale media-grant rejection in web and
mobile clients. They don't emulate a real LiveKit SFU or inspect the frame
counters maintained inside LiveKit's web worker and native SDKs.

Before media E2EE is released, the remaining external gate must run web,
desktop, Android, and iOS participants against a real SFU while exercising
microphone, camera, screen video, and screen audio under loss, duplication,
reordering, reconnect, and epoch rotation. It must confirm that stale or
replayed frames fail closed and that no client silently downgrades to
plaintext.

The deployment kill switch prevents only new proposals. Rekey, recovery,
reporting with explicit disclosure, and ordinary encrypted-room operation stay
available, and the switch can never authorize plaintext in an encrypted room.
