# Ruby 3.3 with bundler and rake.
FROM ruby:3.3-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && gem install bundler rake \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
