# PDF processing stack (pypdf, pdfplumber, reportlab, pdf2image) with poppler-utils.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    poppler-utils \
    && pip install --no-cache-dir pypdf pdfplumber reportlab pdf2image \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
