# Selenium with headless Chromium and chromedriver.
FROM python:3.11-slim

ENV CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER=/usr/bin/chromedriver

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    chromium \
    chromium-driver \
    fonts-liberation \
    && pip install --no-cache-dir selenium webdriver-manager \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
