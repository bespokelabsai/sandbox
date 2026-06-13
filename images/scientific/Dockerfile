# Scientific computing stack (numpy, scipy, sympy, networkx, statsmodels).
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    numpy \
    scipy \
    sympy \
    networkx \
    statsmodels
