FROM python:3.12-alpine

LABEL org.opencontainers.image.title="Quotarr" \
      org.opencontainers.image.description="Per-user auto-approval quotas for Seerr" \
      org.opencontainers.image.source="https://github.com/ZexusAU/quotarr" \
      org.opencontainers.image.licenses="MIT"

# tzdata so TZ=Australia/Sydney etc. works for the calendar-week reset
RUN apk add --no-cache tzdata \
 && addgroup -S -g 1000 quotarr \
 && adduser -S -D -H -u 1000 -G quotarr quotarr \
 && mkdir -p /data \
 && chown quotarr:quotarr /data

WORKDIR /app
COPY quotarr.py ./
COPY static ./static

ENV DATA_DIR=/data \
    PORT=8484 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER quotarr
VOLUME ["/data"]
EXPOSE 8484

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8484'), timeout=4)" || exit 1

CMD ["python", "/app/quotarr.py"]
