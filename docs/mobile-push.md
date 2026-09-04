# Mobile push delivery

Kaede separates chat federation from mobile push identity. The official mobile
app can connect to any compatible home, while its closed-app notifications use
the Kaede-operated relay at `push.kaede.chat`. Your home does not need the
official Firebase or APNs credentials.

Push is a wake mechanism, not a message transport. Chat and federation keep
working when the relay or a platform provider is unavailable.

## Delivery choices

| Choice | Intended use | Provider credentials |
| --- | --- | --- |
| Official relay | Official Kaede Android/iOS distribution | Held only by the Kaede relay |
| Custom relay | Community distribution serving several homes | Held only by that relay operator |
| Direct FCM | One operator's separately signed custom app | Held by that app's home worker |
| Disabled | Foreground-only notifications | None |

The package ID `chat.kaede.mobile` and its signing/update lineage are reserved
for official releases. A community build uses its own package/bundle ID,
signing identity, Firebase/APNs application, and relay pin. Credentials for one
mobile application cannot notify another application.

## Registration

The official app pins the relay URL, logical relay origin, and mobile
application ID at build time. A home cannot redirect its provider token.

1. The user chooses background notifications before the operating-system
   permission prompt.
2. The app creates a random route ID, wake-authentication secret, and relay
   management secret in Android Keystore or iOS protected app-group storage.
   iOS creates a second, independently scoped route for VoIP calls.
3. The authenticated app asks its home for a five-minute enrollment grant. The
   home signs the relay audience, app ID, platform, and route ID. The grant
   includes no Kaede user or room identifier.
4. The app sends that grant and its FCM token directly to the pinned relay.
   iOS repeats enrollment with its PushKit token for calls. The relay verifies
   the home's federation key and stores each provider token encrypted.
5. The relay returns an opaque subscription and a signed receipt. The app gives
   only that receipt, subscription, route ID, and wake secret to its home.
6. The home verifies the relay receipt and stores the subscription and wake
   secret. It never receives the official provider token.

Provider-token refresh repeats enrollment. The relay permits separate opaque
routes for the same provider token, so a future multi-account session vault can
deliver to more than one home without changing the wire protocol. The current
mobile app keeps one active account at a time. Logout removes that home binding
and attempts both device-authorized and signed-home revocation; subscriptions
also expire if both final revocations are lost.

## Wake delivery

After committing an eligible message projection, the recipient home writes a
content-free wake to a durable outbox. The wake contains random request,
delivery, route, event, and subscription values plus an expiry and HMAC. It has
no user, sender, guild, channel, message, attachment, or notification-kind
field.

The home signs the HTTP request with its normal Kaede federation key. The relay
binds the subscription to that signing origin, writes the wake to its durable
provider queue, and returns `202` only after that commit. Requests are
idempotent by home origin and request ID. Both queues retry with bounded
backoff until the ten-minute wake expires.

The relay forwards a version 2 data payload containing only:

```json
{
  "sync_version": "2",
  "route_id": "opaque",
  "event_token": "opaque",
  "delivery_id": "opaque",
  "expires_at": "epoch-seconds",
  "wake_mac": "opaque"
}
```

The relay never receives the wake secret, so it cannot invent a wake the app
will accept. The app or signed notification extension verifies the route,
expiry, and constant-time HMAC before contacting the home. The wake proof can
redeem only its single-use, ten-minute event token; the login session is not
shared with extensions. Before returning display details, the home rechecks
device ownership, current room access, blocks, read state, DND, guild settings,
message deletion, and self-authorship. On iOS, ordinary APNs alerts carry a
private generic fallback which the Notification Service Extension replaces
with these details. VoIP wakes are accepted only for urgent call deliveries
and are reported to CallKit immediately.

An already-used, expired, suppressed, malformed, or unauthenticated wake is
silent. A generic local fallback appears only for an authentic wake whose home
temporarily cannot be reached.

## Privacy

| Party | Information available |
| --- | --- |
| Recipient home | Account, preferences, eligible event, device binding, relay choice |
| Relay | Verified home origin, opaque subscription/route, provider token, platform, timing, network metadata, provider result |
| FCM/APNs | App identity, provider token/device, timing/network/delivery metadata, opaque wake fields |
| Remote sender or guild | No device, provider, or relay information |
| Device | Details fetched from its authenticated home; E2EE plaintext only after local decryption |

Relay subscription and delivery tables contain no Kaede account, guild,
channel, or message IDs. Provider tokens are encrypted at rest. Application
logs must redact provider tokens, subscriptions, enrollment grants, receipts,
wake fields, and device secrets. Completed provider outcomes are retained for
one day for idempotency; retention jobs purge expired subscriptions and queued
wakes.

## End-to-end encrypted rooms

The relay path is unchanged for E2EE because it carries only an opaque wake.
When the home cannot safely produce plaintext, it returns the generic text
`New encrypted message` and no sender profile. A device may replace that with a
local preview only after retrieving ciphertext and decrypting with device-held
keys.

Kaede's current encrypted envelope may include explicit recipient mention
references for notification routing. Those references are visible to the
participating homes but never enter the relay or provider payload. A future
metadata-private encryption mode will need an authenticated per-recipient hint.
Without one it can offer generic all-message wakes or no closed-app wakes, not
mention-only delivery.

## Home configuration

Recommended official-app configuration:

```dotenv
KAEDE_PUSH_RELAY_ENABLED=true
KAEDE_PUSH_RELAY_URL=https://push.kaede.chat
KAEDE_PUSH_RELAY_ORIGIN=kaede.chat
KAEDE_PUSH_RELAY_APP_ID=chat.kaede.mobile
KAEDE_PUSH_RELAY_SERVICE_ENABLED=false
KAEDE_PUSH_ENABLED=false
```

The official relay operator additionally sets:

```dotenv
KAEDE_PUSH_RELAY_SERVICE_ENABLED=true
KAEDE_PUSH_RELAY_FCM_SERVICE_ACCOUNT_B64=<base64 service-account JSON>
KAEDE_PUSH_RELAY_APNS_KEY_B64=<base64 APNs .p8 contents>
KAEDE_PUSH_RELAY_APNS_KEY_ID=<Apple key ID>
KAEDE_PUSH_RELAY_APNS_TEAM_ID=<Apple team ID>
KAEDE_PUSH_RELAY_APNS_TOPIC=chat.kaede.mobile.voip
```

Only relay workers receive those credentials. FCM delivers ordinary Android
and iOS notifications. APNs VoIP wakes launch PushKit and CallKit for incoming
iOS calls. Don't set either provider credential on an ordinary home.
A custom build can instead use `KAEDE_PUSH_ENABLED=true` with
`KAEDE_PUSH_FCM_SERVICE_ACCOUNT_B64`, but that legacy/direct transport is not
compatible with the official store app.

## Existing deployment conversion

1. Apply the Alembic migration. Existing `push_devices` become `direct_fcm`
   devices and keep working during the client transition.
2. Enable the relay variables on each home and restart the API, worker,
   scheduler, and preflight services.
3. Configure Firebase and, when supporting iOS calls, APNs credentials only on
   the relay operator.
4. Ship an official app containing its matching Firebase client file and relay
   pin. When users next enable or refresh notifications, the app registers with
   the relay and replaces its direct device row.
5. After active devices have converted, set `KAEDE_PUSH_ENABLED=false` and
   remove direct-FCM credentials from homes.

No chat data migration or federation outage is required.

## Failure and abuse handling

- Relay failure never rolls back a message or federation transaction.
- A `410` disables the stale device binding; users can re-enable it in
  settings.
- A `429` includes `Retry-After`; the home keeps the same idempotency key.
- Registration and wakes are limited by source, authenticated home, strict
  payload size, and relay capacity.
- Suspended federation origins cannot register or deliver wakes. Relay policy
  may independently quarantine an abusive home.
- Calls require a separate urgent lane before call push is enabled.
- Metrics use aggregate/status labels rather than unbounded home domains.

Monitor home outbox age, relay queue age, provider status, invalid-token rates,
signature/replay rejection, `429` responses, and key/certificate expiry.
Closed-app delivery failures should be visible in mobile settings, but must not
become persistent warnings when a user intentionally disabled push.

## User controls

The settings screen identifies the home and relay before opt-in, explains the
metadata boundary, and provides a per-account background-delivery control.
Disabling it unregisters the home binding. The app remains fully usable and
receives gateway alerts while open.

The diagnostic view should show distribution channel, package/bundle ID, home,
push transport, relay/provider host, and connection state. It must never show a
provider token, subscription, grant, HMAC, or management secret.
