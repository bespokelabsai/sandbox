# PHP 8.3 CLI with Composer.
FROM php:8.3-cli-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    unzip \
    git \
    && php -r "copy('https://getcomposer.org/installer','/tmp/c.php');" \
    && php /tmp/c.php --install-dir=/usr/local/bin --filename=composer \
    && rm -f /tmp/c.php \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
