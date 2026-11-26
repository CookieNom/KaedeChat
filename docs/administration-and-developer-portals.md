# Instance Administration and Developer Portal

Kaede has two management areas, available on the web and in the desktop app:

- `/administration` is for instance owners and the staff they delegate.
- `/developers` is open to every active local human account.

On mobile, the same areas live under User Settings. The mobile Instance
Administration screens show only the sections your role can use.

## Instance Administration

### Voice and video (RTC)

Kaede uses its built-in LiveKit server by default. Owners and instance
administrators can set up Cinnamon as an optional provider under
**Administration → Voice and video** (on mobile, under Instance
Administration). Nothing for Cinnamon is preconfigured; you supply the
credentials, endpoints and regions.

1. Enter the **project** API key and secret, an automatic `wss://` endpoint,
   and the regional endpoints your project allows. Kaede encrypts the
   credentials with the instance secret key and never shows the saved secret
   again, so keep the instance secret key with your database backups.
2. Click **Save RTC settings**.
3. Test Automatic, then **Test without latency hints**, then each enabled
   region. Tests only create and remove rooms named `kaede-rtc-test-…`. If
   cleanup fails, the result names the room you need to remove.
4. Select Cinnamon as the provider.

In your Cinnamon project:

- Set the webhook receiver to the complete URL shown in administration:
  `https://<configured instance domain>/api/v1/voice/cinnamon/webhook`. It's a
  direct public route, so don't add a query string or redirect.
- Sign deliveries with the same project API-key identifier shown there.
- Enable the listed region IDs for your project and plan, and allow both
  automatic and manual routing.
- Keep the automatic-routing allowlist consistent with the regions you want to
  permit. Regions you disable in Kaede are left out of its selector and probes.
- Leave the **required hint** policy off. Clients send only successful HTTPS
  round-trip measurements, so routing has to work when every probe fails. No
  location information is collected or sent.

After a test room, refresh the webhook status to confirm that Cinnamon's
events were accepted and processed. Kaede stores each verified event before
acknowledging it and retries failed processing in the background, ignoring
events that no longer match the live room. Pending events survive restarts.
When you upgrade, apply database migrations before starting the new API
workers.

Provider, endpoint and credential changes apply to new rooms only. Rooms that
are already running keep their saved endpoint and credentials, including for
moderation and reconnects, so keep old project credentials valid until those
rooms finish.

### Granting access

Administrative access belongs to an ordinary local user account. The host
grants the first owner role from the CLI. Register the local account first,
then run this from the repository root:

```sh
make exec SERVICE=api COMMAND='kaede admin-grant alice --role owner'
```

Use the same `make exec SERVICE=api COMMAND='...'` wrapper for other changes,
such as `kaede admin-grant bob --role trust_safety` or
`kaede admin-revoke bob --role trust_safety`. The command reads the
deployment's `.env`.

The command accepts a local `username` or `username@this-instance` and rejects
remote and bot accounts. Granting a role the user already holds succeeds
without changing anything. Revoking a role they don't hold returns an error.

The owner role can only be granted or removed from the CLI. An owner can grant
and revoke every other role from the Administration page. Keep at least two
owner-capable local accounts on a production instance.

Staff use their normal account session; the browser never receives
`KAEDE_ADMIN_TOKEN`. Every request rechecks the caller's active grants, so
disabled users, expired or revoked grants, remote users and bot users are
refused.

### Roles

| Role | Access |
| --- | --- |
| Owner (`owner`) | Every administration capability; CLI controlled |
| Administrator (`administrator`) | Voice and video, users, instances, bots, reports, and audit; can view operators but not change them |
| Trust & Safety (`trust_safety`) | Reports, child safety, local-user state, and instance policy |
| Bot reviewer (`bot_reviewer`) | Bot applications and the audit log |
| Operations (`operations`) | Instance policy and the audit log |
| Auditor (`auditor`) | Read-only reports and audit log |

Every role can also view the overview, users, applications, instances and
operators.

The panel hides controls you can't use, and the API repeats every permission
check. `GET /api/v1/administration/@me` returns your roles and effective
capabilities.

### Panel sections

**Overview** shows counts of local users, known instances, bot applications,
active bot installations, open reports and blocked instances.

**Users** lets staff search local usernames and disable or re-enable accounts.
You can't disable your own account here.

**Applications** lists local and federated bot applications. Staff with
`bots.manage` can suspend or reactivate an application and review App
Directory listings (see [App Directory](#app-directory)).

Suspending an application:

- moves its guild installations to `suspended` and takes its commands and
  interactions out of service, including user-installed commands;
- ends its active DM capabilities, DM grants and consents, encrypted-room
  participation and voice/video media sessions. Affected encrypted rooms are
  rekeyed.

Reactivating it:

- resumes only the guild installations that the suspension paused;
- makes a user-installed command available again only while the user's own
  installation is still active and, on another instance, its time-limited
  lease hasn't expired;
- doesn't restore DM access, consents, encrypted-room participation or media
  sessions. The bot has to open a fresh DM capability and repeat consent, room
  admission and the MLS rekey.

Removals made for other reasons are permanent. A guild kick, ban, uninstall,
failed reinstall or explicit revocation marks the installation `revoked`, and
reactivating the application never brings it back. A user's own revocation
also stays in place. Guild administrators keep the final say over
installations in their guilds; instance staff can only pause and resume what a
suspension paused.

Only the application's home instance can suspend or reactivate it. Staff on
other instances can enforce their own instance policy against a remote
application, but can't change its status there. Their instance follows the
home's signed updates.

**Instances** shows, for each known remote instance, what your instance holds
locally: users and their messages, guild replicas, federated DM
conversations, cached media, inbound event counts, last contact, software
version and delivery backlog. Expand a row for a storage breakdown, and use
search and sorting to compare instances. Viewing it requires `admin.read`.

Storage totals combine quota-counted database data and cached media bytes.
They aren't the physical database size or the remote server's own totals.
Guild usage counts toward the guild's origin, DM usage toward its quota origin,
and media toward the attachment's origin. Counts follow retention and leave
out deleted messages.

The same section manages exact-domain `silence` and `suspend` rules.
Subdomains are included only when you select that option. A new rule applies
consistently to queued deliveries and replicas. See
[Federation allowlists and blocklists](operator.md#federation-allowlists-and-blocklists)
for what each rule blocks.

**Operators** lists active grants. Owners can add or revoke non-owner roles
here. Owner rows are shown but are managed from the CLI.

**Audit log** lists recent instance actions with actor, action, target, safe
metadata and timestamp. Secrets and message bodies are never written to audit
metadata.

### Reports and Trust & Safety

Users can report a plaintext message they can access from its context menu,
and follow the status of their own reports at `/reports`. The categories are
fixed:

- child sexual abuse material (CSAM)
- spam
- harassment
- hate
- sexual content
- violence
- self-harm
- impersonation
- privacy
- malware
- illegal content
- other

When a report is submitted, the server rechecks the reporter's channel access.
It then copies the message content, author reference, channel reference and
message timestamp into the case, so later edits or deletion can't erase the
review record. The reporter can't read the evidence snapshot, the assignee or
the internal resolution.

An E2EE message can be reported only after the reporter's client decrypts that
specific message and the reporter confirms the selective disclosure. The case
stores the disclosed text, a fingerprint of the original ciphertext, and
`server_verified=false`, and moderators see that the evidence is
reporter-supplied. Room keys, other messages and surrounding history are never
included automatically.

A report about a message in a channel hosted on another instance is forwarded
to that instance, and the local copy is marked `awaiting_remote`. If the
remote instance is unavailable or rejects it, the report isn't saved and the
reporter sees an error. CSAM reports are never forwarded. They always stay
with the reporter's home instance for child-safety review.

The Trust & Safety queue belongs to the instance; guild moderators don't
receive it. Authorized staff can triage, review, request more information,
record action or no action, mark duplicates and reopen cases. Each local user
can submit at most 10 reports per hour.

### Child-safety review and external reporting

CSAM reports and PhotoDNA matches are handled in **Administration → Child
safety**, separate from the ordinary report queue. Reading cases requires
`safety.read`, managing them requires `safety.manage`, and downloading
preserved evidence requires `safety.evidence`. Evidence is encrypted and is
never previewed automatically. Staff must download it explicitly through an
authenticated, audited endpoint.

Review a pending case to confirm removal or dismiss the finding. Account
suspension is an option only for a local subject with verified provenance;
reporter-disclosed E2EE evidence doesn't establish the alleged sender's
responsibility. To confirm removal when the source is unverified, you must
record that you independently verified the source message.

The case keeps the original identity snapshot alongside the account's current
identity and the session device, IP address and activity metadata available at
deletion. Passwords, MFA secrets, session IDs and authentication tokens are
never copied. Remote account identities remain claims of their originating
instance.

Submit the report through CyberTipline yourself, then record the completed
submission's receipt or reference, the actual submission time, the evidence
you selected and any notes. This records that the submission is complete; it
doesn't transmit anything to NCMEC. The retention deadline is at least one
calendar year from the recorded submission time, and you can extend it with a
reason. Placing or releasing a hold also requires a reason and is audited.

Closing a case doesn't delete its evidence. Deletion becomes available only
after review and any applicable reporting obligations are complete, the
retention deadline has passed and every hold is released. Pending cases, and
confirmed cases that haven't been submitted, can't be expired this way.
Disposal also removes the preserved account and message snapshots. Ordinary
account or message deletion doesn't dispose of retained case evidence. A daily
retention task also disposes of closed cases once these conditions hold.

### Administration API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/administration/@me` | Current roles and capabilities |
| `GET` | `/api/v1/administration/overview` | Instance counts |
| `GET/POST` | `/api/v1/administration/operators` | List or grant non-owner roles |
| `DELETE` | `/api/v1/administration/operators/{grant}` | Revoke a non-owner role |
| `GET` | `/api/v1/administration/users` | Search local accounts |
| `PATCH` | `/api/v1/administration/users/{user}` | Disable or enable an account |
| `GET` | `/api/v1/administration/applications` | List bot applications |
| `PATCH` | `/api/v1/administration/applications/{app}` | Activate or suspend an app |
| `PATCH` | `/api/v1/administration/applications/{app}/directory` | Approve, remove, or place an App Directory listing |
| `GET` | `/api/v1/administration/reports` | List reports |
| `PATCH` | `/api/v1/administration/reports/{report}` | Update a report's status and resolution |
| `POST` | `/api/v1/administration/reports/{report}/actions` | Record an enforcement action |
| `GET` | `/api/v1/administration/reports/{report}/attachment/{variant}` | Download preserved attachment evidence |
| `GET` | `/api/v1/administration/reports/{report}/export` | Export a report case |
| `POST` | `/api/v1/administration/reports/{report}/external-submissions` | Record an external report submission |
| `GET` | `/api/v1/administration/child-safety[/{case}]` | List or read child-safety cases |
| `POST` | `/api/v1/administration/child-safety/{case}/...` | Review, record submissions, set retention, place or release holds, and close |
| `GET` | `/api/v1/administration/child-safety/{case}/evidence/{evidence}` | Download one preserved evidence item |
| `DELETE` | `/api/v1/administration/child-safety/{case}/evidence` | Dispose of a case's preserved evidence |
| `GET/PUT` | `/api/v1/administration/rtc` | Read or save voice and video settings |
| `GET` | `/api/v1/administration/rtc/probes` | Read the latency probe endpoints |
| `POST` | `/api/v1/administration/rtc/test` | Test an RTC configuration |
| `GET` | `/api/v1/administration/instances/statistics` | Local footprint and delivery statistics for remote peers |
| `GET/PUT` | `/api/v1/administration/instances/blocks` | List or create instance rules |
| `DELETE` | `/api/v1/administration/instances/blocks/{domain}` | Remove an instance rule |
| `GET` | `/api/v1/administration/audit` | Read recent audit events |
| `POST` | `/api/v1/reports` | Submit a report |
| `POST/PUT` | `/api/v1/reports/{report}/attachment-evidence` | Reserve or commit reporter-supplied attachment evidence |
| `GET` | `/api/v1/reports/@me` | Read the reporter-safe case view |

## Developer Portal

Every active local human user can create applications. The first time you
open `/developers`, Kaede creates a protected **Personal** team for you. It's
always available, stays private to your account, and owns new applications by
default even if you also belong to shared teams. You can create named teams
too, and add human users from any federated Kaede instance to them.

Developer-team roles are separate from instance administration roles:

| Role | Developer access |
| --- | --- |
| Owner | Full team and application control |
| Administrator | Everything an owner can do except granting the owner role, changing an owner's role, or removing an owner |
| Developer | Create applications, publish commands, create invite links, enroll workers, and manage assets and emoji |
| Security | Control credentials, worker enrollment and revocation, and instance rules |
| Analyst | Read-only access to applications, commands, workers, invite links, installations, and rules |
| Support | Same read-only access as Analyst |

Only owners, administrators and Security members can see control credentials.
Changing application settings, including scopes, intents and target policy,
needs an owner or administrator. The last owner can't be demoted or removed.
Personal teams stay single-user. Each team, including Personal, can own at most
75 applications that haven't been deleted.

Teams and applications stay on their home instance when members use other Kaede
instances. Members can still use the normal team and application URLs from
their own instance, which forwards each portal action to the team's home. The
home rechecks the member's current membership and role every time. When
someone is removed from a team, their instance is told, and the removal still
arrives after a long outage. A member's profile details are accepted only from
that member's own instance.

### Applications

Creating an application also creates its bot user, with `account_type=bot`.
Bot usernames use the regular `username@domain` format with a unique numeric
suffix. API payloads also include the bot's composite snowflake reference. The
Python SDK exposes both forms as `User.handle` and `User.ref`; snowflakes are
never decoded into usernames.

An application page manages:

- display name, description, support and privacy URLs
- requested REST scopes, Gateway intents, and permissions
- scoped control credentials
- Ed25519 worker keys and exact target-domain delegation
- global slash and context command definitions
- guild invite templates
- exact-instance allow and deny rules
- active and suspended installations

The web page shows whether encrypted participation is enabled but can't change
it. To turn on the `participant` E2EE mode, use the mobile Developer Portal's
**Encrypted interaction modes** setting or `PATCH /api/v1/applications/{app}`.
Neither portal edits guild-specific command sets; publish those through the API
or with `Client.sync_commands(guild=...)`.

Control credentials are shown once and stored as hashes. They carry only
`workers.manage` and `commands.manage`, and can't authenticate a user session,
a bot REST request or a Gateway connection. Revoking one blocks future control
operations immediately.

Workers generate their Ed25519 private key locally, and only the public key is
enrolled. A worker exchanges a short assertion for an eight-minute target
token. Every REST or Gateway request carries a nonce-based DPoP proof bound to
the HTTP method, path, query, token and worker key.

### App Directory

On the web and desktop, you can open **App Directory** from a server in three
ways: right-click it in the server rail and hover **Server Settings**, use the
server menu, or open **Server Settings → App Directory**. The standalone
Directory page is only on desktop and the web. On mobile, people find reviewed
apps through the chat-bar App Launcher, which opens the native **Add App**
review.

To list an application, an owner opts in from the Developer Portal and
provides:

- a short summary, a category, one to five unique tags and a full description;
- HTTPS support, privacy-policy and terms links;
- at least one active install template.

The product page can also show up to five uploaded images and allowlisted
YouTube video IDs in total, in an order you choose, up to five named HTTPS
links, the
Discord locales the app supports, and a localized description for any declared
locale. A user-install-only listing uses the same consent template as a guild
listing, but it can claim user-install support only after it publishes an
active global user-install command.

Kaede adds up to five popular global slash commands, based on use at the app's
home instance, and up to three reviewed apps with overlapping categories and
tags. **Copy link** copies the product page's shareable URL.

Changing the app's public identity, discovery details, install support or media
order withdraws the existing approval until an instance reviewer approves the
new version.

Team members can open **Directory listing** to check **Discovery status** and
the **Product page preview** before the listing is approved or the checklist is
complete. The preview shows only what's been filled in, without placeholder
content, and a readiness checklist shows what's still missing. Team members on
other instances see the same preview.

Staff with `bots.manage` review opted-in applications in **Administration →
Applications**. Approving or removing a listing, or placing it in the
`featured`, `staff-picks` or `new-and-noteworthy` collection, needs a visible
reason, which is kept in the audit log. A remote application's approval comes
from its home instance; you can apply your own instance policy to it but can't
approve it yourself.

Directory search covers your own instance's catalog or one federated domain
you choose. Results come back a page at a time. The app's home instance applies
the developer's target policy and the requesting instance's block status, and
silenced or suspended instances can't browse its catalog.

The **App Launcher** sits to the right of the composer on web and desktop, and
to the left of it on mobile. It shows your recent apps for the signed-in
account, commands from current installations, reviewed collections, and a
search of the selected instance's whole catalog. The optional **Directory
instance** control defaults to your home instance but accepts one federated
domain; malformed domains, and results from any other instance, are rejected.
An app with no command in the current channel opens its install review instead
of showing an empty row. A bot's chat or member profile also shows **Add App**,
using its active template even when the app isn't publicly listed. Bots never
show friend controls.

### Guild installation

Install templates produce shareable links at:

```text
/applications/{application-ref}/install/{template-slug}
```

The invite card shows the application's home instance, bot identity, requested
scopes, intents, named guild permissions, privacy and support links, and E2EE
disclosure. The guild picker lists only guilds where you have Manage Guild or
Administrator, and the guild's home instance rechecks that permission before
installing.

The same link works for a guild on another Kaede instance. That instance
fetches a signed manifest from the application's home, applies both
instances' federation policy, creates the bot member and role, and stores the
installation grant. Bot workers then connect directly to that instance;
runtime traffic isn't routed through the application's home.

Guild administrators manage installed bots at `/g/{guild-ref}/integrations`.
Removing a bot revokes its future access, removes its member and role, and
notifies a remote application home when needed.

### Commands and E2EE

Commands use a bounded, Discord-style schema. Option types include
subcommands, strings, integers, numbers, booleans, users, channels, roles,
mentionables and attachments. Kaede validates names, descriptions, choices,
numeric and string bounds, option counts, nesting depth and the total size of
the command set.

Installed slash commands appear in the channel composer. The guild's home
instance checks permissions on each use and delivers the interaction over the
bot's direct Gateway connection. A bot must respond or defer within 3 seconds;
the interaction token then stays valid for 15 minutes.

In plaintext channels, content and history require their own scope and the
`message_content` intent. In an E2EE channel, the server can't
provide content or history. A guild administrator can separately admit a
`participant` installation to an encrypted channel. Admission snapshots the
app's verified devices, rotates the room keys, and gives the bot only history
from after admission. Until a verified bot device is admitted, encrypted
payloads stay opaque and no API falls back to plaintext. A bot operator that
receives room keys becomes a recipient and may keep anything the bot decrypts.

### Developer API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET/POST` | `/api/v1/developer-teams` | List or create teams |
| `GET/POST` | `/api/v1/developer-teams/{team}/members` | List or add members |
| `PATCH/DELETE` | `/api/v1/developer-teams/{team}/members/{user}` | Change or remove a member |
| `GET/POST` | `/api/v1/applications` | List or create applications |
| `GET/PATCH` | `/api/v1/applications/{app}` | Read or change an application |
| `GET` | `/api/v1/applications/{app}/directory-preview` | Read the private product preview and readiness status |
| `GET/POST` | `/api/v1/applications/{app}/credentials` | List or create control credentials |
| `DELETE` | `/api/v1/applications/{app}/credentials/{credential}` | Revoke a credential |
| `GET/POST` | `/api/v1/applications/{app}/workers` | List or enroll workers |
| `DELETE` | `/api/v1/applications/{app}/workers/{worker}` | Revoke a worker |
| `GET/PUT` | `/api/v1/applications/{app}/commands` | Read or replace global commands |
| `GET/PUT` | `/api/v1/applications/{app}/guilds/{guild}/commands` | Read or replace one guild's commands |
| `GET/POST` | `/api/v1/applications/{app}/install-templates` | List or create invite links |
| `GET` | `/api/v1/applications/{app}/installations` | List installations |
| `GET` | `/api/v1/applications/{app}/instance-rules` | List exact-domain policy rules |
| `PUT/DELETE` | `/api/v1/applications/{app}/instance-rules/{domain}` | Set or remove an exact-domain rule |
| `GET/POST` | `/api/v1/applications/{app}/assets` | List or commit application assets |
| `POST` | `/api/v1/applications/{app}/assets/tickets` | Reserve an application asset upload |
| `GET/PATCH/DELETE` | `/api/v1/applications/{app}/assets/{asset}` | Read, update, or remove an asset |
| `GET/POST` | `/api/v1/applications/{app}/emojis` | List or commit application emoji |
| `POST` | `/api/v1/applications/{app}/emojis/tickets` | Reserve an application emoji upload |
| `GET/PATCH/DELETE` | `/api/v1/applications/{app}/emojis/{emoji}` | Read, update, or remove app emoji |
| `GET` | `/api/v1/application-directory` | Search a local or selected remote catalog |
| `GET` | `/api/v1/application-directory/bot-profiles/{bot}` | Resolve a bot profile's authority-owned Add App action |
| `GET` | `/api/v1/application-directory/{app}` | Read a reviewed application product page |
| `POST` | `/api/v1/bot-control/applications/{app}/workers` | Enroll using a control token |
| `PUT` | `/api/v1/bot-control/applications/{app}/commands` | Sync global commands using a control token |
| `PUT` | `/api/v1/bot-control/applications/{app}/guilds/{guild}/commands` | Sync one guild's commands using a control token |

See [Bot API quickstart](bot-api-quickstart.md) for the Python SDK and
[Bot and automation API](bots-and-automations.md) for runtime routes, Gateway
events, rate limits, federation, and E2EE behavior.

Target tokens, like control credentials, are stored as hashes. Database
migrations run through the deployment's `migrate` service. See the
[operator guide](operator.md) for upgrades and backups.

### Installing vendor extensions

Install trusted packages under `vendor/extensions/<name>/extension.py`. This directory is ignored by Git. Back it up and distribute it separately. For Kubernetes set `KAEDE_VENDOR_DIR` to the absolute directory containing the packages on each API node, then redeploy and restart the API. Packages execute with the API process's permissions. Install them through your deployment process.

Each entry point exports an `app.vendor.Extension`. Its title and description identify a dashboard pane. Declare text or checkbox fields with a unique key and label. The validator accepts the edited list of records and returns normalized records or raises `ValueError` with a message for the administrator. Configuration is versioned per instance. For task buttons supply an `admin_router` and matching `actions` pairs of route name and label. Buttons POST to the package's named subroute. The connector enforces the extensions management capability on every route. Link packages can return exact-host HTTPS query rules from `link_rules(records, platform)`. Client-visible rules must contain only public values.

With the affiliate package installed, open **Administration → Shopping links**, add a retailer hostname, its query parameter and your shared affiliate code. Select Web, Desktop (Tauri), or Mobile app per program. Selecting no platform pauses the entry. Existing values of that parameter are preserved unless you select **Replace an existing affiliate code**. Confirm the program permits each selected platform and any replacement. Use separate entries for separate hostnames.
