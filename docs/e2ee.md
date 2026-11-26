# End-to-end encryption protocol and rollout

This document is the security contract for Kaede E2EE. A UI lock or an opaque
`Message.e2ee` object doesn't by itself show that this contract is active.

[Identity](#account-identity-and-device-access) · [Room policy](#room-policy) ·
[Messages](#message-envelope) · [Files](#files-and-previews) ·
[Calls](#voice-and-video) · [Release checks](#release-gate)

## Scope and threat model

MLS 1.0 (RFC 9420) is the room key agreement protocol, and
`MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519` is the mandatory suite. In an
active room, new message text, filenames, attachment plaintext, microphone
audio, camera video, and screen-share media are confidential against
honest-but-curious servers and network attackers. Each approved account
encryption identity is a distinct MLS leaf. A user's signed-in clients unlock
and sync that one identity through a password-encrypted account vault.

The server still learns routing metadata:

- room and sender references, participating users and homes, timestamps,
  ciphertext sizes, delivery and download activity, and voice track kinds;
- mention recipients, reply references, and sticker and custom-emoji
  references;
- reactions and who reacted, and poll answer IDs, counts, and voters.

Padding and traffic-analysis resistance are out of scope. E2EE can't stop a
recipient, a compromised endpoint, or malicious browser code served by an
instance from recording plaintext.

Instance federation signatures authenticate servers and transport. They don't
authenticate user devices and must never stand in for MLS credentials.

Account identities are trust-on-first-use until participants compare the room
safety number over a separate trusted channel. Package self-signatures and
participant-home metadata reject inconsistent substitutions. An actively
malicious authority can still present a new, internally consistent first-seen
identity and join as an apparent participant, reading content until someone
notices. Comparing safety numbers catches this, and it must be repeated after
any membership or identity change. Until then the room is encrypted, but its
participants, and its protection against an active server, are unverified.

## Account identity and device access

An account has one active portable encryption identity. Registration uses an
Ed25519 proof of possession over a one-use server challenge bound to:

- the local account and current login session;
- the 32-byte device identity public key; and
- the SHA-256 digest of the bounded MLS credential.

The server stores public credentials, one-use MLS KeyPackages, and a bounded
AES-GCM ciphertext of the portable MLS state. The client derives that
ciphertext's key from the account password and never sends it. Password KDF
version 2 prepends this client-controlled UTF-8 context to each 16-byte server
salt before PBKDF2-SHA256 (600,000 iterations):

`kaede-password-kdf-v2\0{auth|vault}\0{canonical locally selected home domain}\0`

The home domain comes from browser navigation or the native client's local
configuration, never from a KDF response. This binds a captured authentication
secret to one home, and keeps the authentication and vault keys distinct even
if a malicious server repeats or relays salts. Authentication receives only the
`auth` output. The non-extractable AES-GCM `vault` key stays on the client.
Canonical deployments must redirect alternate hostnames before login so every
client derives against the account's advertised home domain.

Version 2 is mandatory. Clients and servers reject a missing, unknown, or
legacy KDF version and never fall back to sending the literal password. A
legacy credential can only be replaced through password recovery. There's no
same-password login upgrade, because it would disclose both the password and
the vault salt to the home. The server never receives an identity private key,
MLS group secret, recovery secret, attachment key, or LiveKit frame key.

Vault writes take a short per-account lease and use monotonic revision
compare-and-swap. This is a security requirement: two clients must not advance
the same MLS state concurrently. The lease only orders writes and never holds a
key or plaintext. Logout and expired authentication clear local vault keys and
MLS state. Another signed-in client can download and decrypt the latest
ciphertext again.

Vault format 2 encrypts a monotonic sequence and its parent-chain commitment
inside the portable state, and binds the same sequence into the AES-GCM
authenticated data. The server keeps only an append-only 32-byte digest per
envelope revision. Clients extend those digests with the domain-separated chain
`R_n = SHA256(R_(n-1) || u64BE(n) || D_n)` and require the latest decrypted
state to authenticate the exact computed `R_(n-1)`. That detects a
higher-numbered stale fork, a lower revision, a changed digest at the same
revision, and a relabeled sequence.

Each native client stores its last confirmed revision, digest, and chain root
in platform-protected storage under a hashed account label. This checkpoint
holds no secrets. It survives logout, expired authentication, and ordinary
MLS-state deletion; vault keys, MLS state, and plaintext caches don't. A new
client has no checkpoint, so it trusts its first complete chain on first use.
Safety-number comparison, or restoring from an already trusted client, is the
defense against a malicious home at first contact.

Only a successful authenticated password/E2EE reset may clear the checkpoint.
Recovery import verifies the backup locally, performs that reset, and only then
reseals the recovered state at sequence 1 with the zero parent.

An E2EE reset also creates a five-minute, one-time recovery authorization. Its
raw value goes only to the initiating login session, and the server stores only
a hash. Device enrollment consumes it, but the session fence stays until both
the replacement identity and its revision-one vault are committed. Either can
commit first, so a crash in between can't let another session publish an old
identity or restore an old vault. Until both exist, other sessions can't acquire
or write the vault or supersede the reset. The initiating session may repeat a
reset whose response was lost, and an expired fence can be replaced by another
authenticated reset.

The synced vault keeps at most 2,000 recently decrypted messages within an
8 MiB serialized plaintext-cache budget, whichever is hit first. That leaves
headroom for MLS state and recovery journals inside the 32 MiB vault limit.
Ciphertext stays in the conversation, but plaintext that has aged out of every
trusted client and recovery backup may not be recoverable on a new client.

Rust and native bindings zero mutable secret and plaintext buffers when their
ownership ends, and browser code clears mutable typed arrays. Browser engines
can still keep immutable strings or garbage-collected copies that application
code can't erase. Kaede limits their lifetime and never persists or logs them,
but doesn't claim forensic erasure from a compromised endpoint's RAM.

Login sessions are revocable independently. Rotating the shared encryption
identity is destructive: it revokes all earlier identity records, pauses
affected rooms for rekey, and abandons encrypted history that isn't in the
synced vault or a recovery backup. Identity-list generations are monotonic and
travel with federated profiles, and an older profile can't roll them back.

KeyPackage uploads are signed by the device identity and bind the device,
suite, expiry, order, and digest of every package. Claims are atomic and
one-use. The proof shows possession of a package's key, not who owns a
first-seen key.

## Room policy

Each channel carries a monotonic `encryption_policy_generation` and a state:

`plaintext -> proposed -> activating -> active <-> rekeying`

`failed` is terminal for that proposal. `legacy` marks pre-protocol opaque
transport and must never be described as MLS E2EE.

In a 1:1 DM, either participant can request encryption, and the other must
agree after reviewing the warning card. Disagree leaves the conversation
unencrypted. Requests expire after 24 hours. The authority stores the consent,
binds it to the participants and the activation, and checks it again before
activating, federated DMs included. Group DMs and guild channels keep their
existing activation permissions. Every participant's instance must advertise
`e2ee-mls/1`, and activation fails if support is missing or can't be verified.

### Turning encryption off in a DM

This works only for DMs and group DMs where everyone is on the same instance.
Open encryption settings from the conversation header on web, or conversation
settings on mobile, and choose **Request to turn off encryption**. Every
current participant must agree within 24 hours. A membership change cancels
the request, and any active call must end first.

The conversation still appears once in the inbox. A dated divider marks where
server-readable messages begin. Earlier messages stay in their original MLS
room and decrypt on the device. An earlier draft can be moved into the new
composer after confirmation, and queued sends keep their original destination.
Cross-instance conversations stay encrypted until a federation transition
protocol exists.

The authority closes the original room to new messages and creates its
plaintext successor in one transaction. Every participating home validates the
exact mode, state, generation, protocol, suite, group ID, and epoch. A missing
capability reduces availability and never causes a downgrade.

### Admission rules

Messages record the immutable policy generation and epoch they were admitted
under, which keeps honest pre-activation history valid. The database rejects
encrypted bodies in plaintext rooms, plaintext bodies in encrypted rooms, and
stale generation or epoch writes. Application checks cover attachments and
return stable client errors. Older history can still be deleted, except that
messages under or removed by child-safety review can't be edited or deleted,
and channels holding them can't be deleted.

Membership changes are MLS control operations. Removing a member or rotating an
account identity puts the room in `rekeying`, with no new application messages,
until an authority-ordered commit excludes the old leaf. New members get only
future history by default. Plaintext system notices never enter an encrypted
room, with one exception: when staff confirm a child-safety case, the authority
replaces that message with a plaintext removal notice and updated clients purge
their decrypted copies. A malicious authority could abuse this to hide content,
but it gains no plaintext.

An encrypted room supports at most 500 members and 48 enrolled devices. Larger
rooms can't activate or finish a rekey.

### Threads and forums

Threads are independent MLS rooms. A forum parent stores policy only and never
shares a content key with its posts. Thread titles, tags, archive/lock state,
member counts, authorship, and other routing metadata stay plaintext.
Activating an ordinary thread follows the same irreversible, future-only flow
as any channel.

An E2EE-required forum applies that flow to every new post, with no plaintext
starter. Post creation commits only a thread shell and a durable, nonce-bound
starter reservation, bound to the creating human or to the exact worker or
webhook automation device and installation lineage. The client activates the
child MLS room, then atomically claims the reservation with exactly one rich-v2
encrypted starter that reuses the thread snowflake. Other sends, a different
claimant, and a changed retry body fail closed. If activation fails, only the
metadata shell remains, with a retry action.

Creating a thread from a message in an active E2EE parent also creates an
independent required-E2EE child shell. The child keeps the source message's
snowflake and reference, but the source body stays in the parent MLS group and
the server never copies or decrypts it. After activation, replies use the
child group. Native `/thread` may create an empty child shell but can't submit
a plaintext or parent-group starter with it.

Bots receive thread and forum metadata without cryptographic admission. A
participant-mode bot gets encrypted child-room content only through a verified
worker-owned MLS device with an active, administrator-approved participation
row and a future-only history floor. E2EE-required replies and writes to an
active child fail closed until that admission or its rekey completes.

## Message envelope

The version-1 JSON object is legacy opaque transport. The MLS application
envelope is version 2 and binds, in authenticated data:

- protocol and cipher suite;
- canonical room reference and MLS group ID;
- sender device (the MLS BasicCredential separately binds the author account);
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
intent, and an optional author-free forward snapshot. Authorities keep only the
public authenticated routing projection: attachment and expression references,
resolved mention recipients, component callback metadata, opaque poll answer
identities and counts, expiry, application attribution, and revisions. They
never receive poll questions or labels, message text, embed text, or component
display text.

Servers validate the bounded public context and relay the MLS wire message as
opaque bytes. Clients reject any mismatch before decrypting.

Poll votes and finalization use only deterministic answer IDs, counts, voter
state, and expiry at the authority. Finalization, manual or scheduled, creates
the idempotent type-46 `POLL_RESULT` message. Its public `poll_result`
projection has no labels. It binds the source poll reference, source encryption
mode, exact answer counts, total selections, and an optional unique winner
ID and count. For an encrypted poll, clients show the question and winning
label only after decrypting and verifying the source poll locally. If the
source is missing or can't be verified, the labels show as unavailable.
Authorities relay votes, finalization, and type-46 state to every DM
participant home without question, answer, or emoji text.

### Secure forwarding

Forwarding never asks an authority to decrypt an MLS message and never copies
MLS ciphertext between rooms. A human first calls
`POST /api/v1/channels/{source}/messages/{message}/forward/prepare`; a bot calls
`POST /api/v1/bots/channels/{source}/messages/{message}/forward-authorize`.

The source authority rechecks the requester's live membership or channel
installation, history, content and attachment grants, and, for an E2EE source,
the participant device and history floor. It returns a short-lived signed
authorization bound to:

- the requester and any application or device;
- the source message and channel, and the destination channel and its
  encryption mode;
- the client nonce, source type, flags, timestamps, and age context;
- attachment references and the source rich-v2 forward-projection digest;
- the canonical sticker items and custom-emoji routing tokens the source
  already approved, so a remote destination can recheck expression permissions
  without seeing text.

An encrypted-source proof contains no decrypted body.

The client builds the author-free, depth-one snapshot and checks its digest
against the proof. Forwards into an E2EE room encrypt a fresh rich-v2 snapshot
for the destination group. E2EE-to-plaintext requires a disclosure
confirmation and sends the snapshot only to that destination. Attachments are
downloaded with the source grant and uploaded again for the destination. The
authenticated `plaintext_sha256`, filename, MIME type, size, and voice metadata
must stay identical, while attachment IDs, domains, file IDs, keys, and
ciphertext hashes are rebound. Nested snapshots are rebound the same way, so
earlier rooms' keys and URLs never leak.

Before admission, the destination verifies the source signature, every proof
binding, current destination grants, the snapshot metadata, attachment count
and integrity, and the destination MLS AAD. Cross-authority requests follow the
same contract, and neither a replica nor an application home may combine grants
from different installations. A queued federated write must have been signed
while the 90-second proof was live. Retries validate against that signed event
time, and the nonce stays idempotent. Legacy envelopes without the v2
commitment fail closed. Polls, calls, activities, unsupported message types,
and snapshots deeper than one can't be forwarded.

Announcement following is an unattended fan-out into separately keyed rooms,
not a user-mediated forward. Kaede rejects E2EE activation for announcement
channels and active follower targets, and rejects encrypted follow creation. A
future encrypted bridge would have to be a consented MLS participant in every
source and destination room and re-encrypt each publication. Copying
ciphertext and falling back to plaintext are both forbidden.

## Reports and voluntary disclosure

Reporting never uploads a room key. If a reporter chooses to share an encrypted
message, the client sends the selected decrypted text after confirmation. The
reporter can add up to 20 adjacent messages they confirm, and a decrypted copy
of a selected attachment with its filename. These go to the room's moderation
authority, which may be another instance. A child-safety (`csam`) report is
the exception: it stays with the reporter's home and is never forwarded. The
server stores:

- the reporter-supplied plaintext;
- a SHA-256 fingerprint of the exact stored ciphertext envelope; and
- `server_verified=false` disclosure metadata.

Moderators must treat this as user-supplied evidence and must not claim
non-repudiation. Other encrypted messages need their own selection, and
surrounding history is included only if the reporter opts in.

A disclosed attachment is uploaded as plaintext, so it gets the malware check
and PhotoDNA when enabled, and is stored as reporter-supplied evidence. For a
child-safety report, the authority also seals the message snapshot and any
held attachment bytes, ciphertext included, in an encrypted case. Those records
stay until staff close the case and retention ends, at least one year after a
confirmed case's external submission.

## Files and previews

Web, packaged desktop, Android, and iOS implement `e2ee-media/1`. If local
cryptography is unavailable, attachment sends in an encrypted room fail
closed. There's no plaintext fallback.

Encrypted files use a versioned, chunked AEAD format. The real filename, MIME
type, dimensions, duration, plaintext hashes, and preview relationships live in
the encrypted manifest. The server sees only opaque blob IDs, ciphertext sizes
and digest, protocol version, and availability state. Senders don't upload
plaintext thumbnails or posters. Files usually show a download action. Voice
messages play in the client, and ephemeral media is decrypted locally for
display. A future preview must encrypt its bytes separately and describe the
relationship only inside the manifest.

Servers authorize, quota, store, federate, cache, and delete ciphertext. They
don't MIME-sniff, decode, derive previews, or claim to malware-scan it. Clients
don't yet label encrypted files as unscanned or warn before opening risky
ones, so only open attachments from sources you trust. Automatic link previews
are off for decrypted text. A future manual preview must disclose that it
reveals the URL to the instance.

## Voice and video

LiveKit media E2EE follows the channel's E2EE policy. It isn't a per-call
choice or a downgrade path. Encrypted text, thread, and DM channels protect
their new messages, files, and any supported call tied to the channel. Encrypted
voice channels protect microphone, camera, screen video, and screen audio at
the endpoints. Signaling, participants, timing, track types, and traffic
metadata stay visible.

Every call binds a monotonic media-policy generation, a fresh media session ID,
a crypto epoch, the protocol, and the suite in the authority response,
federation state, token metadata, and client state. These fields are
downgrade-sensitive. The media session ID binds the channel, the concrete
LiveKit room, the MLS group and generation, the epoch, the protocol, and the
suite. Keys never appear in LiveKit JWTs, SQL, Redis, federation JSON, logs, or
telemetry.

Authorities require a non-revoked, proof-of-possession device before minting an
encrypted grant, and remote homes sign that device attestation. Admission
webhooks compare every field against current room policy, so a grant minted
before activation, a rekey, or a removal is rejected. Activation, rekey, and
encrypted membership changes close the old LiveKit room before publishing the
new epoch.

Clients install authenticated device sender keys and enable frame encryption
before connecting or publishing. A missing key, an old client, an encryption
failure, or a mixed-mode move blocks media rather than falling back. Users must
join encrypted voice themselves, and a moderator can't force-move a client
without its device-bound media setup.

Every client requires an explicit encryption-mode bit and checks it both ways
against the channel policy before connecting or opening capture. Packaged
desktop clients pass the full validated policy across the UI/native boundary
and compare it with the separately fetched native grant. The connected-session
indicator comes from that validated policy, not from channel metadata alone. A
move across the plaintext/E2EE boundary disconnects and requires a fresh,
user-initiated join.

## Product disclosures

In encrypted DMs and group DMs, open search in the conversation to find text in
messages loaded on the device. Loading earlier messages widens the search.
Search terms and results stay on the device.

The activation confirmation spells out what changes:

- Protection covers only future content; old history stays readable to
  servers.
- Server search, automatic previews, traditional webhooks, and bots that
  aren't admitted as verified MLS participants stop working, and notifications
  may become generic.
- Encrypted files aren't server-scanned.
- Losing the synced vault, all trusted local state, and the recovery backup
  loses history, and unsupported clients can't use the room.
- Metadata stays visible, and removed members keep what they already received.
- The roster is trust-on-first-use until participants compare the safety
  number over a separate trusted channel, again after roster or identity
  changes.

Recovery settings also say that synced decrypted history is limited to the
newest 2,000 messages or 8 MiB, so older plaintext must stay on a trusted
client or in a recovery backup.

After activation, clients show a persistent status indicator and explain
disabled features in context. The full warning repeats only for activation,
joining an encrypted room, recovery, and integration changes. "Encrypted" and
"identity verified" are separate states.

## Release gate

`KAEDE_E2EE_ACTIVATION_ENABLED=true` is the default. If you deploy clients
that haven't passed the applicable gates below, set it to `false`. That hides
new activation and makes the API reject new proposals.

1. External review of the protocol, bindings, recovery, and metadata policy.
2. Cross-language RFC vectors for web, desktop, and mobile.
3. Three-home ordering tests for proposals, commits, welcomes, application
   messages, history, removal, revocation, and replay.
4. Ingress-matrix tests for every API, webhook, bot, federation, replica, and
   history path, plus the database admission guard.
5. At-rest and forensic tests proving keys and plaintext stay out of server
   logs, databases, caches, crash reports, and native ciphertext caches, plus
   best-effort lifetime and zeroization tests for mutable client buffers that
   account for the browser-engine limit above.
6. Encrypted file tamper, chunk, and manifest tests, and cross-client LiveKit
   media tests including packet loss and key rotation.

The repository's deterministic tests cover three-home authority binding,
membership and device-change fanout, durable MLS control catch-up and history
boundaries, retry after an unreachable authority, three-member MLS exporter
rotation and removal, and stale media-grant rejection on web and mobile. They
don't emulate a real LiveKit SFU or inspect the frame counters inside
LiveKit's web worker and native SDKs.

Before relying on media E2EE in a release, run web, desktop, Android, and iOS
participants against a real SFU. Exercise microphone, camera, screen video,
and screen audio under loss, duplication, reordering, reconnect, and epoch
rotation. Confirm that stale or replayed frames fail closed and that no client
downgrades to plaintext.

The kill switch blocks only new proposals. Rekey, recovery, reporting with
disclosure, and normal encrypted-room use keep working, and the switch can
never allow plaintext in an encrypted room.
