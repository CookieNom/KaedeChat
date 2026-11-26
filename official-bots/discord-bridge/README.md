# Discord ↔ KaedeChat bridge

This bot copies new messages between a Discord channel and a KaedeChat channel.
You pick channel pairs from dropdowns opened by **KaedeChat slash commands**.
Docker installs the official `kaede-bot` SDK from PyPI using uv.

## What do I need to create?

| Item | What you need to do | What it is for |
| --- | --- | --- |
| Discord bot token | Create/copy it in Discord's Developer Portal and put it in `.env`. | Lets the bridge connect to Discord every time it starts. |
| Kaede application | Create it in KaedeChat's Developer Portal. | Gives your bridge a bot identity and defines its allowed access. |
| Kaede control credential | Create it once and paste it into the terminal prompt in step 5. | Gives the setup command permission to register this copy of the bot. |
| Kaede worker keys | **Nothing manually.** Step 5 generates and saves them automatically. | Let this copy of the bot connect to KaedeChat after setup. |
| Slash commands | Run the command-publishing step below once. | Adds `/bridge-pair`, `/bridge-list`, and `/bridge-unpair` to KaedeChat. |
| PyPI / uv publishing token | **Not needed to run the bridge.** | Only SDK maintainers use it when publishing releases. |

"Worker" just means the running copy of this bot on your machine. The portal
also has a **Worker keys → Enroll worker** form, but **don't fill it out for
this guide**. The Docker enrollment command does that for you.

## Before you start

You'll need Docker with `docker compose`, a copy of this repository, and
permission to add bots to both servers. Use ordinary, unencrypted text channels
for your first pair, and let their members know messages will be copied to the
other platform. Your Kaede instance must be running a backend recent enough to
include the application-home identity fix; rebuilding only the bridge can't
update the server.

The commands below use **Bash**. From the repository root, run:

```sh
cd official-bots/discord-bridge
cp .env.example .env
chmod 600 .env
```

Only copy the example file on your first setup, because copying it again
replaces your settings. Keep this terminal open in this folder for the next
steps.

## 1. Create the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications)
   and create an application, for example **KaedeChat Bridge**.
2. Open its **Bot** page. Under **Token**, use **Reset Token** if needed and copy
   the bot token. Put it after `DISCORD_TOKEN=` in your `.env` file. Use the bot
   token, not the application ID or OAuth2 client secret.
3. On the same page, turn on **Message Content Intent** and save. The bot needs
   it to read the text it copies.
4. Use the application's server installation link with the `bot` scope to add
   it to your Discord server. In the channel you'll bridge, grant **View
   Channel**, **Send Messages**, and **Embed Links**. You don't need
   Administrator permission or Discord slash commands. See
   [Discord's bot overview](https://docs.discord.com/developers/bots/overview)
   if you haven't installed a bot before.

The bot may show as offline until step 6. That's expected.

## 2. Create and install the KaedeChat application

1. In KaedeChat, open **User settings → Developer Portal**, then **Create
   application**. Give it a name such as **Discord Bridge**.
2. On its **Permissions & installs** page, select these **Scopes** (things the bot may do):
   `guilds.read`, `channels.read`, `messages.metadata`, `messages.content`,
   `messages.send`, `applications.commands`, `interactions.respond`.
3. Select these **Gateway intents** (updates the bot should receive):
   `guilds`, `guild_messages`, `message_content`, `interactions`.

   These are KaedeChat names. `messages.metadata` covers message events and
   `messages.content` their text. You don't need the message history scope.
   Kaede's `interactions` intent isn't a Discord setting; on Discord, the only
   intent you turn on is **Message Content Intent** from step 1.

4. In the permission checklist, allow viewing channels and sending messages.
   Under **Installation contexts**, turn on server/guild installation. A
   user/account installation can't read ordinary channel messages.
5. If you're bridging a server on another KaedeChat instance, make sure **Target
   policy** allows that instance. **Local instance only** won't.
6. Click **Save changes** before making an invite.
7. Open **Invite links & servers**, enter a slug such as `discord-bridge` and an
   invite name, then click **Create invite link**. **This activates the
   application, and it has to happen before worker enrollment.** Open the link
   and install the bot in the KaedeChat server you want to bridge, approving
   the requested access. The bridge uses plaintext channels and doesn't enroll
   an encrypted participant device.
8. Check the bot's permissions in that server. It needs to view and send
   messages in the paired channel. If you limit its installation to selected
   channels, include that one.

New applications start in `draft` status, and creating the first invite link is
what activates them. A control credential alone doesn't. If you try to enroll a
draft application, you'll get `BOT_CONTROL_TOKEN_INVALID` even with the right
credential. This activation has nothing to do with App Directory approval, and
you don't need to list the bridge there.

One application is enough for this bridge, even across several KaedeChat
instances. Install it in each server you want to bridge.

Now fill in the two Kaede settings in `.env`:

```dotenv
DISCORD_TOKEN=your-discord-bot-token
KAEDE_APPLICATION_HOME=https://chat.example
KAEDE_APPLICATION_REF=123@chat.example
```

Replace all the example values:

- `KAEDE_APPLICATION_HOME` is the HTTPS address of the KaedeChat instance where
  you **created the application**, with no path or trailing slash.
- `KAEDE_APPLICATION_REF` is the application's numeric ID, then `@` and that
  instance's domain. It's in the application page's address:
  `/developers/123@chat.example` means `123@chat.example`. If the address shows
  `%40`, replace it with `@`. This is the **application** ID, not a channel ID
  or the bot's username.

The domains in these two settings must match. The channels you bridge can be on
other instances. You'll pick them with the commands in step 6.

## 3. Who can manage channel pairs?

Pairing takes someone on each side:

- On KaedeChat, anyone with **Administrator** permission in a guild where the
  bot is installed can use `/bridge-pair`, `/bridge-list`, and `/bridge-unpair`.
  Manage Server alone isn't enough.
- On Discord, someone with **Manage Channels** permission in the channel
  approves it by sending `!kaede-pair` with the Kaede server reference. The
  approval lasts three minutes.

You don't list usernames or user IDs in `.env`. Kaede administrators can only
pick Discord channels that have a current approval for their guild and that the
bot can view and send to.

Lists and removals cover only the current Kaede guild. Each menu belongs to the
person and guild that opened it, and Administrator permission is checked again
on every selection. Pairs are stored in SQLite and survive restarts.

## 4. Create the Kaede control credential

Go back to the application's **Credentials & workers** page. Under **Control
credentials**, enter a label such as `Discord bridge setup` and click **Create
credential**. Copy the token when it appears, since the portal shows it only
once.

You need this credential for initial setup, but it doesn't go in `.env`. The
next step uses it to register the worker and publish the slash commands. Leave
the portal's manual **Commands** and **Worker keys** forms alone.

## 5. Register the bot and publish its Kaede commands — once

In the same Bash terminal, run:

```sh
docker compose build
read -rs -p 'Paste the Kaede control credential, then press Enter: ' KAEDE_BOT_CONTROL_TOKEN
printf '\n'
export KAEDE_BOT_CONTROL_TOKEN
docker compose run --rm -e BRIDGE_ENROLL=1 -e KAEDE_BOT_CONTROL_TOKEN bridge
docker compose run --rm -e BRIDGE_SYNC_COMMANDS=1 -e KAEDE_BOT_CONTROL_TOKEN bridge
unset KAEDE_BOT_CONTROL_TOKEN
```

When prompted, paste the **control credential from step 4** and press Enter.
Nothing appears while you paste. That's on purpose.

The enrollment command generates the worker's keys, registers the public key
with KaedeChat, and saves the private key in Docker's persistent `bridge-data`
volume. You never handle those keys yourself. The second command publishes the
three slash commands. Use a dedicated Kaede application for the bridge, because
publishing replaces that application's whole global command list.

Both commands return to the prompt when they finish. They don't start relaying
yet. If either prints an error, fix it before you continue.

Refresh the application's **Workers** section. An active worker named
`discord-bridge` means enrollment worked.

From now on, the bridge starts with its saved worker keys and doesn't need the
control credential. `unset` only clears the token from this terminal. It
doesn't revoke the credential in KaedeChat.

## 6. Start the bridge and choose channels in KaedeChat

```sh
docker compose up -d
docker compose logs -f
```

An activated application with an enrolled worker can start before it's
installed in any Kaede server. It picks up installations automatically.

In a Kaede server where the application is installed, type **`/bridge-pair`**.
The command shows the Kaede server reference. Ask a Discord member with **Manage Channels** permission to send `!kaede-pair ID@domain` in the intended Discord channel using that reference. Run `/bridge-pair` again within three minutes. Each approval connects one channel.

Setup happens in **one KaedeChat message** that only you can see. Each choice
moves that message to the next step:

1. Select a Kaede text channel from this server.
2. Select a Discord server with an approved channel.
3. Select a Discord text channel by name (its category is shown too).
4. Select **Confirm pairing** to start sharing messages, or **Cancel**.

**Picking an option moves on right away.** There's no separate Next button.
**Earlier choices / More choices** buttons show up only when a dropdown has more
than 25 options, and they just page through the list. Old menus are replaced,
and the controls disappear when setup finishes. Menus expire after three
minutes, so run the command again to start over or change an earlier choice.
Only channels the bot can view and send to are offered.

The pair is saved and takes effect immediately, with no restart. Each channel
can be in only one pair. Use **`/bridge-list`** to browse this Kaede server's
pairs, or **`/bridge-unpair`** to disconnect one. Disconnecting also discards
any queued messages for that pair that haven't been sent.

To test, send a **new message from your own account** in the Discord channel.
It should appear in KaedeChat with a Discord author label. Then send one in
KaedeChat and check that it shows up in Discord. Old messages and messages from
other bots aren't copied. Press Ctrl+C to stop following the logs; the bridge
keeps running.

If one direction doesn't work, check `/bridge-list`, the bot's permissions in
both channels, Discord's Message Content Intent, and the Kaede scopes and
intents from step 2.

## Restarting, changing settings, and common errors

| Situation | What to do |
| --- | --- |
| Restart the bridge | Run `docker compose restart`. Do not enroll again. |
| Change channel pairs | Use `/bridge-pair` and `/bridge-unpair` in KaedeChat. No restart needed. |
| Change `.env`, such as replacing the Discord token | Run `docker compose up -d --force-recreate` so the container receives the new values. |
| Stop the bridge | Run `docker compose down`. Its saved data remains. |
| `Worker already enrolled` | Keep the saved keys. Run the command-publishing command in step 5 if needed, then step 6. |
| Slash commands do not appear | Run the command-publishing step, check `applications.commands` in the application and installation, and reopen KaedeChat. |
| Permission denied | You need Administrator permission in the current Kaede guild. Open your own command menu instead of using another person's menu. |
| `Discord approval expired or channel access changed` | The three-minute approval ran out or the bot lost access to the channel. Have someone with Manage Channels send `!kaede-pair` again, then rerun `/bridge-pair`. |
| Missing saved worker credentials | Step 5 has not succeeded, or you are using a different/empty Docker volume. |
| Enrollment reports an authorization error | Check that the control credential belongs to this application, is active, and the application allows all the scopes/intents in step 2. |
| Delivery failures keep retrying | Inspect `docker compose logs --tail=100`, then check destination access and channel IDs. |

The `bridge-data` volume holds the channel pairs, worker keys, delivery queue,
and SDK replay positions. Keep it when you update or move the bot, and back it
up somewhere safe. **`docker compose down -v` deletes this data**, keys
included. Run only one bridge process per volume.

## Updating an existing deployment

After pulling new code, rebuild the image and publish the commands again so
KaedeChat applies the current permission requirements:

```sh
docker compose build
read -rs -p 'Kaede control credential: ' KAEDE_BOT_CONTROL_TOKEN
printf '\n'
export KAEDE_BOT_CONTROL_TOKEN
docker compose run --rm -e BRIDGE_SYNC_COMMANDS=1 -e KAEDE_BOT_CONTROL_TOKEN bridge
unset KAEDE_BOT_CONTROL_TOKEN
docker compose up -d --force-recreate
```

Keep the data volume and enrolled worker, and your existing pairs stay. Remove
any old `KAEDE_BRIDGE_ADMINS` or `KAEDE_BRIDGE_ADMIN_REFS` settings from `.env`,
since they're no longer used.

## Behavior and limits

- New human messages relay in both directions with a platform and author label.
  Bots and webhooks are ignored to avoid loops. Mentions never notify anyone.
- Each relayed message has an author embed with the sender's profile picture
  (when there is one) and source platform. The message text stays outside the
  embed. Discord uses the sender's server avatar or default avatar, and Kaede
  uses their public profile avatar. Older SQLite queues upgrade automatically.
- Long text is split into labeled messages. Discord attachments are sent as
  links, which may expire. Kaede attachments get a notice to view them in
  KaedeChat, because downloading them may need authorization.
- Only ordinary text channels are supported. Encrypted channels, DMs, and
  threads aren't, and neither are history import, edits, deletes, reactions,
  file reuploads, or rich embed conversion.
- SQLite keeps queued text until it's sent, then clears the text and keeps the
  delivery ID to skip replays. Failed sends, including permission failures,
  retry with capped backoff, so check the logs and fix the configuration. Later
  messages can overtake a failed one. Deduplication records grow with traffic;
  archive the database during planned maintenance if needed.
- Delivery is at least once after a message is queued. A crash after a remote
  send but before success is recorded can duplicate a Discord message. Kaede
  sends reuse a stable nonce. Discord doesn't replay messages from while the
  bot was offline, and Kaede replay is limited by the server backlog. This
  isn't an archival service.

## Local check

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```

API references: [discord.py](https://discordpy.readthedocs.io/en/stable/api.html),
[SQLAlchemy SQLite](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html).
