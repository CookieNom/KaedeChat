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

## Examples and reference

[SDK recipes](https://github.com/CookieNom/KaedeChat/blob/main/docs/bot-sdk-recipes.md)
cover messages, interactions, files, polls, buttons, forums, threads, task
boards, guild administration, webhooks, events, federated guilds and DMs,
audio playback/receiving, camera and screen video, and encrypted participants.
The same resource methods work across instances when the application has an
approved installation there; preserve full `id@domain` references.

[API reference](https://github.com/CookieNom/KaedeChat/blob/main/docs/bots-and-automations.md)
covers scopes, intents, REST routes, Gateway events, authorization, and E2EE.
Event delivery can repeat after reconnects, so handlers that perform durable
work should deduplicate it. Bots can create and end polls but cannot vote.
User installations support explicit commands; ordinary messaging, calls, and
voice require guild-install access and the appropriate grants.

Maintainers: see the [release guide](https://github.com/CookieNom/KaedeChat/blob/main/sdk/python/RELEASING.md).
