# Go 1.22 toolchain.
FROM golang:1.22-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Disable VCS stamping so `go build` works outside a git checkout.
ENV GOFLAGS=-buildvcs=false
