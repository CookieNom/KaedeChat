# Discord ↔ KaedeChat bridge

This bot copies new messages between a Discord channel and a KaedeChat channel.
Choose channel pairs with dropdowns opened by **KaedeChat slash commands**.
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

“Worker” just means the running copy of this bot on your machine. The portal's
**Worker keys → Enroll worker** form is a manual alternative; **do not fill it
out for this guide**. The Docker enrollment command does that work for you.

## Before you start

You need Docker with `docker compose`, a copy of this repository, and permission
to add bots to both servers. Use ordinary, unencrypted text channels for your
first pair. Tell their members that messages will be copied to the other platform.

The terminal commands below use **Bash**. Run them from the repository root,
starting with:

```sh
cd official-bots/discord-bridge
cp .env.example .env
chmod 600 .env
```

Only copy the example files on your first setup; copying them again would replace
your settings. Keep this terminal open and stay in this folder for the next steps.

## 1. Create the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications)
   and create an application, for example **KaedeChat Bridge**.
2. Open its **Bot** page. Under **Token**, use **Reset Token** if needed and copy
   the bot token. Put it after `DISCORD_TOKEN=` in your `.env` file. Use the bot
   token, not the application's ID or OAuth2 client secret.
3. On the same page, enable **Message Content Intent** and save. This lets the
   bot read the text it needs to copy.
4. Use the application's server installation link with the `bot` scope to add
   it to your Discord server. Grant **View Channel** and **Send Messages** in
   the channel you will bridge. You do not need Administrator permission or
   slash commands. This version pairs ordinary text channels.

The bot may appear offline until step 6. That is expected.
See [Discord's bot overview](https://docs.discord.com/developers/bots/overview)
for the server installation flow.

## 2. Create and install the KaedeChat application

1. In KaedeChat, open **User settings → Developer Portal**, then **Create
   application**. Give it a name such as **Discord Bridge**.
2. On its **Permissions & installs** page, select these **Scopes** (things the bot may do):
   `guilds.read`, `channels.read`, `messages.metadata`, `messages.content`,
   `messages.send`, `applications.commands`, `interactions.respond`.
3. Select these **Gateway intents** (updates the bot should receive):
   `guilds`, `guild_messages`, `message_content`, `interactions`.

   Use these exact names in the **KaedeChat** Developer Portal. The message
   scope is `messages.metadata` (message events) plus `messages.content` (their
   text). Message history is a separate scope and is not needed for this bridge.
   In Discord's portal, only enable **Message Content Intent** as described in
   step 1; Kaede's `interactions` intent is not a Discord setting.

4. In the permission checklist, allow viewing channels and sending messages.
   Under **Installation contexts**, enable the server/guild installation option.
   A user/account installation cannot read ordinary channel messages.
5. If bridging a server on another KaedeChat instance, make sure **Target
   policy** allows that instance. **Local instance only** will not allow it.
6. Click **Save changes** before making an invite.
7. Open **Invite links & servers**, enter a slug such as `discord-bridge` and an invite
   name, then click **Create invite link**. **This activates the application and must
   happen before worker enrollment.** Copy and open the link to install
   the bot in the KaedeChat server you want to bridge. Approve the requested
   access. This bridge uses plaintext channels and does not enroll an encrypted
   participant device.
8. Check the bot's permissions in the target server. Allow it to view and send
   messages in the paired channel; if you restrict its installation to selected
   channels, include that channel.

You only need one application for this running bridge, even if it connects to
several KaedeChat instances. Install that application in each target server.
You do not need to list it in the public App Directory.

Now fill in the two Kaede settings in `.env`:

```dotenv
DISCORD_TOKEN=your-discord-bot-token
KAEDE_APPLICATION_HOME=https://chat.example
KAEDE_APPLICATION_REF=123@chat.example
```

Replace all example values:

- `KAEDE_APPLICATION_HOME` is the HTTPS address of the KaedeChat instance where
  you **created the application**, without a path or trailing slash.
- `KAEDE_APPLICATION_REF` is the application's numeric ID plus `@` and that
  instance's domain. You can get it from the application page's address:
  `/developers/123@chat.example` means `123@chat.example`. If the address shows
  `%40`, replace that with `@`. This is the **application** ID, not a channel ID
  or the bot's username.

The domains in these two settings must match. Your bridged channels can belong
to other instances; select those channels through the commands in step 6.

## 3. Who can manage channel pairs?

Anyone with **Administrator** permission in a Kaede guild where the bot is
installed can use `/bridge-pair`, `/bridge-list`, and `/bridge-unpair`.
Manage Server permission alone is insufficient. No username or user-ID setting
is required in `.env`.

Administrators can select Kaede channels in their current guild and Discord
channels from **any Discord server this bot has joined**, provided the bot can
view and send messages there. There is no separate Discord administrator check
or Discord-side approval. Install this bridge only across communities whose
Kaede administrators you trust with that access.

Lists and removals are limited to the current Kaede guild. Each menu is bound
to the person and guild that opened it, and Administrator permission is checked
again on every selection. Pairs are stored in SQLite and survive restarts.

**Create the invite link before enrolling the worker.** New Kaede applications
start in `draft` status. Creating their first invite link activates them;
creating a control credential alone does not. Enrollment of a draft application
returns `BOT_CONTROL_TOKEN_INVALID` even when the credential is correct.
This activation is separate from public App Directory/discovery approval.

## 4. Create the Kaede control credential

Go back to your Kaede application's **Credentials & workers** page. Under **Control
credentials**, enter a label such as `Discord bridge setup`, then click
**Create credential**. Copy the token when it appears; the portal shows it only
once. Keep it ready for the next step.

**Yes, you need this credential for initial setup.** You do not need to put it
in `.env`. The next step registers the worker and publishes the slash commands
for you. Leave the portal's manual **Commands** and **Worker keys** forms alone.

## 5. Register the bot and publish its Kaede commands — once

In the Bash terminal from earlier, run:

```sh
docker compose build
read -rs -p 'Paste the Kaede control credential, then press Enter: ' KAEDE_BOT_CONTROL_TOKEN
printf '\n'
export KAEDE_BOT_CONTROL_TOKEN
docker compose run --rm -e BRIDGE_ENROLL=1 -e KAEDE_BOT_CONTROL_TOKEN bridge
docker compose run --rm -e BRIDGE_SYNC_COMMANDS=1 -e KAEDE_BOT_CONTROL_TOKEN bridge
unset KAEDE_BOT_CONTROL_TOKEN
```

When prompted, paste the **Kaede control credential from step 4**, then press
Enter. Nothing appears while you paste; that is intentional.

The enrollment command generates the worker's private/public keys, registers
its public key with KaedeChat, and saves its private key in Docker's persistent
`bridge-data` volume. You never need to copy or generate those keys yourself.
The second command publishes the three built-in slash commands to KaedeChat.
Use a dedicated Kaede application for the bridge: command publishing replaces
that application's global command list. Both commands return to your terminal
when finished; they do not start relaying messages yet. If it prints an error, resolve it before continuing.

Refresh the application's **Workers** section: an active worker named
`discord-bridge` should now appear. This confirms enrollment succeeded.

Normal startup uses the saved worker keys and does not need the control
credential. `unset` removes the token from this terminal's environment; it does
not revoke the credential in KaedeChat.

## 6. Start the bridge and choose channels in KaedeChat

```sh
docker compose up -d
docker compose logs -f
```

In a Kaede server where the application is installed, type **`/bridge-pair`**.
All of the following menus appear **in KaedeChat**, visible only to you:

1. Select a Kaede text channel from this server.
2. Select a Discord server the bot has joined.
3. Select a Discord text channel by name (its category is shown too).
4. Select **Confirm pairing** to start sharing messages, or **Cancel**.

Menus have Previous/Next buttons when there are more than 25 choices. They expire
after three minutes; run the command again if needed. Only channels the bot can
view and send to are offered. Encrypted Kaede channels are excluded.

The pair is saved in SQLite and takes effect immediately, without restarting.
Each channel can belong to only one pair. Use **`/bridge-list`** to browse this
Kaede server's pairs, or **`/bridge-unpair`** to select a pair to disconnect.
Disconnecting also discards unsent queued messages for that pair.

Send a **new message from your human account** in the Discord channel. It should
appear in KaedeChat with a Discord author label. Send a new message in KaedeChat
and check that it appears in Discord. Old messages and messages from other bots
are not copied. Press Ctrl+C to stop following logs; the bridge keeps running.

If a direction does not work, check `/bridge-list`, the bot's permissions in
both channels, Discord's Message Content Intent, and Kaede's message scopes and
intents. Encrypted Kaede channels are not supported.

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
| Missing saved worker credentials | Step 5 has not succeeded, or you are using a different/empty Docker volume. |
| Enrollment reports an authorization error | Check that the control credential belongs to this application, is active, and the application allows all the scopes/intents in step 2. |
| Delivery failures keep retrying | Inspect `docker compose logs --tail=100`, then check destination access and channel IDs. |

The `bridge-data` volume contains the channel pairs, worker keys, message delivery queue, and
SDK replay positions. Keep it when updating or moving the bot and back it up
securely. **`docker compose down -v` deletes this data**, including the keys.
Run only one bridge process per volume.

## Updating an existing deployment

After pulling the updated code, rebuild the image and publish commands again
so Kaede uses the Administrator permission requirement:

```sh
docker compose build
read -rs -p 'Kaede control credential: ' KAEDE_BOT_CONTROL_TOKEN
printf '\n'
export KAEDE_BOT_CONTROL_TOKEN
docker compose run --rm -e BRIDGE_SYNC_COMMANDS=1 -e KAEDE_BOT_CONTROL_TOKEN bridge
unset KAEDE_BOT_CONTROL_TOKEN
docker compose up -d --force-recreate
```

Keep the existing data volume and enrolled worker. Existing channel pairs remain.
Remove any old `KAEDE_BRIDGE_ADMINS` or `KAEDE_BRIDGE_ADMIN_REFS` settings from
`.env`; they are no longer used.

## Behavior and limits

- Relays new human messages in both directions with a platform/author label.
  Bots and webhooks are ignored to prevent loops. Mentions never notify users.
- Long text is split into labeled messages. Discord attachments are sent as
  links (which may expire); Kaede attachments receive a notice to view them in
  KaedeChat because their downloads may require authorization.
- No encrypted channels, DMs, history import, edits, deletes, reactions, file
  reuploads, or rich embed conversion. The channel pickers support ordinary
  text channels; threads are not offered.
- SQLite stores queued text until successfully sent, then clears that text and
  keeps the delivery ID for replay deduplication. Failed sends retry with capped
  backoff, including permission failures; inspect logs and correct configuration.
  Later messages can overtake a failed delivery. Deduplication records grow with
  traffic; archive the database during planned maintenance if necessary.
- Delivery is at least once after enqueue: a crash after a remote send but before
  recording success can duplicate a Discord message. Kaede sends reuse a stable
  nonce. Discord does not replay messages received while the bot is offline;
  Kaede replay is limited by the server backlog. This is not an archival service.

## Local check

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```

API references: [discord.py](https://discordpy.readthedocs.io/en/stable/api.html),
[SQLAlchemy SQLite](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html).
