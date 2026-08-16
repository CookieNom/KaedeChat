# Python bot API quickstart

The `kaede-bot` package connects your worker directly to every Kaede instance where your application is installed. The application home only manages identity and configuration. It never proxies traffic.

## Create the application

Open **User settings → Developer Portal** and create an application. Pick the scopes and Gateway intents it needs. Then create an invite template and use its link to install the bot in a guild. Remote guilds go through the same consent page.

Message content is a separate scope and intent. Don't request it for a command-only bot.

E2EE channels have two modes you should understand before choosing one. `interaction_only` accepts only encrypted command payloads that a user submits to the bot on purpose. `participant` reserves the permission boundary for Kaede's forthcoming bot-device key protocol; until a verified device is admitted, the SDK receives opaque encrypted envelopes and no plaintext history.

Create a control credential on the application page and store it as
`KAEDE_BOT_CONTROL_TOKEN`. The token is shown once. It can only enroll workers
and publish commands.

## Enroll a worker once

Generate the worker key on the machine that will run the bot. `enroll` sends only the public key and writes the private state with mode `0600`.

```python
import asyncio
import os

from kaede_bot import WorkerState

async def enroll() -> None:
    await WorkerState.enroll(
        application_home="https://apps.example",
        application_ref="123@apps.example",
        control_token=os.environ["KAEDE_BOT_CONTROL_TOKEN"],
        directory="/run/secrets/kaede-worker",
        name="production",
        scopes=[
            "applications.commands",
            "interactions.respond",
            "guilds.read",
            "channels.read",
            "messages.send",
        ],
        intents=["guilds", "interactions"],
        target_domains=[],
    )

asyncio.run(enroll())
```

Keep `KAEDE_BOT_CONTROL_TOKEN` in your deployment secret store, and rotate it from the Developer Portal when needed. Your bot never uses a human session during normal operation.

## Run the bot

```python
import asyncio

import kaede_bot as kaede

bot = kaede.Client(
    worker_state=kaede.WorkerState.load("/run/secrets/kaede-worker"),
    intents=kaede.Intents.default(),
)

@bot.command(name="ping", description="Check whether the bot is awake")
async def ping(interaction: kaede.Interaction) -> None:
    await interaction.respond("Pong!")

asyncio.run(bot.start("https://chat.example", "https://community.example"))
```

When your command definitions change, call `sync_commands(application_home=..., control_token=...)` from a controlled deployment job. You don't need to run it on every process start.

## IDs and usernames

Kaede resources use composite references such as `987654321@chat.example`. A snowflake is an opaque database and ordering identifier; it can't be decoded into a username. Fetch the user once and use the returned handle:

```python
ref = kaede.EntityRef.parse("987654321@chat.example")
user = await bot.fetch_user(ref, target="https://chat.example")
print(user.handle)   # regular username@instance formatting
print(user.mention)  # <@987654321@chat.example>
```

Always keep the full composite reference. Two instances may issue the same numeric snowflake.

## Rate limits and reconnects

The SDK handles the connection plumbing for you. It reads `Retry-After`, obtains short-lived target tokens, and signs every request with the enrolled worker key. It also sends Gateway heartbeats and resumes from per-topic sequence cursors. Event delivery is at least once, so persistent bots should make their handlers idempotent.

Kaede never forwards bot tokens between instances. To cut off a worker, revoke it in the Developer Portal; token issuance and Gateway sessions stop at every target once the short token lifetime runs out.
