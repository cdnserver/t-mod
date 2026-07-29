FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PUPPETEER_SKIP_DOWNLOAD=true \
    DISPLAY=:99

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       chromium \
       ffmpeg \
       fonts-liberation \
       fonts-noto-color-emoji \
       libnss3 \
       libxss1 \
       tini \
       xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN corepack enable \
    && corepack prepare pnpm@11.9.0 --activate \
    && pnpm install --prod --frozen-lockfile

COPY src ./src
COPY docker-entrypoint.sh /usr/local/bin/discord-stream-entrypoint
RUN sed -i 's/\r$//' /usr/local/bin/discord-stream-entrypoint \
    && chmod +x /usr/local/bin/discord-stream-entrypoint \
    && mkdir -p /data/chrome \
    && chown -R node:node /app /data

USER node
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/discord-stream-entrypoint"]
