# Frontend image: Next.js 16 (standalone output).
#
# ---------------------------------------------------------------------------
# The build-time API URL, which is the one thing that reliably goes wrong here.
# ---------------------------------------------------------------------------
# `NEXT_PUBLIC_*` is not read at run time. Next.js INLINES it into the client
# JavaScript bundle during `next build`. So this value is frozen into the image and
# setting it in `environment:` at run time does nothing at all.
#
# It must therefore be the URL **the browser** can reach -- not the URL this
# container can reach. Those differ under compose, and the tempting wrong answer is
# the service name:
#
#     NEXT_PUBLIC_API_BASE_URL=http://backend:8000    <-- WRONG
#
# `backend` resolves on the compose network, not in the user's browser. The page
# would load, the map would never appear, and the console would show connection
# errors against a host that does not exist. Use the address the browser will use:
# http://localhost:8000 for a local `docker compose up`, or the public origin of the
# API for a real server deployment.
#
# Changing it means REBUILDING this image (`docker compose build frontend`), not
# restarting it.
# ---------------------------------------------------------------------------

# --- Stage 1: dependencies ---------------------------------------------------
FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
# `npm ci` needs the dev dependencies: TypeScript and Tailwind run at build time.
RUN npm ci

# --- Stage 2: build ----------------------------------------------------------
FROM node:22-slim AS builder
WORKDIR /app

ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL}

# The assistant service's public origin, when it is deployed separately. Empty
# means "same origin as the API above", which is the single-process shape -- so a
# local `docker compose up` needs nothing here. Same inlining caveat as above: this
# is frozen into the bundle at build time and must be the URL the BROWSER can reach.
ARG NEXT_PUBLIC_ASSISTANT_BASE_URL=
ENV NEXT_PUBLIC_ASSISTANT_BASE_URL=${NEXT_PUBLIC_ASSISTANT_BASE_URL}

ENV NEXT_TELEMETRY_DISABLED=1

COPY --from=deps /app/node_modules ./node_modules
COPY . .

RUN npm run build

# --- Stage 3: runtime --------------------------------------------------------
FROM node:22-slim AS runtime
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# node:22-slim already ships a non-root `node` user (uid 1000).
USER node

# The standalone bundle carries its own minimal node_modules and server.js. `static`
# and `public` are NOT included in it and must be copied alongside, or the page
# renders with no CSS and no JS chunks.
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
COPY --from=builder --chown=node:node /app/public ./public

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD node -e "require('http').get('http://127.0.0.1:3000/',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"

# Exec form so node is PID 1 and gets SIGTERM directly.
CMD ["node", "server.js"]
