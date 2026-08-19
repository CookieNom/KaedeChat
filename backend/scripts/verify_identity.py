from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pyotp
from sqlalchemy import select

import app.api.auth as auth_api
from app.core.settings import get_settings
from app.db.models import EmailOutbox, User
from app.email.backends import OutboundEmail
from app.email.outbox import drain_email_outbox
from app.main import app
from app.tasks import purge_unverified_accounts_in_session
from scripts.email_tokens import token_from_email
from scripts.verification import (
    PASSWORD_KDF_VERSION,
    VerificationFailure,
    authentication_secret,
    failure_message,
    password_kdf_metadata,
    require,
)

PASSWORD = "correct horse battery staple"  # noqa: S105 - disposable validation credential
NEW_PASSWORD = "a new correct horse password"  # noqa: S105 - disposable validation credential
AUTH_SALT = bytes(range(16))
VAULT_SALT = bytes(reversed(range(16)))
RESET_AUTH_SALT = bytes(range(16, 32))


async def verify() -> None:
    emails: list[tuple[str, str, str]] = []

    class CaptureEmailBackend:
        async def send(self, message: OutboundEmail) -> None:
            emails.append((message.to, message.subject, message.text))

    async def suppress_immediate_wake() -> None:
        """Exercise the scheduler-recovery path without an identity-check worker."""

    auth_api.wake_email_outbox = suppress_immediate_wake

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):  # noqa: SIM117
        settings = get_settings()
        password_secret = authentication_secret(PASSWORD, settings.domain, AUTH_SALT)
        new_password_secret = authentication_secret(
            NEW_PASSWORD,
            settings.domain,
            RESET_AUTH_SALT,
        )

        async def drain_mail() -> dict[str, int]:
            return await drain_email_outbox(
                app.state.sessionmaker,
                settings,
                backend=CaptureEmailBackend(),
            )

        async with httpx.AsyncClient(transport=transport, base_url="http://kaede.test") as client:
            register = await client.post(
                "/api/v1/auth/register",
                json={
                    "username": "maple",
                    "email": "maple@example.com",
                    "password": password_secret,
                    "password_kdf": password_kdf_metadata(
                        AUTH_SALT,
                        vault_salt=VAULT_SALT,
                    ),
                },
            )
            require(register.status_code == 201, f"registration failed: {register.text}")
            require(register.json()["handle"] == "maple@schema.localhost", "handle mismatch")
            async with app.state.sessionmaker() as database_session:
                pending_email = await database_session.scalar(select(EmailOutbox))
                if pending_email is None:
                    raise VerificationFailure(
                        "registration returned success but committed no "
                        "verification-email outbox row"
                    )
                initial_ciphertext = pending_email.encrypted_payload
                require(
                    b"maple@example.com" not in initial_ciphertext,
                    "outbox exposed recipient PII at rest",
                )
            concurrent_drains = await asyncio.gather(drain_mail(), drain_mail())
            require(
                sum(result["delivered"] for result in concurrent_drains) == 1,
                "concurrent workers did not claim exactly one delivery",
            )
            require(len(emails) == 1, "verification email was not delivered")
            first_token = token_from_email(emails[-1][2])
            require(
                first_token.encode() not in initial_ciphertext,
                "outbox exposed a bearer token at rest",
            )
            await drain_mail()
            require(len(emails) == 1, "delivered outbox row was sent twice")

            # A resend extends the credential lifetime, so the account cleanup
            # task must not delete the user at the original registration cutoff.
            async with app.state.sessionmaker() as database_session:
                registered_user = await database_session.scalar(
                    select(User).where(User.username == "maple")
                )
                if registered_user is None:
                    raise VerificationFailure(
                        "the registered user row disappeared before account-expiry validation"
                    )
                registered_user.created_at = datetime.now(UTC) - timedelta(
                    hours=settings.verification_ttl_hours, minutes=1
                )
                await database_session.commit()

            superseded_verification = first_token
            resend = await client.post(
                "/api/v1/auth/verify-email/resend",
                json={"email": "maple@example.com"},
            )
            require(resend.status_code == 202, f"verification resend failed: {resend.text}")
            await drain_mail()
            require(len(emails) == 2, "replacement verification email was not queued")
            async with app.state.sessionmaker() as database_session:
                purged = await purge_unverified_accounts_in_session(database_session, settings)
                require(purged == 0, "fresh verification resend did not defer account purge")
            superseded = await client.post(
                "/api/v1/auth/verify-email",
                json={"token": superseded_verification},
            )
            require(superseded.status_code == 400, "superseded verification token remained valid")
            verification = token_from_email(emails[-1][2])
            verified = await client.post("/api/v1/auth/verify-email", json={"token": verification})
            require(verified.status_code == 200, f"verification failed: {verified.text}")
            emails.clear()

            login = await client.post(
                "/api/v1/auth/login",
                headers={"X-Kaede-Client": "mobile"},
                json={
                    "identifier": "maple",
                    "password": password_secret,
                    "password_kdf_version": PASSWORD_KDF_VERSION,
                    "device_name": "integration test",
                },
            )
            require(login.status_code == 200, f"login failed: {login.text}")
            access = login.json()["access_token"]
            refresh = login.json()["refresh_token"]
            require(access.startswith("kc1_at_"), "access token prefix mismatch")
            require(refresh.startswith("kc1_rt_"), "refresh token prefix mismatch")
            bearer = {"Authorization": f"Bearer {access}", "X-Kaede-Client": "mobile"}

            me = await client.get("/api/v1/users/@me", headers=bearer)
            require(
                me.status_code == 200 and me.json()["username"] == "maple",
                "GET /api/v1/users/@me expected HTTP 200 and username 'maple'; "
                f"received HTTP {me.status_code}: {me.text}",
            )
            patched = await client.patch(
                "/api/v1/users/@me/settings",
                headers=bearer,
                json={"theme": "dark", "dm_privacy": "friends"},
            )
            require(
                patched.status_code == 200 and patched.json()["theme"] == "dark",
                "settings update expected HTTP 200 and theme 'dark'; "
                f"received HTTP {patched.status_code}: {patched.text}",
            )

            web_login = await client.post(
                "/api/v1/auth/login",
                headers={"X-Kaede-Client": "web"},
                json={
                    "identifier": "maple@schema.localhost",
                    "password": password_secret,
                    "password_kdf_version": PASSWORD_KDF_VERSION,
                    "device_name": "browser integration test",
                },
            )
            require(web_login.status_code == 200, f"handle login failed: {web_login.text}")
            require(web_login.json()["access_token"] is None, "web access token leaked in body")
            require("kc_access" in web_login.cookies, "web access cookie missing")
            csrf_rejected = await client.patch(
                "/api/v1/users/@me/settings", json={"theme": "light"}
            )
            require(csrf_rejected.status_code == 403, "cookie mutation bypassed CSRF guard")
            empty_bearer = await client.patch(
                "/api/v1/users/@me/settings",
                headers={"Authorization": "Bearer "},
                json={"theme": "light"},
            )
            require(
                empty_bearer.status_code == 401,
                "empty bearer credential fell through to cookie authentication",
            )
            csrf_allowed = await client.patch(
                "/api/v1/users/@me/settings",
                headers={"X-Kaede-Client": "web"},
                json={"theme": "light"},
            )
            require(csrf_allowed.status_code == 200, "web CSRF header was rejected")
            bearer_precedence = await client.patch(
                "/api/v1/users/@me/settings",
                headers={"Authorization": f"Bearer {access}"},
                json={"theme": "dark"},
            )
            require(bearer_precedence.status_code == 200, "bearer did not override cookie auth")

            setup = await client.post(
                "/api/v1/auth/mfa/setup",
                headers=bearer,
                json={
                    "password": password_secret,
                    "password_kdf_version": PASSWORD_KDF_VERSION,
                },
            )
            require(setup.status_code == 200, f"MFA setup failed: {setup.text}")
            secret = setup.json()["secret"]
            enabled = await client.post(
                "/api/v1/auth/mfa/enable",
                headers=bearer,
                json={"code": pyotp.TOTP(secret).now()},
            )
            require(enabled.status_code == 200, f"MFA enable failed: {enabled.text}")
            require(len(enabled.json()["recovery_codes"]) == 10, "recovery code count mismatch")
            revoked_other_session = await client.get("/api/v1/users/@me")
            require(
                revoked_other_session.status_code == 401,
                "MFA enable did not revoke the other browser session",
            )
            kept_current_session = await client.get("/api/v1/users/@me", headers=bearer)
            require(
                kept_current_session.status_code == 200,
                "MFA enable revoked the session that performed reauthentication",
            )
            replacement_without_factor = await client.post(
                "/api/v1/auth/mfa/setup",
                headers=bearer,
                json={
                    "password": password_secret,
                    "password_kdf_version": PASSWORD_KDF_VERSION,
                },
            )
            require(
                replacement_without_factor.status_code == 401,
                "MFA replacement did not require the current factor",
            )
            replacement_with_factor = await client.post(
                "/api/v1/auth/mfa/setup",
                headers=bearer,
                json={
                    "password": password_secret,
                    "password_kdf_version": PASSWORD_KDF_VERSION,
                    "current_code": pyotp.TOTP(secret).now(),
                },
            )
            require(
                replacement_with_factor.status_code == 200,
                "MFA replacement rejected valid password and current factor",
            )

            mfa_login = await client.post(
                "/api/v1/auth/login",
                headers={"X-Kaede-Client": "mobile"},
                json={
                    "identifier": "maple@example.com",
                    "password": password_secret,
                    "password_kdf_version": PASSWORD_KDF_VERSION,
                },
            )
            require(mfa_login.json()["mfa_required"], "MFA was not required")
            completed = await client.post(
                "/api/v1/auth/mfa",
                headers={"X-Kaede-Client": "mobile"},
                json={
                    "ticket": mfa_login.json()["mfa_ticket"],
                    "code": pyotp.TOTP(secret).now(),
                },
            )
            require(completed.status_code == 200, f"MFA login failed: {completed.text}")
            old_refresh = completed.json()["refresh_token"]

            rotated = await client.post(
                "/api/v1/auth/refresh",
                headers={"X-Kaede-Client": "mobile"},
                json={"refresh_token": old_refresh},
            )
            require(rotated.status_code == 200, f"refresh failed: {rotated.text}")
            rotated_access = rotated.json()["access_token"]
            reused = await client.post(
                "/api/v1/auth/refresh",
                headers={"X-Kaede-Client": "mobile"},
                json={"refresh_token": old_refresh},
            )
            require(reused.status_code == 401, "refresh reuse was not rejected")
            revoked = await client.get(
                "/api/v1/users/@me",
                headers={"Authorization": f"Bearer {rotated_access}"},
            )
            require(revoked.status_code == 401, "reuse did not revoke the session")

            pending_email_change = await client.post(
                "/api/v1/auth/email/change",
                headers=bearer,
                json={
                    "email": "maple-new@example.com",
                    "password": password_secret,
                    "password_kdf_version": PASSWORD_KDF_VERSION,
                },
            )
            await drain_mail()
            require(
                pending_email_change.status_code == 200 and len(emails) == 1,
                f"email-change request failed: {pending_email_change.text}",
            )
            stale_email_change_token = token_from_email(emails.pop()[2])

            forgot = await client.post(
                "/api/v1/auth/password/forgot", json={"email": "maple@example.com"}
            )
            await drain_mail()
            require(
                forgot.status_code == 202 and len(emails) == 1,
                "password-reset request expected HTTP 202 and exactly one email; "
                f"received HTTP {forgot.status_code}, {len(emails)} emails: {forgot.text}",
            )
            reset_token = token_from_email(emails.pop()[2])
            reset = await client.post(
                "/api/v1/auth/password/reset",
                json={
                    "token": reset_token,
                    "password": new_password_secret,
                    "password_kdf": password_kdf_metadata(RESET_AUTH_SALT),
                },
            )
            require(reset.status_code == 200, f"password reset failed: {reset.text}")
            stale_email_change = await client.post(
                "/api/v1/auth/email/change/confirm",
                json={"token": stale_email_change_token},
            )
            require(
                stale_email_change.status_code == 400,
                "password reset did not invalidate a pending email change",
            )
            old_login = await client.post(
                "/api/v1/auth/login",
                json={
                    "identifier": "maple",
                    "password": password_secret,
                    "password_kdf_version": PASSWORD_KDF_VERSION,
                },
            )
            require(old_login.status_code == 401, "old password remained valid")
            new_login = await client.post(
                "/api/v1/auth/login",
                json={
                    "identifier": "maple",
                    "password": new_password_secret,
                    "password_kdf_version": PASSWORD_KDF_VERSION,
                },
            )
            require(new_login.status_code == 200, f"new password login failed: {new_login.text}")
            require(new_login.json()["mfa_required"], "MFA state was lost after reset")


def main() -> None:
    asyncio.run(verify())
    print("identity verification passed")


if __name__ == "__main__":
    try:
        main()
    except VerificationFailure as error:
        raise SystemExit(failure_message("identity", error, "make identity-check")) from None
