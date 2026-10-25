#!/bin/sh
# Starts as root so it can fix /data ownership (Docker creates missing
# bind-mount folders as root), then drops to PUID:PGID.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
DATA_DIR="${DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    if [ "$(stat -c %u:%g "$DATA_DIR")" != "$PUID:$PGID" ]; then
        echo "Setting owner of $DATA_DIR to $PUID:$PGID"
        chown -R "$PUID:$PGID" "$DATA_DIR"
    fi
    exec su-exec "$PUID:$PGID" "$@"
fi

# Already running as a non-root user (compose "user:" set): run as is.
exec "$@"
