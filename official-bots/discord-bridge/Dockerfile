FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY official-bots/discord-bridge/requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt \
    && useradd --uid 10001 --create-home bridge \
    && mkdir /data && chown bridge:bridge /data && chmod 700 /data
COPY official-bots/discord-bridge/bridge.py official-bots/discord-bridge/pairing.py ./
USER bridge
CMD ["python", "bridge.py"]
