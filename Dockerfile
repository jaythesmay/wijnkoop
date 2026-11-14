FROM python:3.12-slim

ARG BUILD_DATE=unknown
ARG GIT_SHA=unknown

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WIJNKOOP_DATA=/data \
    TZ=Europe/Amsterdam \
    BUILD_DATE=${BUILD_DATE} \
    GIT_SHA=${GIT_SHA}

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --no-create-home --home-dir /app --shell /usr/sbin/nologin wijnkoop

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY wijnkoop ./wijnkoop
COPY datasette ./datasette
COPY start.sh .

VOLUME ["/data"]
EXPOSE 8001
CMD ["./start.sh"]
