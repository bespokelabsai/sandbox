# R 4.4.1 with common data-analysis packages (data.table, ggplot2, dplyr, jsonlite).
FROM r-base:4.4.1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libcurl4-openssl-dev \
    libssl-dev \
    libxml2-dev \
    && Rscript -e "install.packages(c('data.table','ggplot2','dplyr','jsonlite'), repos='https://cloud.r-project.org')" \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
