FROM node:20-slim

RUN corepack enable pnpm

WORKDIR /app

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
RUN pnpm run build

EXPOSE 3000
CMD ["/bin/sh", "-c", "printf 'window.__ORCHESTRATOR_PORT__=\"%s\";' \"${ORCHESTRATOR_PORT:-8000}\" > /app/dist/runtime-config.js && exec pnpm run preview -- --host 0.0.0.0 --port 3000"]
