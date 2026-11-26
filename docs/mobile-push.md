# Mobile push delivery

The official mobile app works with any compatible home, but its closed-app
notifications go through the Kaede-operated relay at `push.kaede.chat`, so your
home doesn't need Firebase or APNs credentials. Push only wakes the app. Chat
and federation keep working if the relay or a provider is down.

## Delivery choices

| Choice | Intended use | Provider credentials |
| --- | --- | --- |
| Official relay | Official Kaede Android/iOS distribution | Held only by the Kaede relay |
| Custom relay | Community distribution serving several homes | Held only by that relay operator |
| Direct FCM | One operator's separately signed custom app | Held by that app's home worker |
| Disabled | Foreground-only notifications | None |

The package ID `chat.kaede.mobile` and its signing and update lineage are
reserved for official releases. A community build needs its own package/bundle
ID, signing identity, Firebase/APNs application, and relay pin. One app's
credentials can't notify another app.

## Home configuration

For the official app, set:

```dotenv
KAEDE_PUSH_RELAY_ENABLED=true
KAEDE_PUSH_RELAY_URL=https://push.kaede.chat
KAEDE_PUSH_RELAY_ORIGIN=kaede.chat
KAEDE_PUSH_RELAY_APP_ID=chat.kaede.mobile
KAEDE_PUSH_RELAY_SERVICE_ENABLED=false
KAEDE_PUSH_ENABLED=false
```

Don't set any provider credentials on an ordinary home. Only the official relay
operator adds these, and only on relay workers:

```dotenv
KAEDE_PUSH_RELAY_SERVICE_ENABLED=true
KAEDE_PUSH_RELAY_FCM_SERVICE_ACCOUNT_B64=<base64 service-account JSON>
KAEDE_PUSH_RELAY_APNS_KEY_B64=<base64 APNs .p8 contents>
KAEDE_PUSH_RELAY_APNS_KEY_ID=<Apple key ID>
KAEDE_PUSH_RELAY_APNS_TEAM_ID=<Apple team ID>
KAEDE_PUSH_RELAY_APNS_TOPIC=chat.kaede.mobile.voip
```

FCM delivers ordinary Android and iOS notifications. APNs VoIP wakes start
PushKit and CallKit for incoming iOS calls. The deployment derives the API's
non-secret `KAEDE_PUSH_RELAY_VOIP_AVAILABLE` flag from the configured APNs key.
Custom deployments with separate API and worker processes must set that flag to
`true` on the API when their relay worker has APNs configured. Don't copy the
key into the API process.

A custom build can use `KAEDE_PUSH_ENABLED=true` with
`KAEDE_PUSH_FCM_SERVICE_ACCOUNT_B64` instead. This legacy direct transport
doesn't work with the official store app. Client build settings are in the
[mobile README](../mobile/README.md#background-notification-builds).

## Existing deployment conversion

1. Apply the Alembic migration. Existing `push_devices` become `direct_fcm`
   devices and keep working while clients move over.
2. Enable the relay variables on each home and restart the API, worker,
   scheduler, and preflight services.
3. Configure Firebase, and APNs if you support iOS calls, only on the relay
   operator.
4. Ship an official app with its matching Firebase client file and relay pin.
   When users next enable or refresh notifications, the app registers with the
   relay and replaces its direct device row.
5. Once active devices have converted, set `KAEDE_PUSH_ENABLED=false` and remove
   direct-FCM credentials from homes.

This needs no chat data migration or federation outage.

## Failure and abuse handling

| Situation | Behavior |
| --- | --- |
| Relay failure | Never rolls back a message or federation transaction. |
| `410` on the ordinary route | Disables the stale device binding. Users can re-enable it in settings. |
| `410` on the iOS VoIP route | Clears only that route. The device stays enabled, and calls arrive as ordinary urgent alerts until the app registers a new VoIP route. |
| `429` | Includes `Retry-After`. The home keeps the same idempotency key. |
| Abuse | Registration and wakes are limited by source, authenticated home, strict payload size, and relay capacity. Suspended federation origins can't register or deliver, and operators can suspend an abusive home through federation policy. |

Calls share the same queues as other wakes. Metrics use aggregate and status
labels, not unbounded home domains.

Monitor:

- home outbox age and relay queue age
- provider status and invalid-token rates
- signature and replay rejections, and `429` responses
- key and certificate expiry

Closed-app delivery failures should show in mobile settings, but must not turn
into persistent warnings when a user has turned push off.

## User controls

After sign-in, an account with no notification choice yet is asked once per
device (remembered across sign-out) whether to enable notifications. Setup
failures stay in the prompt with a retry button. Settings name the home and
relay, explain what metadata each sees, and give each account its own
background-delivery switch. Turning it off unregisters the home binding; the
app still gets gateway alerts while open.

After a setup failure, settings can copy diagnostics with the platform,
transport, failed step, and error code, never a provider token, subscription,
grant, HMAC, or management secret.

## Collecting mobile debug logs

In Android or iOS **Settings → Troubleshooting**, turn on **Debug logging**,
reproduce the problem, and choose **Copy debug log**. For startup or Apple push
registration problems, turn logging on first and then restart the app.

The setting is remembered on the device. Logs stay in memory, keep only recent
events, and reset on restart, **Clear log**, or turning logging off. Nothing is
uploaded automatically. Reports include the app version, startup and connection
events, API status codes, and notification setup errors. iOS reports also
include native Apple registration callbacks and token-presence flags. Logs never
contain message content, passwords, tokens, request URLs, or request/response
bodies.

## Registration

The official app pins the relay URL, logical relay origin, and mobile app ID at
build time, so a home can't redirect its provider token.

1. The user opts in to background notifications before the system permission
   prompt.
2. The app creates a random route ID, wake-authentication secret, and relay
   management secret in Android Keystore or iOS protected app-group storage.
   iOS creates a second, separately scoped route for VoIP calls.
3. The app asks its home for a five-minute enrollment grant. The home signs the
   relay audience, app ID, platform, and route ID. The grant contains no Kaede
   user or room identifier.
4. The app sends the grant and its FCM token straight to the pinned relay. iOS
   enrolls again with its PushKit token for calls. The relay checks the home's
   federation key and stores each provider token encrypted.
5. The relay returns an opaque subscription and a signed receipt. The app gives
   its home only the receipt, subscription, route ID, and wake secret.
6. The home verifies the receipt and stores the subscription and wake secret.
   It never sees the official provider token.

A provider-token refresh repeats enrollment. The relay allows several opaque
routes per token, though the app currently has one active account at a time.
Logout removes that home binding and tries both device-authorized and
signed-home revocation. If both are lost, the subscription still expires.

## Wake delivery

After committing an eligible message, the recipient home writes a content-free
wake to a durable outbox. The wake holds random request, delivery, route, event,
and subscription values, an expiry, and an HMAC. It has no user, sender, guild,
channel, message, or attachment field. A `priority` of `urgent` marks calls
(see [Privacy](#privacy) for who can tell).

The home signs the request with its normal Kaede federation key. The relay
binds the subscription to that signing origin, commits the wake to its durable
provider queue, and only then returns `202`. Requests are idempotent by home
origin and request ID. Both queues retry with bounded backoff until the
ten-minute wake expires.

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

The relay never has the wake secret, so it can't forge a wake the app will
accept. The app, or its signed notification extension, checks the route, the
expiry, and the HMAC (in constant time) before contacting the home. The wake
can only redeem its single-use, ten-minute event token. Extensions never get the
login session.

Before returning display details, the home rechecks device ownership, current
room access, blocks, read state, DND, guild settings, message deletion, and
self-authorship. On iOS, ordinary APNs alerts carry a generic private fallback
that the Notification Service Extension replaces with these details. VoIP wakes
are accepted only for urgent call deliveries and go straight to CallKit.

A wake can be already used, expired, suppressed, malformed, or unauthenticated.
On Android these are silent, and a generic local notification appears only for
an authentic wake whose home can't be reached right now. On iOS the APNs alert
has already been shown, so the extension shows its generic text in all of these
cases.

## Privacy

| Party | Information available |
| --- | --- |
| Recipient home | Account, preferences, eligible event, device binding, relay choice |
| Relay | Verified home origin, opaque subscription/route, provider token, platform, timing, network metadata, provider result. It also sees whether a wake is a call (`urgent`) or other activity (`normal`), and iOS calls use the VoIP route when one is registered. |
| FCM/APNs | App identity, provider token/device, timing/network/delivery metadata, opaque wake fields. On iOS, a call arrives as a VoIP push or an "Incoming call" alert, and other activity as a "New activity" alert. Android wakes look the same for both. |
| Remote sender or guild | No device, provider, or relay information |
| Device | Details fetched from its authenticated home; E2EE plaintext only after local decryption |

Relay subscription and delivery tables hold no Kaede account, guild, channel,
or message IDs, and provider tokens are encrypted at rest. Logs must redact
provider tokens, subscriptions, enrollment grants, receipts, wake fields, and
device secrets. Completed provider outcomes are kept for one day for
idempotency. Retention jobs purge expired subscriptions and queued wakes.

## End-to-end encrypted rooms

E2EE doesn't change the relay path, because the relay only carries an opaque
wake. When the home can't safely produce plaintext, it returns the generic text
`New encrypted message` with no sender profile. The device may replace that
with a local preview after fetching the ciphertext and decrypting it with its
own keys.

The encrypted envelope may include recipient mention references for
notification routing. Participating homes can see them, but they never reach
the relay or provider payload.
