# Multistage production Dockerfile for Next.js dashboard with Python runtime

#### Stage 1: Node dependencies ####
FROM node:20-bookworm AS node_deps
WORKDIR /app

ENV PNPM_HOME=/usr/local/share/pnpm
ENV PATH="$PNPM_HOME:$PATH"
RUN corepack enable && corepack prepare pnpm@9.12.0 --activate

COPY apps/dashboard/package.json ./package.json
COPY apps/dashboard/pnpm-lock.yaml ./pnpm-lock.yaml
RUN pnpm install --frozen-lockfile

#### Stage 2: Next.js build ####
FROM node:20-bookworm AS node_builder
WORKDIR /app

ENV PNPM_HOME=/usr/local/share/pnpm
ENV PATH="$PNPM_HOME:$PATH"
RUN corepack enable && corepack prepare pnpm@9.12.0 --activate

COPY --from=node_deps /app/node_modules ./node_modules
COPY --from=node_deps /usr/local/share/pnpm /usr/local/share/pnpm
COPY --from=node_deps /usr/local/lib/node_modules /usr/local/lib/node_modules

COPY apps/dashboard/ ./
RUN pnpm build && pnpm prune --prod

#### Stage 3: Python dependencies ####
FROM python:3.11-slim AS python_deps
WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    libssl-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

#### Stage 4: Production runtime ####
FROM node:20-bookworm-slim AS runner
WORKDIR /app

ENV NODE_ENV=production \
    PORT=3050 \
    PYTHON_BIN=/usr/bin/python3 \
    PNPM_HOME=/usr/local/share/pnpm

ENV PATH="$PNPM_HOME:$PATH"

# Install system dependencies and Python runtime
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 \
      python3-pip \
      python3-venv \
      curl \
      bash \
      ca-certificates \
      && rm -rf /var/lib/apt/lists/* && \
    corepack enable && corepack prepare pnpm@9.12.0 --activate

# Copy application build artifacts
COPY --from=node_builder /app/.next ./.next
COPY --from=node_builder /app/public ./public
COPY --from=node_builder /app/node_modules ./node_modules
COPY --from=node_builder /app/package.json ./package.json
COPY --from=node_builder /app/pnpm-lock.yaml ./pnpm-lock.yaml

# Copy python dependencies and source code
COPY --from=python_deps /install /usr/local
COPY core ./core
COPY utils ./utils
COPY scripts ./scripts
COPY network ./network
COPY parsers ./parsers
# create mount points for runtime data without baking local contents
RUN mkdir -p /app/config /app/data /app/logs
COPY requirements.txt ./

# Create non-root user
RUN groupadd -r nextjs && useradd -r -g nextjs nextjs
RUN mkdir -p /home/nextjs/.cache && chown -R nextjs:nextjs /home/nextjs
RUN chown -R nextjs:nextjs /app
USER nextjs

EXPOSE 3050

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
CMD wget -qO- http://127.0.0.1:3050/api/health || exit 1

VOLUME ["/app/data", "/app/config", "/app/logs"]

CMD ["pnpm", "start"]
