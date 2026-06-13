# Rust toolchain with clippy and rustfmt.
FROM rust:1-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN rustup component add clippy rustfmt
