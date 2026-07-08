#!/bin/sh
docker rm -f camera-capture-server 2>/dev/null || true

docker run -d \
  --name camera-capture-server \
  --restart unless-stopped \
  --network host \
  --device /dev/video1:/dev/video1 \
  -v /mnt/camera-storage/measurements:/data/measurements \
  camera-capture
