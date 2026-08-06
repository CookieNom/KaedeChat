# Voice, video, screen sharing, and calls

Updated: 2026-07-20

## Architecture

Kaede uses LiveKit-backed guild voice channels and two-party DM calls without adding
ephemeral media state to PostgreSQL. The guild home owns
`g.<guild_id>.<channel_id>`; the caller's instance owns
`d.<channel_id>.<call_id>`. Room and participant identity construction is
centralized, canonical, and covered by tests. Participant identities are the
immutable numeric user reference plus home domain (`<user_id>@<domain>`).

`POST /api/v1/channels/{channel}/voice/token` is the only guild join flow. A
replica checks its cached `CONNECT` permission before sending a signed
`POST /_kaede/v1/voice/token`; the home repeats the membership and channel
permission calculation against authoritative SQL. Tokens expire after at most
15 minutes, name one room and identity, never carry room-administration or data
publication grants, and map `SPEAK` to microphone while `STREAM` independently
maps to camera, screen video, and screen audio. Server deaf disables subscription.
The browser receives only the public LiveKit WebSocket URL and token.

Each `(room, identity)` has a Dragonfly generation. The generation is embedded
in signed participant metadata. LiveKit's signed webhook is checked against the
exact raw-body SHA-256 before parsing, duplicate webhook IDs are suppressed, and
a stale or malformed join is removed. A Redis-leased coordinator re-evaluates
every connected guild participant from SQL every five seconds. It bumps the
generation and updates or removes the live participant whenever `CONNECT`,
`SPEAK`, `STREAM`, timeout, membership, role, overwrite, or persisted voice
moderation state changes. This deliberately secures future mutation paths without
requiring each path to remember a voice callback. LiveKit participants are also
reconciled every 60 seconds to repair missed webhooks.

Server mute and deafen are persisted only in `guild_members.voice_flags` and are
reapplied to every mint. Moderators with `MUTE_MEMBERS`, `DEAFEN_MEMBERS`, or
`MOVE_MEMBERS` can change those flags or disconnect a participant. Gateway opcode
4 accepts exactly `{self_mute, self_deaf}` booleans and cannot choose or move a
channel. Voice state dispatch is ephemeral and never consumes the resumable
gateway stream.

Authoritative home occupancy is held in Dragonfly. Every 30 seconds the elected
coordinator sends a signed snapshot to domains with members in that guild.
Receivers atomically reject an older `generated_at`, mark occupancy stale after
the configured threshold (75 seconds by default), and render stale state as
unknown instead of current. S2S loss does not disconnect existing LiveKit media,
but a new remote join fails with `VOICE_HOME_UNREACHABLE` and a bounded retry
hint.

DM calls use TTL-bounded Dragonfly records and atomic Lua transitions. Signed
`POST /_kaede/v1/calls` requests bind the call, channel, authority, actor, and
creation time; actor ownership and current DM participation are rechecked at the
receiver. Clock-skew validation applies to call creation only. Later actions must
match the stored channel and creation time, so a long-running call remains usable
without allowing callers to substitute stale context. The initiator is accepted
when the call is created but cannot activate its own ringing call; the other
participant must accept before either instance mints their token.

The caller's instance is the state authority. A replica applies the exact
validated response returned by that authority instead of independently computing
a transition. Terminal state is also pushed through authenticated
`POST /_kaede/v1/calls/state`. End and decline retries are idempotent, preserve the
original terminal timestamp, and retry the safe gateway, federation, and room
cleanup projections after a lost response. Deleting the `d.*` LiveKit room evicts
participants but is best-effort, so an SFU outage cannot turn a committed end into
a false API failure. A five-minute scheduled sweep removes ended or Redis-orphaned
DM rooms with bounded deletions and control-plane timeouts. The browser implements
ring, accept, decline, end, microphone, camera, screenshare, remote audio,
participant video tiles, a large screenshare tile, and a responsive control dock
for both DM calls and guild voice channels.

## Deployment and validation

The production `voice` Compose profile runs pinned LiveKit with host networking,
ICE TCP 7881, UDP mux 7882, embedded TURN UDP (13478 by default), TURN/TLS 5349,
an API webhook on the loopback-only API binding, and an empty-room timeout of
300 seconds. Caddy exposes `/livekit` only when `KAEDE_VOICE_ENABLED=true`.
Application controls reach the host-networked server through the Docker host
gateway; secrets never reach the frontend or gateway service.

`make voice-check` starts only an isolated, non-published Dragonfly and LiveKit
validation pair. It checks occupancy replay fencing, caller/callee call
transitions, terminal and replica replay, orphan-room cleanup, room control, and
token generation, then removes the project and volumes. The normal backend suite
covers grant claims, webhook verification, canonical identifiers, metadata
binding, strict federation schemas, authoritative call-state propagation, and
stale occupancy. The frontend suite covers token admission; lint, Svelte
diagnostics, production build, and CSP validation cover the complete dock.

## Limitations

Voice federation has no SFU failover in v1. The guild or call authority remains
the only media authority, so calls become unavailable rather than moving to a
different authority during an outage. Rate limits, admission controls,
observability, and operational procedures are documented in
[m6-hardening-release.md](m6-hardening-release.md).
