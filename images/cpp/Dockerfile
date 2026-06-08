# C/C++ toolchain (gcc 13) with cmake, ninja, clang and gdb.
FROM gcc:13-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    cmake \
    ninja-build \
    clang \
    gdb \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
