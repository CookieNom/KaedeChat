# kaede-bot

The async Python SDK for Kaede bots. Requires Python 3.11 or newer.
Install `kaede-bot`; import `kaede_bot`.

```sh
python -m pip install kaede-bot
# Include the LiveKit transport for audio, video, and screen sharing:
python -m pip install 'kaede-bot[voice]'
```

For a uv project, use `uv add kaede-bot` (or `uv add 'kaede-bot[voice]'`).
Encoded audio playback also needs the `ffmpeg` executable on your PATH.
For encrypted calls with Kaede clients, Linux x86_64 workers can opt in to
the matching native LiveKit library. [Encrypted bot voice setup](https://github.com/CookieNom/KaedeChat/blob/main/docs/bot-api-quickstart.md#encrypted-bot-voice)

## First bot

Follow the [step-by-step quickstart](https://github.com/CookieNom/KaedeChat/blob/main/docs/bot-api-quickstart.md):

1. Create an application in Kaede's Developer Portal.
2. Approve its installation in a test guild.
3. Enroll a worker and save its private state.
4. Write and publish `/ping`.
5. Run the worker and test the command in Kaede.

The control token is used for enrollment and command publishing. A running bot
uses its worker key and short-lived tokens issued by each target instance.
Keep the worker directory private, persistent, and writable for replay cursors.

For self-hosted storage on a private network, configure its exact HTTPS origin
for the API target that issues its media tickets. For example, pass
`private_media_origins={"https://chat.example": ["https://storage.internal"]}`
to `kaede_bot.Client`. The standard `media.<authority>` host also supports
private addresses for the bot's application home and explicitly configured targets.

## Examples and reference

[SDK recipes](https://github.com/CookieNom/KaedeChat/blob/main/docs/bot-api-quickstart.md#sdk-recipes)
cover:

- messages, files, polls, buttons, and other interactions
- guild administration, webhooks, events, forums, threads, and task boards
- federated guilds and DMs
- audio playback and receiving, camera and screen video
- encrypted participants

The same methods work on any instance where the application has an approved
installation. Keep full `id@domain` references.

[API reference](https://github.com/CookieNom/KaedeChat/blob/main/docs/bots-and-automations.md)
covers scopes, intents, REST routes, Gateway events, authorization, and E2EE.
Events can be delivered again after a reconnect, so deduplicate any durable
work. Bots can create and end polls but can't vote. User installations only
run commands; messaging, calls, and voice need a guild installation with the
right grants.

Maintainers: see the [release guide](https://github.com/CookieNom/KaedeChat/blob/main/sdk/python/RELEASING.md).
