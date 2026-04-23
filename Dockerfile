FROM python:3.12-slim

# Install Node.js 20 LTS (required for Claude Code CLI)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Optional ngrok install — driven by docker-compose build arg which is itself
# driven by `${NGROK_AUTHTOKEN:+1}` in docker-compose.yml. When the authtoken
# is not set, INSTALL_NGROK is empty and we skip the install entirely.
ARG INSTALL_NGROK
RUN if [ "$INSTALL_NGROK" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends gnupg curl ca-certificates \
        && curl -fsSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
             | gpg --dearmor -o /etc/apt/trusted.gpg.d/ngrok.gpg \
        && echo "deb https://ngrok-agent.s3.amazonaws.com bookworm main" \
             > /etc/apt/sources.list.d/ngrok.list \
        && apt-get update && apt-get install -y --no-install-recommends ngrok \
        && apt-get clean \
        && rm -rf /var/lib/apt/lists/* ; \
    fi

RUN useradd -m appuser && mkdir -p /home/appuser/.claude && chown appuser:appuser /home/appuser/.claude

WORKDIR /app

# Install dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source and project config
COPY src/ ./src/
COPY projects.json .

RUN chown -R appuser:appuser /app

USER appuser

WORKDIR /app/src

CMD ["python", "main.py"]
