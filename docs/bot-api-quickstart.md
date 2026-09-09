# Build your first Kaede bot

This guide takes you from an empty folder to a working `/ping` command.
You need Python 3.11 or newer, an account on a running Kaede instance, and a
guild where you can install applications. You do not need to host Kaede to
write a bot. To host the server too, follow the [root README](../README.md#setup).

A bot has three parts: an **application** in the Developer Portal, an
**installation** approved by a guild or user, and a **worker** (your Python
process). The worker has its own signing key. Its control token is only for
setup and command publishing.

## 1. Install the SDK

In a terminal:

```sh
mkdir my-kaede-bot
cd my-kaede-bot
python3 -m venv .venv
. .venv/bin/activate
python -m pip install kaede-bot
```

On Windows, use `py -3 -m venv .venv` and activate with
`.venv\Scripts\Activate.ps1` in PowerShell. If you already use uv,
`uv add kaede-bot` installs the same package; run scripts with `uv run python`.
The package is named `kaede-bot`, but Python imports `kaede_bot`.

To use this checkout's SDK instead, run this from the repository root in an
activated virtual environment:

```sh
python -m pip install -e ./sdk/python
```

## 2. Create and install the application

1. Sign in to your home instance and open **User settings → Developer Portal**
   (or `/developers`). Create an application and copy its full reference,
   such as `123@chat.example.com`.
2. Request the scopes `applications.commands` and `interactions.respond`,
   and the Gateway intent `interactions`. This first bot only responds to
   commands, so it does not need message-content access.
3. Create a guild-install invite template with those grants. Select E2EE
   `disabled` for this plaintext example. Open the invite link, select your
   test guild, and approve the installation. A remote guild uses the same flow.
4. Create a control credential on the application page. Copy it when shown;
   you will enter it at a hidden prompt in the next steps.

Use a normal text channel for the first test. The guild's command permissions
must allow your account to invoke the application there.

## 3. Enroll the worker once

Save this as `enroll.py`. Replace the two example values with your application's
HTTPS origin and full reference. Their domains must match exactly.

```python
import asyncio
from getpass import getpass

from kaede_bot import WorkerState

async def main() -> None:
    await WorkerState.enroll(
        application_home="https://chat.example.com",
        application_ref="123@chat.example.com",
        control_token=getpass("Control token: "),
        directory=".kaede-worker",
        name="my-first-bot",
        scopes=["applications.commands", "interactions.respond"],
        intents=["interactions"],
        target_domains=[],
    )

if __name__ == "__main__":
    asyncio.run(main())
```

Run it from your bot folder:

```sh
python enroll.py
```

Enrollment sends the public key to Kaede and saves the private worker state
locally with owner-only permissions. Keep `.kaede-worker/` out of Git and back
it up securely. The directory must stay writable because the SDK saves event
replay cursors there. Reuse it on restart; do not enroll a new worker each time.
An empty `target_domains` adds no worker-specific domain restriction; the
application and installation policies still apply.

## 4. Write the bot

Save this as `bot.py` in the same folder:

```python
import asyncio

import kaede_bot as kaede

bot = kaede.Client(
    worker_state=kaede.WorkerState.load(".kaede-worker"),
    intents=kaede.Intents(
        guilds=False,
        guild_messages=False,
        direct_messages=False,
        direct_message_reactions=False,
        message_reactions=False,
        interactions=True,
    ),
)

@bot.command(name="ping", description="Check whether the bot is awake",
             contexts=["guild"], integration_types=["guild_install"])
async def ping(interaction: kaede.Interaction) -> None:
    await interaction.respond("Pong!")

async def main() -> None:
    try:
        await bot.start()
    finally:
        await bot.close()

if __name__ == "__main__":
    asyncio.run(main())
```

`start()` discovers approved target instances and keeps their Gateway
connections open. This process must keep running for commands to receive replies.

## 5. Publish the command, then run

Save this as `sync_commands.py`, replacing the home URL:

```python
import asyncio
from getpass import getpass

from bot import bot

async def main() -> None:
    try:
        await bot.sync_commands(
            application_home="https://chat.example.com",
            control_token=getpass("Control token: "),
        )
    finally:
        await bot.close()

if __name__ == "__main__":
    asyncio.run(main())
```

Then run:

```sh
python sync_commands.py
python bot.py
```

In the test guild, type `/ping` and select the application's command. You
should receive **Pong!**. Stop the process with Ctrl+C. Run the sync script
again whenever command definitions change; it publishes the commands registered
in `bot.py`. Normal startup only needs worker state, not the control token.

## 6. Add features

The [SDK recipes](bot-sdk-recipes.md) cover messages, files, reactions, polls,
buttons, guild management, forums, threads, tasks, invites, webhooks, scheduled
events, federation, DMs, voice, video, screen sharing, and encrypted participants.
Each feature needs its own scopes and, for events, intents. Grant those through
the portal and worker configuration before adding the handler.

For an existing bot you can deploy with Docker, see the
[Discord bridge](../official-bots/discord-bridge/README.md).

A **user installation** is another option for command-only apps. It exposes
commands in approved guild, bot-DM, or private-channel contexts without adding
a bot member or granting ordinary messages, outbound DMs, calls, or voice.
Users manage it under **User settings → Authorized apps**. Use a guild
installation for the broader recipes.

## Troubleshooting

| What you see | What to check |
| --- | --- |
| Import fails | Activate the virtual environment used to install `kaede-bot`. |
| Worker state is missing | Run enrollment once, then start from the same folder. Use an absolute state path in a service. |
| Enrollment or sync fails | Use the exact HTTPS application home and matching full reference. Redirect URLs are rejected. Check the control credential. |
| `/ping` is missing | Run command sync; check installation, approved contexts, and the invoking user's command permissions. |
| Command appears but never replies | Keep `bot.py` running; check worker revocation, target connectivity, and the `interactions` intent. |
| REST returns 403 | Check application, installation, worker scopes, channel restrictions, and live role permissions. All must allow the action. |
| Message content is empty | Grant `messages.content` and enable `message_content` along with the message event intent. |
| Voice join fails | Enable LiveKit on the channel's authority and grant the requested voice scopes and live permissions. |
| Encrypted room rejects the bot | Participant mode also requires an approved MLS device in that exact room. See the encrypted recipes. |

For API implementers, [Bots and automations](bots-and-automations.md) documents
worker authentication, REST routes, Gateway payloads, and encryption rules.
Bot requests use short-lived target tokens and signed proofs; a human bearer
token or a control token is not a substitute for runtime bot authentication.
