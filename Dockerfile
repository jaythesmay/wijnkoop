FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WIJNKOOP_DATA=/data \
    TZ=Europe/Amsterdam

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY wijnkoop ./wijnkoop
COPY datasette ./datasette
COPY start.sh .

VOLUME ["/data"]
EXPOSE 8001
CMD ["./start.sh"]
