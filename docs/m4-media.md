# Media and webhooks

This document describes the private media lifecycle, federated media cache,
browser upload experience, and incoming webhook surface. Durable wire behavior
is normative in [kaede-fed-v1.md](kaede-fed-v1.md).

## Storage and delivery

- Three private S3-compatible buckets (`kaede-attachments`, `kaede-derived`, and
  `kaede-remote-cache`) are used. Garage remains the self-hosted default and
  creates them idempotently; external providers can use pre-created buckets,
  configurable SigV4 regions, path/virtual addressing, and session credentials.
  Browser credentials are never issued; uploads and downloads use narrowly
  scoped SigV4 URLs.
- Channel upload tickets enforce `ATTACH_FILES`, declared MIME and size bounds,
  per-user ticket count, pending-byte quota, and total storage quota under a
  locked accounting row. PUT signatures bind the exact `Content-Length` and
  `Content-Type`, and expire after 15 minutes. Message creation finalizes only
  an owned, unexpired, single-use ticket whose object-store `HEAD` length
  matches.
- Taskiq processing sniffs magic bytes, rejects executable and active content,
  scans through ClamAV INSTREAM, and never exposes an original until it is
  clean. Browser `PUT` credentials address staging-only keys. After validation,
  the worker writes the exact in-memory bytes that passed scanning to a
  content-addressed clean key that has never been disclosed to the browser, and
  atomically switches the database reference to that key. pyvips emits
  animated-preserving WebP variants at 128, 512, and 1024 pixels plus blurhash
  and perceptual hash metadata. FFmpeg produces bounded WebP posters for
  supported videos.
- Avatar, banner, guild icon/banner, and emoji ticket/commit paths use the same
  scan gate. Public assets are content-addressed and return a cacheable redirect
  whose lifetime is shorter than its immutable seven-day S3 target.
- A guild may own between one and 1,000 custom emoji according to the operator's
  configured limit (100 by default). Names are unique within a guild and emoji
  images have an independent 512 KiB default upload limit. Membership grants use
  of that collection; posting it in another guild additionally requires the
  destination channel's `USE_EXTERNAL_EMOJIS` permission. Messages store a
  domain-qualified emoji identity so equal names and snowflakes from different
  instances remain unambiguous. Guild snapshots and sequenced create/delete
  events replicate emoji metadata while immutable bytes remain at their origin.
- Message media authorization rechecks channel membership and
  `VIEW_CHANNEL`/`READ_MESSAGE_HISTORY`, then issues a private 24-hour target
  behind a five-minute redirect. Message deletion asynchronously removes the
  original and all derivatives and releases the uploader's quota.
- Remote attachment identities remain `(origin_domain, attachment_id, variant)`.
  Fetches use a fixed signed `/_kaede/v1/media/...` path through the shared
  DNS-pinning SSRF guard, reject redirects and oversized responses, re-sniff and
  re-scan bytes locally, and cache them in the selected store with a 100 GiB LRU
  ceiling and 30-day TTL by default. Cache hits bypass fetch admission; misses
  use a per-user Dragonfly bucket and one of eight process-local fetch/scan/store
  permits so distinct uncached objects cannot exhaust API memory or outbound
  capacity. Authenticated `media.delete` envelopes purge all cached variants.
- Orphan tickets, optional unattached-media retention, and remote-cache eviction
  are scheduled, bounded, `SKIP LOCKED` jobs. Storage failure never silently
  converts an unavailable object into a successful deletion.
- The Paper Lantern composers accept file selection, drag/drop, and clipboard
  files, show per-file upload progress, and support attachment-only messages.
  Settings expose avatar and banner upload/scan/commit behavior.
- Incoming guild webhooks support create/list/update, secret rotation, revocation,
  path-token and `Authorization: Bearer` execution, and a five-per-two-second
  bucket. Only a SHA-256 token digest is stored. Webhook messages carry durable,
  explicit attribution and never render as an ordinary message from the creator,
  including on federated guild replicas.

## Security invariants

Every bucket is private and is reachable only with a valid signature. Garage
uses the dedicated media virtual host; external providers use their configured
HTTPS origin. The signed upload length prevents a client from declaring a tiny
ticket and temporarily storing a much larger body. File
extensions, browser MIME values, remote scan claims, remote content types, and
event-provided URLs are never trusted. A still-valid upload credential can
rewrite only its abandoned staging key, never the clean object served by Kaede.
The cleanup sweep retains the staging-key reference until the presigned `PUT`
has expired and then retries deletion, covering a client that rewrites staging
after the worker's best-effort immediate deletion.

Federation media authorization discloses no attachment existence to a peer that
has no participating DM user or guild member with channel visibility. Cache
tombstones can remove only objects whose origin equals the authenticated and
envelope-signing origin. Webhook tokens are returned only at creation/rotation,
redacted by the edge log policy, compared in constant time, and become unusable
immediately after rotation or revocation.

## Validation

`make media-check` creates a unique disposable Compose project with PostgreSQL,
Dragonfly, Garage, and ClamAV on internal networks and publishes no host ports.
It verifies:

- idempotent bucket initialization and real Garage SigV4 upload/download;
- exact-length signing and message-time `HEAD` enforcement;
- denial before scan, clean PNG derivatives/metadata, and authenticated serving;
- local EICAR rejection and original removal;
- incoming webhook attribution, token rotation, old-token rejection, and
  revocation; and
- message deletion plus object unavailability after purge.

The gate removes containers, networks, and volumes on either success or failure.
Unit tests additionally cover signing vectors, MIME/magic rejection, safe
filenames, attachment-only payload validation, animated derivative preservation,
and token digest behavior. `make migration-check` covers the media migrations,
downgrade, re-upgrade, schema assertions, and metadata drift.

## Related features

LiveKit voice, video, screen sharing, occupancy, moderation enforcement,
cross-instance token brokering, and DM call signaling are documented in
[m5-voice.md](m5-voice.md). Release controls and observability are documented in
[m6-hardening-release.md](m6-hardening-release.md). Presence, typing, and live
occupancy remain ephemeral.
