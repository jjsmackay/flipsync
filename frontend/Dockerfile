# Build the React app
FROM node:20-slim AS build

RUN corepack enable pnpm

WORKDIR /app

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
RUN pnpm run build

# Serve the built assets with Caddy. Caddy streams request bodies with no size
# cap by default, so multi-GB uploads pass through to the orchestrator without
# the buffering/reset problems of the dev preview server. It reads the upstream
# from ORCHESTRATOR_PROXY_TARGET at startup (see Caddyfile).
FROM caddy:2-alpine

COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv

EXPOSE 3000
