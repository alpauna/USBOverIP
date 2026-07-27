# Build context is the repo root (see docker-compose.server.yml), so that
# this image can also pull in the shared common/ library.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        usbip \
        kmod \
        usbutils \
        iproute2 \
        wireguard-tools \
        nftables \
        python3 \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY common/ /app/common/
COPY server/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY server/app/ /app/app/
COPY server/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV PYTHONPATH=/app
ENV WEB_PORT=8000
EXPOSE 8000 3240

ENTRYPOINT ["/app/entrypoint.sh"]
