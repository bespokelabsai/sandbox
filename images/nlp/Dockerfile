# NLP toolkit (spaCy + NLTK) with the en_core_web_sm model and common NLTK corpora baked in.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

ENV NLTK_DATA=/usr/share/nltk_data

RUN pip install --no-cache-dir spacy nltk \
    && python -m spacy download en_core_web_sm \
    && python -m nltk.downloader -d /usr/share/nltk_data punkt stopwords wordnet
