# Jellyfin movie nights

An official KaedeChat bot that plays movies and TV episodes, with picture and
sound, into a voice/video call. Each host connects their own Jellyfin account
with Quick Connect. Friends watch the bot's screen share and don't need
Jellyfin accounts.

## Deploy

Use Docker Compose on Linux. From the repository root:

```sh
cd official-bots/jellyfin
cp .env.example .env
chmod 600 .env
```

Create a **dedicated application** in KaedeChat's Developer Portal and set:

- Scopes: `guilds.read`, `channels.read`, `applications.commands`,
  `interactions.respond`, `voice.connect`, `voice.speak`, `voice.stream`,
  `voice.states.read`.
- Gateway intents: `guilds`, `guild_voice_states`, `interactions`.
- Server/guild installations, with View Channel, Connect, Speak, and Stream
  permissions in the voice channels where it will host movies.

Create an invite link to activate the application, then use it to install the
bot in your server. In `.env`, set `KAEDE_APPLICATION_HOME` to the
application's home URL and `KAEDE_APPLICATION_REF` to its full reference (for
example `123@chat.example`). The
[Discord bridge guide](../discord-bridge/README.md#2-create-and-install-the-kaedechat-application)
shows where to find both. **No Jellyfin server, password, or token goes in
`.env`.**

Create a control credential on the application's **Credentials & workers**
page. Then enroll the worker, publish its commands, and start the bot:

```sh
docker compose build
read -rs -p 'Kaede control credential: ' KAEDE_BOT_CONTROL_TOKEN
printf '\n'
export KAEDE_BOT_CONTROL_TOKEN
docker compose run --rm -e JELLYFIN_BOT_ENROLL=1 -e KAEDE_BOT_CONTROL_TOKEN jellyfin
docker compose run --rm -e JELLYFIN_BOT_SYNC_COMMANDS=1 -e KAEDE_BOT_CONTROL_TOKEN jellyfin
unset KAEDE_BOT_CONTROL_TOKEN
docker compose up -d
```

Publishing commands replaces this application's global command list, so don't
reuse the Discord bridge application. The image installs the SDK from this
checkout so its video publishing support matches the bot.

To update an existing deployment, rebuild, run the command-publishing step
again, then `docker compose up -d`. Keep the existing volume and enrolled
worker.

## Host a movie or TV episode

1. Run `/jellyfin-link server:https://your-jellyfin.example` in your Kaede server.
   Jellyfin subpaths such as `/jellyfin` are supported.
2. In Jellyfin, open your profile's **Quick Connect** page and approve the code
   shown privately in Kaede. Press **I've approved the connection** in Kaede.
   Your Jellyfin administrator must have Quick Connect turned on. The menu
   expires after three minutes, so run the command again if needed.
3. Open the **chat of an unencrypted voice channel** you can join. Use
   `/movie title:Movie name`, or `/tv title:Show name` to choose a series and
   then a numbered episode. Use **Previous page** and **Next page** to browse
   more than 25 results. Selecting an unwatched title starts sharing it with
   everyone in that call; partially watched titles offer **Resume at…** or
   **Start from beginning**.
4. Join the call with friends and open the bot's screen share.
5. Use `/movie-pause`, `/movie-resume`, and `/movie-stop` for both movies and
   episodes. Only the host or a server administrator can control playback.

Use `/continue-watching` for the host's unfinished movies and episodes, or
`/tv-next` for Jellyfin's next-episode suggestions. Episodes don't autoplay, so
run `/tv-next` again when one ends.

### Watch progress

The bot reports progress to the host's Jellyfin account every 10 seconds, on
pause and resume, and on stop, completion, or loss of call access. Friends'
accounts aren't updated. Progress follows the audio and video actually
delivered, so buffering and pauses don't advance it.

Resume positions come from Jellyfin, including progress from other Jellyfin
apps. Jellyfin's resume thresholds decide when a title counts as watched and
what appears in Continue Watching. A forced shutdown can lose progress since the
last successful update. If Jellyfin is unreachable, playback continues and a
warning is logged. A failed final save is also reported in Kaede if the
original interaction can still be edited.

### Unlinking

`/jellyfin-unlink` removes your saved connection, cancels a pending link, and
stops your movie. To revoke the token on the Jellyfin side too, remove **Kaede
Movie Night** from Jellyfin's devices. Relinking replaces your connection and
stops your current movie. Library results and connection messages are visible
only to the user who asked.

## Operation and limits

- Each user's Jellyfin server needs a publicly reachable HTTPS address with a
  valid certificate, so a LAN-only server needs a public HTTPS endpoint.
  Private, loopback, link-local, and other nonpublic addresses are rejected.
  DNS is pinned per request, and redirects and environment proxies are off.
- SQLite keys connections by each user's full Kaede identity, including their
  home domain. Connections are encrypted with a generated key in the data
  volume, and no account passwords are collected. The bot operator can decrypt
  stored access tokens, so users should only connect to an operator they trust.
- Keep and protect the entire `jellyfin-data` volume: `accounts.sqlite3`,
  `accounts.key`, and the enrolled `worker/` identity. Back up the database and
  key together while the bot is stopped. Do not run `docker compose down -v`
  unless you intend to delete all connections and worker credentials.
- One movie or episode plays at a time per deployment, though every user can
  save their own connection. Run one worker process per volume. Playback
  doesn't restart on its own after the bot restarts; use `/continue-watching`
  to resume from Jellyfin.
- The bot outputs a 720p, 24 FPS screen share with stereo audio. FFmpeg decodes
  on the bot host, so allow enough CPU and bandwidth. Files must have a video
  and an audio track, and the first of each is used.
- Subtitles, alternate tracks, seeking during playback, playlists, DVDs,
  encrypted calls, and DMs aren't supported.
- MP4, MKV/WebM, AVI, MPEG/TS, Ogg, and ASF containers work. Byte-range
  requests let the bot read MP4 metadata at the end of a file without
  downloading it all first. The linked user must be allowed direct playback in
  Jellyfin.
- After a pause, receivers may still play a little buffered audio. Losing call
  access or publishing permission stops playback. Run
  `docker compose logs --tail=100` to see failure types; secrets aren't logged.

## Local checks

With the Python SDK and its dependencies installed, plus FFmpeg on `PATH`, run
from the repository root:

```sh
PYTHONPATH=sdk/python:official-bots/jellyfin python3 -m unittest discover -s official-bots/jellyfin/tests -v
```

The decoder check builds a short synthetic MP4 and serves it from a loopback
server, so it needs permission to bind localhost sockets. Without live accounts,
it covers real video/audio output, resume seeking, progress reports,
pause/resume, stop, and cleanup after permission revocation. Library tests cover
paginated TV selection and the resume/restart choice. You still need a live
Jellyfin and Kaede deployment to check call delivery and controls on desktop
and mobile.

API reference: [Jellyfin Quick Connect](https://jellyfin.org/docs/general/server/quick-connect/).
