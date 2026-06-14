# Phase 3 (realtime / AWS Lightsail) container.
# Same code as Phase 1 cron mode — entry point is btc_alert_bot.realtime.
FROM python:3.11-slim

WORKDIR /app

# Build essentials are needed by some indirect deps (e.g. cryptography).
# Strip them after install to keep the final image small.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir -e .

# Persistent state lives under /app/data (mount as a volume).
RUN mkdir -p /app/data

# Run as non-root for safety.
RUN useradd --create-home --shell /bin/bash app && chown -R app:app /app
USER app

# Default command — can be overridden by docker-compose to run main.py instead.
CMD ["python", "-m", "btc_alert_bot.realtime"]
