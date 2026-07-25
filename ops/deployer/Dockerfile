FROM docker:20.10.24-cli@sha256:2967f0819c84dd589ed0a023b9d25dcfe7a3c123d5bf784ffbb77edf55335f0c

COPY deploy.sh /usr/local/bin/thermostat-deploy
RUN chmod 0555 /usr/local/bin/thermostat-deploy

ENV DOCKER_API_VERSION=1.41 \
    TARGET_IMAGE=ghcr.io/allen141/thermostat:main \
    DATA_DIR=/mnt/user/PrivateStorage/projects/thermostat/data \
    STATE_DIR=/state \
    POLL_SECONDS=60

ENTRYPOINT ["/usr/local/bin/thermostat-deploy"]
