# Jellyfin movie nights

A separate official KaedeChat bot that shares movies and TV episodes with picture and sound in a
voice/video call. Each user connects their own Jellyfin account with Quick
Connect. Friends watch the bot's screen share and do not need Jellyfin accounts.

## Deploy

Use Docker Compose on Linux. From the repository root:

```sh
cd official-bots/jellyfin
cp .env.example .env
chmod 600 .env
```

Create a **dedicated application** in KaedeChat's Developer Portal. Configure:

- Scopes: `guilds.read`, `channels.read`, `applications.commands`,
  `interactions.respond`, `voice.connect`, `voice.speak`, `voice.stream`,
  `voice.states.read`.
- Gateway intents: `guilds`, `guild_voice_states`, `interactions`.
- Server/guild installations, with View Channel, Connect, Speak, and Stream
  permissions in the voice channels where it will host movies.

Create an invite link to activate the application, then install it in your
server. Set `KAEDE_APPLICATION_HOME` and `KAEDE_APPLICATION_REF` in `.env` to
this application's home URL and full reference (for example `123@chat.example`).
**No Jellyfin server, password, or token goes in `.env`.**

Create a control credential in the application's **Credentials & workers**
page. Enroll the worker and publish its commands once:

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

Command publishing replaces this application's global commands. Do not reuse
the Discord bridge application. The image installs the SDK from this checkout
so its video publishing support matches the bot.

## Host a movie or TV episode

1. Run `/jellyfin-link server:https://your-jellyfin.example` in your Kaede server.
   Jellyfin subpaths such as `/jellyfin` are supported.
2. In Jellyfin, open your profile's **Quick Connect** page and approve the code
   shown privately in Kaede. Press **I've approved the connection** in Kaede.
   Quick Connect must be enabled by your Jellyfin administrator. The menu expires
   after three minutes; run the command again if needed.
3. Open the **chat of an unencrypted voice channel** you can join. Use
   `/movie title:Movie name`, or `/tv title:Show name` to choose a series and
   then a numbered episode. Use **Previous page** and **Next page** to browse
   more than 25 results. Selecting an unwatched title starts sharing it with
   everyone in that call; partially watched titles offer **Resume at…** or
   **Start from beginning**.
4. Join the call with friends and open the bot's screen share.
5. Use `/movie-pause`, `/movie-resume`, and `/movie-stop`. Only the host or an
   administrator in that server can control playback. These commands work for
   both movies and episodes.

Use `/continue-watching` for the host's unfinished movies and episodes, or
`/tv-next` for Jellyfin's next episode suggestions. After an episode ends, use
`/tv-next` again; episodes do not autoplay.

The bot reports progress to the host's Jellyfin account every 10 seconds, when
paused or resumed, and on stop, completion, or loss of call access. Resume
positions come from Jellyfin, including progress made in other Jellyfin apps.
Friends' accounts are not updated. Progress follows delivered audio/video;
buffering and time spent paused do not advance it. Jellyfin decides when a title
counts as watched and which titles appear in Continue Watching, according to
its resume thresholds. A forced shutdown can lose progress since the last
successful update. If Jellyfin is unavailable, playback continues and a warning
is logged; a failed final save is also reported in Kaede when the original
interaction is still editable.

Existing deployments must rebuild and run the command-publishing step above
again to add `/tv`, `/continue-watching`, and `/tv-next`, then run
`docker compose up -d`. Keep the existing volume and enrolled worker.

`/jellyfin-unlink` removes your saved connection, cancels a pending link, and
stops your movie. To revoke the token at Jellyfin too, remove **Kaede Movie Night**
from Jellyfin's devices. Relinking replaces your connection and stops your
current movie. Library results and connection messages are private to their user.

## Operation and limits

- Users' Jellyfin servers must have a publicly reachable HTTPS address and a
  valid certificate. Private, loopback, link-local, and other nonpublic addresses
  are rejected. DNS is pinned for each request; redirects and environment proxies
  are disabled. A LAN-only Jellyfin server needs a public HTTPS endpoint.
- SQLite uses each user's full Kaede identity, including their home domain.
  Connections are encrypted with a generated key in the data volume. No account
  passwords are collected. The bot operator can decrypt stored access tokens;
  users should connect only to an operator they trust.
- Keep and protect the entire `jellyfin-data` volume: `accounts.sqlite3`,
  `accounts.key`, and the enrolled `worker/` identity. Back up the database and
  key together while the bot is stopped. Do not run `docker compose down -v`
  unless you intend to delete all connections and worker credentials.
- One active movie or episode per bot deployment; all users can save their own
  connection. Run one worker process per volume. Calls do not restart automatically;
  use `/continue-watching` to resume from Jellyfin after restarting the bot.
- Outputs a 720p, 24 FPS screen share with stereo audio. FFmpeg decodes on the
  bot host; allow sufficient CPU and bandwidth. The first video and audio tracks
  are used. Files must contain both tracks. Subtitles, alternate tracks, seeking
  during playback, playlists, DVDs, encrypted calls, and DMs are not supported.
- MP4, MKV/WebM, AVI, MPEG/TS, Ogg, and ASF containers are accepted. Byte-range
  requests allow MP4 metadata at the end of a file without downloading it first.
  Jellyfin direct playback must be allowed for the linked user.
- A pause may leave a short amount of audio already buffered at the receiver.
  Lost call access or revoked publishing permission stops playback. Use
  `docker compose logs --tail=100` for failure types; secrets are not logged.

## Local checks

With the Python SDK and its dependencies installed, plus FFmpeg on `PATH`, run
from the repository root:

```sh
PYTHONPATH=sdk/python:official-bots/jellyfin python3 -m unittest discover -s official-bots/jellyfin/tests -v
```

The decoder check creates a short synthetic MP4 and uses a loopback server; it
requires permission to bind localhost sockets. It exercises actual video/audio
output, resume seeking, progress reports, pause/resume, stop and permission-revocation
cleanup without live accounts. Library tests exercise paginated TV selection and
the resume/restart choice.
A live Jellyfin/Kaede deployment is still needed to verify call delivery and
controls on desktop and mobile.

API reference: [Jellyfin Quick Connect](https://jellyfin.org/docs/general/server/quick-connect/).
