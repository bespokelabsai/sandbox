# Node.js 20 LTS runtime with a TypeScript toolchain (typescript, ts-node) plus
# the pnpm and yarn package managers via corepack. The base image already ships
# a `yarn` binary, so `npm i -g yarn` aborts with EEXIST; corepack manages the
# pnpm/yarn shims instead.
FROM node:20-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN npm install -g typescript ts-node \
    && corepack enable \
    && corepack prepare pnpm@latest yarn@stable --activate
