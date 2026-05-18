FROM python:3.12-slim

# Install base dev tools, Node.js 20 LTS, GitHub CLI, and psql
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        gnupg \
        git \
        make \
        jq \
        wget \
        unzip \
        openssh-client \
        ripgrep \
        postgresql-client \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs gh \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Eclipse Temurin 17 JDK, Docker CLI, Docker Compose plugin, Terraform, Stripe CLI
RUN mkdir -p /etc/apt/keyrings \
    && wget -qO - https://packages.adoptium.net/artifactory/api/gpg/key/public \
        | gpg --dearmor -o /usr/share/keyrings/adoptium-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/adoptium-archive-keyring.gpg] https://packages.adoptium.net/artifactory/deb bookworm main" \
        | tee /etc/apt/sources.list.d/adoptium.list > /dev/null \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
        | tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && curl -fsSL https://apt.releases.hashicorp.com/gpg \
        | gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com bookworm main" \
        | tee /etc/apt/sources.list.d/hashicorp.list > /dev/null \
    && curl -fsSL https://packages.stripe.dev/api/security/keypair/stripe-cli-gpg/public \
        | gpg --dearmor -o /usr/share/keyrings/stripe-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/stripe-archive-keyring.gpg] https://packages.stripe.dev/stripe-cli-debian-local stable main" \
        | tee /etc/apt/sources.list.d/stripe.list > /dev/null \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        temurin-17-jdk \
        docker-ce-cli \
        docker-compose-plugin \
        terraform \
        stripe \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set JAVA_HOME via stable symlink (arch-agnostic)
RUN ln -sf "$(readlink -f /usr/bin/java | sed 's|/bin/java||')" /usr/local/java-home
ENV JAVA_HOME=/usr/local/java-home

# Install AWS CLI v2 (arch-aware)
RUN ARCH=$(dpkg --print-architecture) \
    && if [ "$ARCH" = "amd64" ]; then AWS_ARCH="x86_64"; else AWS_ARCH="aarch64"; fi \
    && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}.zip" -o /tmp/awscli.zip \
    && unzip /tmp/awscli.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscli.zip

# Install Claude Code CLI and pnpm globally
RUN npm install -g @anthropic-ai/claude-code pnpm

RUN useradd -m appuser && mkdir -p /home/appuser/.claude /home/appuser/.claude-host && chown appuser:appuser /home/appuser/.claude /home/appuser/.claude-host

WORKDIR /app

# Install dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source and project config
COPY src/ ./src/
COPY projects.json .

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && chown -R appuser:appuser /app

USER appuser

WORKDIR /app/src

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "main.py"]
