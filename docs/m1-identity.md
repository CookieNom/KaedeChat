# Identity and authentication

User handles are immutable `username@home-domain` identifiers. Email addresses
and passwords stay private to the home instance; federation and chat APIs do
not rely on email addresses.

## Account lifecycle

- Registration enforces case-insensitive username uniqueness. Email-enabled
  instances also require a case-insensitively unique address; email-disabled
  instances create username-and-password accounts without collecting one.
- Email verification uses expiring, one-time, hashed tokens.
- Verification resend is uniform and rate-limited. Issuing a new one-time
  credential invalidates the previous active credential for the same account
  and purpose.
- Login accepts a username, an email, or the full local handle.
- Passwords are hashed with Argon2id (`m=65536`, `t=3`, `p=1`). API hashing and
  verification run in a dedicated thread, with one 64 MiB operation admitted
  per API process — the async event loop is never blocked and there is no
  unbounded worker pool.
- Access tokens are opaque and live in Dragonfly. Refresh sessions rotate and
  are stored in PostgreSQL.
- Refresh-token reuse detection revokes every access token for the session.
- Refresh expiry is both sliding and absolute.
- MFA covers TOTP setup, login challenges, and ten single-use recovery codes.
  Each account has one active MFA login challenge at a time, and failure
  windows are enforced across replacement tickets by account and source
  address.
- Password reset and confirmed email changes are available when email delivery
  is enabled. Logout and session revocation work in every mode.
- Display name, biography, custom status, profile media, and user settings all
  support authenticated reads and updates.
- Email delivery (console, SMTP, or the Mailtrap API) goes through an
  encrypted, transactional PostgreSQL outbox. Taskiq provides content-free wake
  signals; workers claim safely across replicas and retry with bounded backoff
  until the one-time token expires. A minute sweep recovers missed wakes.
- Purge tasks remove expired tokens, sessions, and unverified registrations.

The console backend is development/test-only and deliberately prints complete
messages, including one-time links, as structured output. Production settings
reject it. Production operators may use SMTP, Mailtrap, or disable email
entirely. Disabled mode also disables verification, email changes, and
self-service password recovery; accounts created in that mode can sign in
immediately with their username and password. Downgrading past the
optional-email migration is refused while any local account without an email
exists.

The SvelteKit static application supplies registration, verification, login,
forgot-password, reset-password, and authenticated-shell routes.

## Client authentication

Browser clients receive `kc_access` and `kc_refresh` as HttpOnly cookies. A
state-changing request authenticated by a cookie must include
`X-Kaede-Client: web`. The refresh cookie is scoped to `/api/v1/auth` and uses
`SameSite=Strict`; the access cookie uses `SameSite=Lax`. Production cookies
are also `Secure`.

Native clients send `X-Kaede-Client: mobile`, receive tokens in the response
body, and use `Authorization: Bearer <token>`. When both a bearer token and a
cookie are present, the explicit bearer token wins and cookie CSRF rules do not
apply. Likewise, a refresh token supplied in the request body takes precedence
over an ambient browser cookie.

Authentication failures do not reveal whether an account exists; this is
deliberate. Unknown-account logins still perform an Argon2 verification, and
login rate limiting uses hashed account identifiers and source addresses in
Dragonfly.

Starting MFA requires the current password. Replacing an enabled factor
requires both the current password and a current TOTP or recovery code, and
pending setup material is bound to that authenticated session and to the
password/factor state that authorized it. Enabling, replacing, or disabling MFA
keeps the session that performed reauthentication and revokes every other
refresh session — the user keeps a stable response path while older browser and
device access is removed.

## Validation

`make check` runs backend lint, formatting, typing, and unit tests, then the
frontend side: lint, formatting, Svelte diagnostics, Vitest, and a static
production build. It does not publish host ports.

`make identity-check` creates an isolated Compose project with only disposable
PostgreSQL and Dragonfly services, then migrates and bootstraps a fresh
instance. It verifies registration, email verification, all client
authentication modes, CSRF enforcement, settings, MFA, refresh rotation and
reuse, and password reset. The gate also drains the real SQL email outbox from
concurrent workers, confirms a delivered row is not resent, and checks that
neither the recipient nor the bearer token appears in its stored ciphertext.
Its trap removes the containers, network, and volumes on success or failure.
