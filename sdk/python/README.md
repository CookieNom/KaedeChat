# kaede-bot

Asynchronous Python wrapper for Kaede's direct, federated bot API.

```python
import kaede_bot as kaede

bot = kaede.Client(
    worker_state=kaede.WorkerState.load("/run/secrets/kaede-worker"),
    intents=kaede.Intents.default(),
)

@bot.command(name="ping", description="Check whether the bot is awake")
async def ping(interaction: kaede.Interaction) -> None:
    await interaction.respond("Pong!")

```

Worker enrollment is intentionally separate from normal startup. See `docs/bot-api-quickstart.md` in the Kaede repository.
