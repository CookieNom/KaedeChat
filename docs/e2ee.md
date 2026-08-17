# End-to-end encryption protocol and rollout

This document is the security contract for Kaede E2EE. A UI lock or an opaque
`Message.e2ee` object is not, by itself, evidence that this contract is active.

## Scope and threat model

When a room is active, Kaede servers and federated homes must not learn new
message text, filenames, attachment plaintext, microphone audio, camera video,
or screen-share media. MLS 1.0 (RFC 9420) is the room key agreement protocol.
Every approved device is a distinct MLS leaf. The mandatory suite is
`MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519`.

The server still learns routing metadata: room and sender references,
participating users and homes, timestamps, ciphertext sizes, delivery and
download activity, and voice track kinds. E2EE cannot prevent a recipient,
compromised endpoint, or malicious browser code delivered by an instance from
recording plaintext.

Instance federation signatures authenticate servers and transport. They do not
authenticate user devices and must never substitute for MLS credentials.

## Device identity

An encryption device is separately revocable from a login session. Registration
uses an Ed25519 proof of possession over a one-use server challenge bound to:

- the local account and current login session;
- the 32-byte device identity public key; and
- the SHA-256 digest of the bounded MLS credential.

The server stores public credentials and one-use MLS KeyPackages only. It never
receives an identity private key, MLS group secret, recovery secret, attachment
key, or LiveKit frame-encryption key. A revoked identity key cannot be
re-registered. Device-list generations are monotonic and are propagated with
federated profiles; an older profile can never roll the generation backward.

KeyPackage uploads are signed by the device identity and bind the device, suite,
expiry, order, and digest of every package. Claims must be atomic and one-use.

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

Membership changes are MLS control operations. Removing a member or revoking a
device pauses new application messages in `rekeying` until an authority-ordered
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

LiveKit media E2EE is a separate opt-in policy from message E2EE. It covers mic,
camera, screen video, and screen audio at endpoints; it does not hide signaling,
participants, timing, track types, or traffic metadata.

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
generic; encrypted files are not server-scanned; losing every device and the
recovery key loses history; unsupported clients cannot use the room; metadata
remains visible; and removed members retain content already received.

After activation, clients use a persistent status affordance and contextual
explanations on disabled features. Repeat the full warning only for activation,
encrypted-room join, recovery, and integration changes. “Encrypted” and
“identity verified” are separate states.

## Release gate

`KAEDE_E2EE_ACTIVATION_ENABLED=false` is the default. New activation stays
hidden and the API rejects new proposals until an operator explicitly enables
it after the applicable release gates pass:

1. External review of the protocol, bindings, recovery, and metadata policy.
2. Cross-language RFC vectors for web, desktop, and mobile.
3. Three-home ordering tests for proposals, commits, welcomes, application
   messages, history, removal, revocation, and replay.
4. Ingress-matrix tests for every API, webhook, bot, federation, replica, and
   history path plus the database admission guard.
5. At-rest and forensic tests proving keys/plaintext do not enter server logs,
   databases, caches, crash reports, or native ciphertext caches.
6. Encrypted file tamper/chunk/manifest tests and cross-client LiveKit media
   tests, including packet loss and key rotation.

The deployment kill switch prevents only new proposals. Rekey, recovery,
reporting with explicit disclosure, and ordinary encrypted-room operation stay
available, and the switch can never authorize plaintext in an encrypted room.
