# Identity and authentication

User handles are immutable
`username@home-domain` identifiers; email addresses and passwords remain private
to the home instance. Federation and chat APIs do not rely on email addresses.

## Account lifecycle

- Registration with case-insensitive username uniqueness. Email-enabled
  instances also require a case-insensitively unique address; email-disabled
  instances create username-and-password accounts without collecting one.
- Email verification using expiring, one-time, hashed tokens.
- Uniform, rate-limited verification resend; issuing a new one-time credential
  invalidates the previous active credential for the same account and purpose.
- Login by username, email, or full local handle.
- Argon2id password hashing with `m=65536`, `t=3`, and `p=1`. API hashing and
  verification run in a dedicated thread with one 64 MiB operation admitted per
  API process, rather than blocking the async event loop or using an unbounded
  worker pool.
- Opaque access tokens in Dragonfly and rotating refresh sessions in PostgreSQL.
- Refresh-token reuse detection that revokes every access token for the session.
- Sliding and absolute refresh expiry.
- TOTP setup, login challenges, and ten single-use recovery codes.
- One active MFA login challenge per account, with failure windows enforced across
  replacement tickets by account and source address.
- Password reset and confirmed email changes when email delivery is enabled,
  plus logout and session revocation in every mode.
- Authenticated display-name, biography, custom-status, profile-media, and
  user-settings reads and updates.
- Console, SMTP, and Mailtrap API email delivery through an encrypted,
  transactional PostgreSQL outbox. Taskiq provides content-free wake signals;
  workers claim safely across replicas and retry with bounded backoff until the
  one-time token expires, while a minute sweep recovers missed wakes.
- Purge tasks for expired tokens, sessions, and unverified registrations.

The console backend is development/test-only and intentionally prints complete
messages (including one-time links) as structured output. Production settings
reject it; production operators may use SMTP, Mailtrap, or explicitly disable
email. Disabled mode also disables verification, email changes, and
self-service password recovery; accounts created in that mode can sign in
immediately with their username and password. Downgrading past the optional-email
migration is refused while any local account without an email exists.

The SvelteKit static application supplies registration, verification, login,
forgot-password, reset-password, and authenticated-shell routes.

## Client authentication

Browser clients receive `kc_access` and `kc_refresh` as HttpOnly cookies. A
state-changing request authenticated by a cookie must include
`X-Kaede-Client: web`. The refresh cookie is scoped to `/api/v1/auth` and uses
`SameSite=Strict`; the access cookie uses `SameSite=Lax`. Production cookies are
also `Secure`.

Native clients send `X-Kaede-Client: mobile`, receive tokens in the response
body, and use `Authorization: Bearer <token>`. If both a bearer token and a
cookie are present, the explicit bearer token takes precedence and cookie CSRF
rules do not apply. A refresh token explicitly supplied in the request body also
takes precedence over an ambient browser cookie.

Authentication failures intentionally avoid revealing whether an account
exists. Unknown-account logins still perform an Argon2 verification, and login
rate limiting uses hashed account identifiers and source addresses in
Dragonfly.

Starting MFA requires the current password. Replacing an enabled factor requires
both the current password and a current TOTP or recovery code; pending setup
material is bound to that authenticated session and to the password/factor state
that authorized it. Enabling, replacing, or disabling MFA keeps the session that
performed reauthentication and revokes every other refresh session. This gives
the user a stable response path while removing older browser and device access.

## Validation

`make check` runs backend lint, formatting, typing, and unit tests followed by
frontend lint, formatting, Svelte diagnostics, Vitest, and a static production
build. It does not publish host ports.

`make identity-check` creates an isolated Compose project with only disposable
PostgreSQL and Dragonfly services. It migrates and bootstraps a fresh instance,
then verifies registration, email verification, all client authentication modes,
CSRF enforcement, settings, MFA, refresh rotation/reuse, and password reset. The
gate also drains the real SQL email outbox from concurrent workers, verifies a
delivered row is not resent, and checks that neither the recipient nor bearer
token appears in its stored ciphertext. Its trap removes the containers, network,
and volumes on success or failure.
