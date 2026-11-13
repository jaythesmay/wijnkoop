FROM python:3.12-slim

ARG BUILD_DATE=unknown
ARG GIT_SHA=unknown

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WIJNKOOP_DATA=/data \
    TZ=Europe/Amsterdam \
    BUILD_DATE=${BUILD_DATE} \
    GIT_SHA=${GIT_SHA} \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
# Headless browser for shops whose prices only appear after their JavaScript ran
# (Wijnvoordeel, Wijnbeurs). Only the small headless shell, no full Chrome.
RUN playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*
COPY wijnkoop ./wijnkoop
COPY datasette ./datasette
COPY start.sh .

VOLUME ["/data"]
EXPOSE 8001
CMD ["./start.sh"]
