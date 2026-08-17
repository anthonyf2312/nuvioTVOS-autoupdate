FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    STATE_PATH=/data/state.json

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# /data holds state.json and is expected to be a mounted volume.
RUN useradd --create-home --uid 10001 nuvio \
    && mkdir -p /data \
    && chown -R nuvio:nuvio /app /data
USER nuvio

HEALTHCHECK --interval=5m --timeout=20s --start-period=3m --retries=3 \
    CMD ["python", "-m", "nuvio_updater", "--healthcheck"]

ENTRYPOINT ["python", "-m", "nuvio_updater"]
