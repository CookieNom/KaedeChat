FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY sdk/python /sdk
RUN pip install --no-cache-dir '/sdk[voice]' \
    && useradd --uid 10001 --create-home moviebot \
    && mkdir /data && chown moviebot:moviebot /data && chmod 700 /data
COPY official-bots/jellyfin/*.py ./
USER moviebot
CMD ["python", "bot.py"]
