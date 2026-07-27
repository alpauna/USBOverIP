# Build context is the repo root (see docker-compose.client.yml).
FROM debian:bookworm-slim

# docker.io is installed only for its CLI (`docker ps`/`docker restart`
# against the host's mounted socket) - dockerd is never started in this
# container. util-linux provides nsenter, used to run `qm`/`pvesh` in the
# host's namespaces on Proxmox nodes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        usbip \
        kmod \
        usbutils \
        util-linux \
        docker.io \
        iproute2 \
        wireguard-tools \
        python3 \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY common/ /app/common/
COPY client/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY client/app/ /app/app/
COPY client/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV PYTHONPATH=/app
ENV WEB_PORT=8001
EXPOSE 8001

ENTRYPOINT ["/app/entrypoint.sh"]
