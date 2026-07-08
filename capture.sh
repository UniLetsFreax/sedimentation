#!/bin/sh

INTERVAL_SECONDS="${INTERVAL_SECONDS:-5}"

echo "Kamera-Container gestartet"
echo "Aufnahmeintervall: ${INTERVAL_SECONDS} Sekunden"

while true
do
    TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")
    OUTPUT="/data/measurements/image_${TIMESTAMP}.jpg"

    gst-launch-1.0 -e \
      v4l2src device=/dev/video1 num-buffers=1 \
      ! video/x-raw,width=1920,height=1080,format=YUY2 \
      ! videoconvert \
      ! jpegenc \
      ! filesink location="$OUTPUT"

    echo "Bild gespeichert: $OUTPUT"

    sleep "$INTERVAL_SECONDS"
done
