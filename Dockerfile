FROM python:3.12-slim

# Debian's ffmpeg is built with libzimg, which HDR → SDR needs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# The service does not run as root; data lives in /data.
RUN useradd --create-home --uid 1000 promin \
 && mkdir /data \
 && chown promin:promin /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py ./
COPY promin ./promin

USER promin
ENV PROMIN_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME /data
CMD ["python", "main.py"]
