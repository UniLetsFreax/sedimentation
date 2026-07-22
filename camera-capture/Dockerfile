FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \
    python3 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    && rm -rf /var/lib/apt/lists/*

COPY server.py /app/server.py

EXPOSE 5001

CMD ["python3", "/app/server.py"]
