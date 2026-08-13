# Instance Administration and Developer Portal

This document defines two management surfaces around Kaede's bot platform:

- **Instance Administration** is the control plane for operating one Kaede
  instance. Host access bootstraps the first owner. The panel covers federation,
  bots, Trust & Safety, users, controlled operations, and audit history.
- **Developer Portal** is where local users create and operate applications and
  bots. It covers commands, credentials, workers, install links, federation
  policy, E2EE modes, and diagnostics.

The bot protocol, authentication, installation, Gateway, command, and E2EE
contracts live in [Bots and automations](bots-and-automations.md). This document
covers management and governance. It does not describe an implementation that
already exists.

## Product boundaries

The portals share UI components, not authority.

| | Instance Administration | Developer Portal |
| --- | --- | --- |
| Route | `/administration` | `/developers` |
| Entry | Direct route after an admin grant | Link in user settings |
| Users | Granted local human accounts | All active local human accounts |
| Scope | The local Kaede instance | Applications owned by the user's teams |
| MFA | Required | Required for sensitive operations |
| Message access | Report evidence, when authorized | None |
| Host access | None | None |

Developer access grants no instance authority. Administrator access grants no
ownership of an application and no access to developer secrets. Guild roles,
remote identities, bot credentials, and federation signatures cannot confer
instance-administrator access.

Administration state is local. It is never placed in profiles, presence,
federation documents, guild membership, or bot manifests.

Neither portal includes:

- A shell, SQL console, arbitrary task runner, file browser, or Docker control.
- Raw `.env` values, signing keys, database credentials, TLS keys, or tokens.
- Password, MFA, recovery-code, or session-token disclosure.
- User impersonation.
- General administrator search across private messages.
- E2EE room keys or silent decryption.
- A way for developers to widen guild or instance policy.

## IDs and hostile input

Federated objects use opaque, domain-qualified references. Names are only for
display.

```text
user_id@home.example
application_id@app-home.example
bot_user_id@app-home.example
guild_id@guild-home.example
```

Installation IDs are issued by the target. Reports use a local opaque ID; each
forwarded destination assigns its own case ID. Admin grants use a local user
foreign key and never accept a remote reference.

Display names, reasons, notes, guild names, application names, filenames, and
remote errors are hostile text. They never determine authority, origins,
redirects, or policy targets.

## Instance Administration

### Navigation

- Overview
- Federation
- Users
- Bots and applications
- Reports and Trust & Safety
- Policies
- Operations
- Administrators
- Audit log

### Host bootstrap and recovery

The host CLI is the root of administrator access. The existing shared admin
token is unsuitable for browser use because it has no human attribution, MFA,
least privilege, or reliable per-user revocation.

For a Compose deployment, commands are run inside the API container:

```text
docker compose exec api kaede admin ...
```

The CLI does not invoke Docker itself.

```text
kaede admin bootstrap <local-handle> --role owner
kaede admin grant <local-handle> --role <role> [--expires <time>] --reason <text>
kaede admin revoke <local-handle> [--role <role>] --reason <text>
kaede admin suspend <local-handle> --reason <text>
kaede admin unsuspend <local-handle> --reason <text>
kaede admin list
kaede admin recover-owner <local-handle> --reason <text>
kaede admin recovery-enrollment create --handle <handle> --reason <text>
kaede admin recovery-enrollment approve <enrollment-id> --reason <text>
kaede admin recovery-enrollment revoke <enrollment-id> --reason <text>
kaede admin service-credential create --name <name> --scopes <scopes> --expires <time> --reason <text>
kaede admin service-credential revoke <credential-id> --reason <text>
kaede admin audit verify
```

Grant commands resolve an exact local account, reject remote users, print the
canonical local identity, and mutate under a database transaction and advisory
lock. `bootstrap` works only when no owner exists. A grant remains `pending_mfa`
until the user enrolls MFA and completes a fresh login or step-up.

Grant, recovery, and revocation commands require a reason. They never take a
password, TOTP code, recovery code, private key, or bearer token as a command
argument. Privilege grants and break-glass recovery revoke existing user
sessions so a stolen session cannot inherit new authority.

Owner grants and removals remain CLI-only. Owners can use the panel to grant,
change, expire, suspend, or revoke any non-owner role. The last active owner
cannot be removed without a replacement or recovery operation.

Every CLI change records `host_cli` as the actor plus safe operating-system and
ticket context. `--json` provides bounded machine-readable output for controlled
automation.

#### Owner lockout

`recover-owner` handles an existing local account that can still log in and
complete MFA. Recovery enrollment handles the case where no owner account is
usable:

1. The CLI takes the administration lock and requires the operator to type the
   canonical instance domain.
2. It creates a short-lived, single-use enrollment record. Only a digest of the
   code is stored.
3. The CLI prints the fixed enrollment URL and code once as separate values.
4. The no-store enrollment page receives the code in a same-origin POST body.
   The code never appears in URLs, cookies, analytics, or access logs.
5. Enrollment creates or recovers a local human account, sets a password or
   passkey, enrolls MFA, and confirms recovery codes.
6. The host separately approves the enrollment after checking its ID, account,
   instance, and MFA fingerprint.
7. Approval activates the owner grant, advances the admin generation, and
   revokes other sessions and recovery tickets.

Tickets expire after about 15 minutes. Failed or expired tickets leave no active
grant. This flow is not a permanent browser backdoor or reusable master code.

### Browser authentication

Every administration request requires a normal local-user session, an active
`InstanceAdminGrant`, the endpoint capability, enrolled MFA, and recent MFA-
backed step-up for mutations and sensitive reads.

Step-up creates an opaque, server-side session represented by an HttpOnly,
Secure, SameSite=Strict cookie scoped to `/api/v1/admin`. It is bound to the
user, login session, admin-grant generation, MFA generation, and instance. It
expires in five to ten minutes and has no silent refresh.

Session revocation, grant changes, MFA changes, account disablement, and owner
recovery invalidate step-up immediately. Normal CSRF defenses and an exact
origin check still apply.

High-impact actions also require a typed confirmation and reason. This covers
instance suspension, subdomain-wide rules, evidence export, reporter identity
reveal, mass sanctions, global app suspension, and bulk case actions.

### Roles

All fixed non-owner roles are enabled. Owners may delegate them from the panel.
Role definitions remain code-reviewed capability bundles; operators cannot
create arbitrary role definitions.

| Role | Authority |
| --- | --- |
| Owner | All capabilities and owner recovery; CLI controlled |
| Administrator | Users, federation, bots, reports, policy, and operations |
| Trust & Safety | Reports, evidence, cases, sanctions, forwarding, and appeals |
| Bot reviewer | Applications, invites, installations, workers, and bot policy |
| Operations | Health, quotas, queues, migrations, and safe retries |
| Auditor | Read-only policy, audit, and operations; no evidence by default |

Representative capabilities:

```text
admin.access.read
admin.access.manage_non_owner
admin.audit.read
admin.audit.export
admin.overview.read
admin.users.read
admin.users.suspend
admin.users.sessions.revoke
admin.federation.read
admin.federation.policy.manage
admin.federation.delivery.retry
admin.bots.read
admin.bots.policy.manage
admin.bots.suspend
admin.reports.read
admin.reports.evidence.read
admin.reports.assign
admin.reports.resolve
admin.reports.forward
admin.reports.reporter_identity.reveal
admin.operations.read
admin.operations.retry_safe_job
admin.settings.read
admin.settings.write_safe
```

Routes check capabilities rather than role names. Evidence, reporter identity,
exports, federation policy, and global bot suspension stay separately scoped.

### Overview

The overview shows release and migration state; service health; federation
circuits and queues; storage and quotas; bot workers, installs, rates and
failures; open reports; forwarding failures; evidence expiry; and recent high-
impact admin events. It does not show raw environment values, arbitrary logs,
message content, report evidence, or E2EE data.

### Federation

The federation page extends the existing peer, approval, drain, and
`InstanceBlock` services. A peer view includes identity and key generation,
capabilities, compatibility, last contact, circuit and queue state, replica
storage, cached identities, local memberships, bot installs, report delivery,
and matching policy rules.

Operators can approve peers; silence or suspend exact instances; optionally
include subdomains; set expiry and public/private reasons; preview affected
users, guilds, queues, bots, installs, and known subdomains; validate CSV policy
imports; and run predefined drain or retry actions.

Targets use federation's existing IDNA and origin normalization. Rules reject
the local instance, IP literals, malformed origins, public suffixes, and unsafe
subdomain boundaries.

`silence` removes discovery and pauses configured cross-instance activity while
preserving traffic that policy explicitly allows. `suspend` rejects ordinary
federation traffic and direct bot sessions. Both advance a monotonic generation
so active sessions recheck policy and close promptly.

Suspended peers cannot submit safety reports. Silenced peers are also denied
unless an operator creates a per-peer report-admission exception. The exception
is visible in the peer view, audited separately, and removable without changing
the silence rule.

Unblocking performs bounded reconciliation and revalidates queued work rather
than replaying expired actions. These controls are application-layer federation
policy, not a host firewall.

### Users

The user page distinguishes local humans, local bots, and cached remote
identities. It offers bounded lookup by local handle, composite ID, state, or
origin and shows account state, MFA/verification flags, session count, storage,
restrictions, and linked cases.

Authorized roles may revoke sessions, apply temporary restrictions, disable or
re-enable local accounts, and restrict remote identities locally. The UI never
suggests that a local action disabled a remote account at its home. Passwords,
recovery codes, TOTP secrets, tokens, and unrelated private fields are hidden.

### Bots and applications

This view shows app and bot identity, publisher home, manifest/root-key
generations, review state, installs, grants, E2EE modes, invite resolution,
workers, target credentials, rates, queues, reports, and sanctions.

Operators can set policy for local, remote, reviewed, high-risk, and blocked
apps; add exact app/origin rules; require review for privileged access; suspend
an app locally; disable invites; disconnect workers; revoke target credentials
or local installs; and open a Trust & Safety case.

The page cannot retrieve private keys, worker keys, one-time credentials, OAuth
secrets, E2EE keys, or bot-decrypted plaintext. Revocation stops future access;
it cannot erase data already copied by a bot operator.

### Policies and operations

The browser edits only validated, database-backed live policy: registration,
federation admission, bot admission/review, report admission and retention, safe
quotas, rate ceilings, and media quarantine.

Report categories and rule IDs are fixed by code and protocol. Operators cannot
rename, remove, reorder, or add them. A new category requires a versioned code
and federation change.

Secrets and host settings are read-only summaries at most. The UI may show
their source, restart requirement, and documentation, never their value.
Updates are schema validated, transactional, versioned, diffed, audited, and
protected by generation or ETag.

Operations exposes only bounded queue/job state, predefined retries, usage
reconciliation, safe garbage collection, migration/search/cache/backup status,
and application-defined maintenance modes. No endpoint accepts SQL, shell
commands, task names, URLs, or paths from the browser.

### Administrators and audit

Administrators shows grants, role, expiry, last use, MFA readiness, history,
and suspension. Owners manage non-owner roles here; owner membership remains
CLI-only.

Sensitive reads and all mutations, including failures, create an
`InstanceAuditEvent` with actor, action, target, redacted diff, result, reason,
trace, source, generation, and time. Evidence access, exports, identity reveal,
sanctions, grants, and policy changes are always recorded.

Entries are append-only, hash chained in bounded partitions, backed up, and may
be exported to an operator-owned append-only sink. They exclude secrets, tokens,
E2EE plaintext, and full evidence. This does not claim protection against a host
operator with direct database access.

The legacy shared token remains an opt-in compatibility setting, disabled by
default and never accepted by the browser. Audit identifies it as
`legacy_admin_token`. Named, scoped, expiring, hash-only service credentials
replace it for automation; those credentials cannot manage owners, view
evidence, reveal reporters, recover accounts, or retrieve secrets.

## Reporting and Trust & Safety

Reports belong exclusively to Instance Trust & Safety. Guild moderators do not
receive a report queue or report permission. Trust & Safety staff may coordinate
authoritative guild action through internal moderation tools, but evidence and
reporter data stay in the instance case system.

A report is an allegation. A federation signature proves who sent an envelope,
not whether the allegation is true.

### Targets and categories

Supported targets are plaintext messages, attachments, users/profiles, guilds,
bots/apps/installations, invites, and federated instances.

Categories are fixed wire values:

```text
spam
scam_or_phishing
harassment
hate_or_abuse
threat_or_violence
sexual_content
child_safety
privacy_or_doxxing
impersonation
malware
self_harm_concern
local_rule_violation
illegal_content
other
```

Severity is assigned during triage, not trusted from the reporter.
`self_harm_concern` enters a safety workflow rather than an automatic sanction.

### Submission and evidence

The user selects **Report**, confirms the target, picks a category, optionally
adds a bounded note/context, reviews any federated disclosure, chooses immediate
protection such as hide/block/mute/leave, and receives a local case ID with a
separate forwarding state.

**My reports** shows local review, forwarding, remote receipt, outcome,
closure, and withdrawal. It does not present local acceptance as successful
remote delivery. Submission does not notify the reported user.

For plaintext messages, the client submits a reference, not authoritative
content. The server verifies access, resolves the stored revision, confirms the
room is plaintext, and enforces idempotency and evidence limits.

Evidence may include message/author/context references, times, revision,
bounded structural content, replies, mentions, attachment hashes and metadata,
safe embed metadata, and authority envelope digests. Provenance is one of
`authority_verified`, `verified_local_replica`, `client_supplied`, or
`staff_added`. Screenshots and uploads are never marked verified.

Adjacent messages require explicit preview. Attachments are quarantined,
blurred, non-autoplaying, and served by an authenticated purpose-bound route.
Every reveal/download is audited. Report content never causes URL fetching or
active embed rendering.

### Privacy and E2EE

Reporter identity remains at the reporter's home by default. Forwarding uses
the origin and a case-specific return capability, not the person's account ID.
IP, user agent, email, session, device, and stable cross-case pseudonyms do not
federate.

The reported user never receives reporter identity. Local identity reveal needs
a separate capability, recent step-up, reason, and audit event. Internal,
reporter-visible, and remote-shareable notes are separate and default internal.

The plaintext evidence flow is unavailable in E2EE rooms. Metadata-only reports
may carry sender, room/message reference, timestamp, ciphertext digest, and
routing data, but are never called verified plaintext. Account, bot, invite,
guild, and instance reports remain available without content.

### Cases and federation

```text
submitted -> triaged -> in_review
          -> awaiting_remote | needs_information
          -> action_taken | closed_no_action | duplicate
          -> reopened
```

Reports and decisions are separate records. Decisions store authority, target,
category, scope, action, duration, public reason, private rationale, evidence,
appeal state, and reversal. Affected users receive the action, scope, duration,
public reason, redacted evidence summary, and appeal route, never reporter
identity or unrestricted evidence.

Federation uses separate `report.submit`, `report.receipt`, `report.status`, and
`report.case_message` events. Case messages use destination-issued, report-
specific, expiring capabilities and cannot address another case or relay again.

A signed submission binds origin report ID, audience, target, category, bounded
note, evidence digests/provenance, access-check attestation, disclosure mode,
expiry, nonce, and idempotency. The receiver verifies signature/replay,
authority, size, references, and policy, then issues a local case ID.
Deduplication uses `(origin, report_id, destination)`.

Suspended peers are always denied. Silenced peers are denied unless a visible,
audited per-peer report exception allows them. Urgency does not bypass policy or
rate limits.

### Storage

Limits apply by reporter, target, origin, origin/target pair, evidence bytes,
media count, queue depth, and delivery concurrency. When evidence storage is
near capacity, text-only reports remain available and optional attachment
failure is explicit.

Default retention is full evidence while a case/appeal is open and 90 days
after closure, plus case metadata/decisions/audit for 365 days. Rejected or
duplicate evidence expires sooner. Legal holds are explicit and expiring.

Each evidence object has a random DEK. By default, Kaede wraps it with a
versioned moderation key generated during setup and stored in the deployment's
existing secret boundary, outside PostgreSQL. Operators may use an external KMS
instead. Associated data binds the report, evidence ID, provenance, revision,
and authority. Forwarded evidence gets a new DEK sealed to the destination's
pinned moderation key. Evidence and plaintext keys stay out of logs, caches,
search, and analytics. Key use, evidence reveal, export, and failed access are
audited.

## Developer Portal

### Access and layout

Every active local human account can create applications. Basic creation has no
account-age, MFA, or manual-approval gate. Remote cached accounts cannot create
apps on this home instance.

Review still applies when an app asks for privileged access. Account sanctions
and an emergency instance-wide creation stop remain abuse controls, not normal
eligibility gates.

The portal is a responsive web app linked from user settings. Tauri loads the
same pages. Mobile browsers can use the responsive site; Kaede does not need a
native mobile Developer Portal or mobile-specific API.

Normal reads use the existing login session. Recent reauthentication and MFA
are required for credentials, keys, endpoints, OAuth redirects, federation or
E2EE policy, ownership, suspension, and deletion. Users without MFA are guided
through enrollment when they first attempt one of those actions.

Developer step-up is distinct from admin step-up. It is bound to the login
session, MFA generation, team generation, purpose, and optional app ID, with a
short TTL and no silent refresh.

Portal navigation covers applications, teams, documentation/Python SDK, usage,
and notifications. An app workspace contains identity, commands, install links,
installations, workers, credentials, endpoints, federation, E2EE, diagnostics,
team access, audit, and lifecycle pages.

### Application creation and identity

Creating an application asks for a name and optional description, icon, and
support/privacy links. Kaede creates the draft application, its bot user, and a
personal developer team in one transaction. The bot badge can be previewed at
this point.

The new workspace then guides the developer through commands, scopes, intents,
permissions, data access, E2EE mode, federation policy, workers, and the first
install template. An invite preview appears only after a template has been
saved. Credentials are created separately and shown once.

The overview shows immutable app/bot IDs, publisher team, status, manifest
generation, installs, worker health, command sync, rate pressure, policy
warnings, E2EE modes, and public/install links. Bot users cannot gain passwords,
human sessions, MFA methods, or user tokens.

### Teams

Every app belongs to a team, including single-person projects.

| Role | Access |
| --- | --- |
| Owner | Transfer/delete app, manage owners, all app actions |
| Administrator | Config, workers, credentials, commands, installs |
| Developer | Commands, endpoints, workers, tests, diagnostics |
| Security | Keys, credentials, audit, incident response |
| Analyst | Read-only aggregate usage and health |
| Support | Install diagnostics and public metadata |

All roles are enabled. These are application-team roles and are unrelated to
Instance Administration roles. Team membership is limited to local accounts on
the application home. Ownership transfer requires current-owner step-up,
acceptance by the new owner, a notification delay, and at least one remaining
owner. Changes are audited and notify existing owners.

### Keys and credentials

Kaede-managed application root keys are the default. Setup creates a versioned
application-key wrapping key inside the deployment's existing secret boundary,
so a separate KMS is not required. The application home generates and encrypts
each root, exposes only the required signing operation, and includes the
wrapping key in the operator's encrypted secret backup. Root private keys are
non-exportable and are not displayed in the browser.

Advanced teams may register a developer-held or external-KMS public key and
prove possession. Kaede then never holds the private key, while the team owns
availability, backup, and recovery. Switching modes explains the tradeoff and
requires continuity proof or target reapproval.

Application identity, worker enrollment, target DPoP/session, interaction
verification, OAuth, and E2EE keys remain separate. Opaque credentials are
hash-only at rest, named, scoped, expiring, and shown once. Rotation may allow a
short overlap. Tokens never enter URLs, logs, manifests, invites, analytics, or
exports.

### Workers, commands, and interactions

A worker page shows its public key, environment/tags, target assignments,
intents, scopes, expiry, session ceiling, Gateway/API health, resume state,
rates, activity, and bounded errors.

Workers connect directly to target instances; the application home is not a
traffic relay. Enrollment uses a single-use, audience-bound challenge and proof
of possession. Revocation advances the worker generation and closes old
sessions. Only the target can grant or revoke target-local authority.

The command editor supports slash, user, and message commands; subcommands;
typed options; localization; autocomplete; buttons; selects; modals; deferred
replies; follow-ups; and private responses over Gateway or signed HTTPS.

Developers can use a form or canonical JSON. Publish validates names, types,
limits, duplicate keys, scopes, endpoint health, and E2EE compatibility, then
creates a command generation and displays state per target. One failed target
does not roll back successful targets or appear as global success. Synthetic
test interactions cannot read content or bypass grants.

### Install templates and installations

Templates are stable, named, revisioned bundles of requested scopes, intents,
permissions, contexts, channel constraints, and E2EE mode. The portal provides
portable HTTPS/Kaede links, a signed-data embed preview, clear permission
summaries, target compatibility, and aggregate install outcomes.

Changing a template never expands an existing install. Added access requires a
new target grant and guild-admin consent. Removing a template disables its links
without uninstalling existing bots.

An installation view shows target/guild, template and grant revision, approved
access, channel restrictions, E2EE mode, workers, health, activity, command sync,
and policy failures. Developers may reduce behavior, request a new review, stop
workers, or initiate uninstall; they cannot expand target grants.

### Review and federation policy

Cryptographic application origin and review by a target instance are separate
facts. This design does not define global publisher reputation or verification.
Target review is local and non-transitive, and a remote badge is not rendered as
local approval.

Privileged-access review captures an immutable manifest, key/command
generations, use case, requested data, retention/privacy disclosures, test
install, and contact path. Bot reviewers may approve, restrict, request changes,
reject, or suspend. Sensitive scope expansion, broken key continuity, material
endpoint/retention changes, and expiry trigger re-review.

App federation policy is `open`, `allowlist`, `blocklist`, or `local_only`.
Rules match only exact canonical origins, and deny wins. Effective access is
the intersection of developer policy, target policy, guild consent, install
state, grants, worker scope, and encryption policy. Developers can only narrow
access.

### E2EE, OAuth, and endpoints

E2EE offers two modes:

- **Interaction only:** clients encrypt an explicit command/form payload to the
  app; the bot has no ambient history.
- **Participant:** the bot is a visible cryptographic participant and may read
  authorized future messages. Users are told the bot operator receives
  plaintext.

The portal manages public keys, device verification, modes, rotation, and
compatibility, never decrypted content or room keys. Participant install starts
a new epoch; removal rotates it. Pre-install history keys are denied by default.
Server-side search and plaintext history remain unavailable for E2EE content.

OAuth uses exact HTTPS redirects (apart from documented loopback development),
authorization code, PKCE S256, state, issuer/audience binding, short expiry, and
single use. Implicit grants and open redirects are not supported.

Interaction endpoints require public HTTPS, ownership verification, signed
requests, DNS/IP validation on each connection, private/link-local/metadata
denial, bounded payloads, short deadlines, and no redirects.

### Diagnostics and lifecycle

Diagnostics include rates, latency, retries, Gateway gaps, queue depth, command
outcomes, worker uptime, target compatibility, invite conversion, trace IDs, and
safe errors. They exclude content, E2EE plaintext, reports, unrelated users,
other apps, raw envelopes, and target logs.

Notifications cover worker outages, credential/key expiry, unexpected use,
install changes, command failures, quota pressure, review or enforcement
affecting the app, and deletion milestones. They contain no report details,
reporter identity, evidence, secrets, or message content.

App states are `draft`, `active`, `review_required`, `suspended`, `deleting`,
and `deleted`. Deletion requires step-up, typed confirmation, and reason; then
disables installs, starts a grace period, notifies owners/targets, revokes
workers and credentials, preserves message attribution, and tombstones IDs.

## Data model

Administration adds `InstanceAdminGrant`, `InstanceAdminServiceCredential`,
`InstanceAuditEvent`, versioned `InstancePolicy`, extended `InstanceBlock`, and
`InstanceSanction`.

Trust & Safety adds `AbuseReport`, `ReportEvidence`, `ReportDestination`,
`ReportAssignment`, `ReportNote`, `ReportEvent`, `ModerationDecision`,
`DecisionReport`, `ModerationAppeal`, and `ReportLegalHold`.

Developer management keeps the bot architecture's canonical entities:
`BotApplication`, `BotApplicationKey`, `BotCredential`, `BotInstallation`,
`BotWorker`, `BotInstanceRule`, `BotInstallTemplate`, `ApplicationCommand`, and
`BotToken`. It adds `DeveloperTeam`, `DeveloperTeamMember`, authoritative
`ApplicationOwnership`, `ApplicationReview`, and `ApplicationAuditEvent`.

There is no developer eligibility table because active local users may create
apps. Step-up sessions, Gateway state, nonces, install challenges, and resume
cursors are ephemeral Dragonfly records with bounded TTLs.

## API outline

Collections use opaque cursors, bounded filters, stable ordering, strict
schemas, authorization before disclosure, and generation/ETag checks. Retried
mutations support idempotency; security and moderation actions require reasons.

Key route groups:

```text
/api/v1/admin/@me
/api/v1/admin/auth/step-up
/api/v1/admin/overview
/api/v1/admin/operators
/api/v1/admin/federation/peers
/api/v1/admin/federation/blocks
/api/v1/admin/federation/report-exceptions
/api/v1/admin/users
/api/v1/admin/applications
/api/v1/admin/application-reviews
/api/v1/admin/reports
/api/v1/admin/decisions
/api/v1/admin/audit/events
/api/v1/admin/operations
/api/v1/reports
/api/v1/decisions/{decision}/appeals
/api/v1/applications
/api/v1/developer-teams
/api/v1/applications/{app}/credentials
/api/v1/applications/{app}/workers
/api/v1/applications/{app}/commands
/api/v1/applications/{app}/install-templates
/api/v1/applications/{app}/installations
/api/v1/applications/{app}/diagnostics
/api/v1/applications/{app}/reviews
/api/v1/applications/{app}/audit
/api/v1/applications/{app}/deletion
```

Admin operator endpoints reject the `owner` role. The command editor keeps
changes unpublished until **Publish**; publishing validates the full set and
uses the canonical atomic `PUT /api/v1/applications/{app}/commands` route. Human
OAuth, developer management, bot target auth, bot REST, and Gateway use separate
credentials and authorization dependencies.

## Web, limits, and security

Both portals are Svelte routes and Tauri uses the same pages. Required behavior:

- Separate layouts, state stores, and route guards.
- Server authorization on every request; route guards are only UX.
- Restrictive admin CSP, no third-party scripts/framing/referrer leakage, and
  `no-store` responses.
- Hostile fields rendered as text; evidence blurred and isolated.
- Blast-radius preview for destructive/bulk changes.
- Honest pending/success/failure/unsupported federation state.
- Distinct permission, step-up, policy, rate, quota, outage, and validation
  errors.
- Keyboard, focus, labels, contrast, reduced motion, and screen-reader support.

Dragonfly handles distributed rates; durable stores enforce row/byte ceilings.
Limits apply per admin action, reporter, origin, app, team, worker, install,
command publish, evidence, queue, and export. Defaults are selected through load
testing. Setup accepts `100K`, `2M`, `1GB`, and `10GiB`, shows parsed values,
and estimates storage and throughput impact. Failures include stable codes and
retry times.

Secrets are shown once or never and excluded from logs, errors, analytics,
audit, URLs, and exports. Federated payloads are strictly bounded, audience-
checked, replay-protected, and idempotent. Remote URLs use SSRF-safe transport.
Policy generations fence active work. Report floods, bot loops, exports, and
queue/storage amplification have independent budgets and alerts.

Metrics and alerts cover admin auth/grants, federation queues/circuits, bot
installs/sessions, report backlog/evidence, credentials, workers, commands,
storage pressure, high-impact actions, and audit-chain failure. Backups include
grants, policy, audit, teams, app public config, reports, encrypted evidence,
holds, and key recovery material. Restore drills verify IDs, revocations, keys,
evidence references, and audit chains.

## Migration and release gates

Schema changes add nullable structures, run bounded resumable backfills, enable
new writes, verify invariants, add constraints, then retire legacy behavior.
Existing federation block APIs continue through shared services. The legacy
admin token stays an opt-in transition path, never a portal dependency.

Older peers receive explicit unsupported states for missing bot, worker,
report, or command capabilities. Rolling deploys never widen access because a
component has not learned a restriction.

Release coverage includes:

- CLI bootstrap/recovery, last-owner protection, local-only grants, pending MFA,
  expiry, session invalidation, and concurrency.
- Capability matrix, cross-role, CSRF, origin, step-up, cache, and cross-tenant
  negative tests.
- Federation block/report-exception precedence, session closure, reconciliation,
  and race tests.
- App/origin/install/worker/token suspension and revocation.
- Report access, revision capture, E2EE refusal, provenance, privacy, audience,
  replay, quota, retention, hold, appeal, and hostile-peer tests.
- Evidence XSS, MIME, decompression, quarantine, no-fetch, and audit tests.
- Teams, transfer, credentials, managed/external keys, commands, endpoints,
  templates, invite sanitation, grant non-expansion, and deletion.
- Load, migration, restore, responsive web, Tauri, accessibility, and eventual
  federation-state tests.

No route ships without least-privilege authorization, structured errors,
idempotency where needed, quotas, metrics, audit behavior, documentation, and
client handling.

## Delivery order

1. Admin identity: CLI grants/recovery, roles, MFA step-up, audit, service
   credentials, and legacy-token migration.
2. Admin core: overview, federation, users, policy, and safe operations.
3. Developer foundation: teams, app identity/badge, managed and external keys,
   credentials, and app audit.
4. Commands and install: editor, publish state, links, embeds, consent, and
   target visibility.
5. Bot operations: workers, direct-target state, diagnostics, federation policy,
   revocation, usage, and SDK links.
6. Trust & Safety: reports, evidence, decisions, appeals, retention, and
   federated status.
7. E2EE management: interaction-only first, then participant mode after Kaede's
   group-encryption protocol is ready.

Each stage is production-gated; disabled policy does not justify placeholder
authentication or unsafe APIs.
