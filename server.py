import json
import os
import socket
import subprocess
import threading
import time
from datetime import datetime

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "5001"))
DATA_DIR = "/data/measurements"
measurement_lock = threading.Lock()
measurement_status = {
    "active": False,
    "measurement_id": None,
    "images_captured": 0,
    "total_images": 0
}


def update_measurement_status(**changes):
    with measurement_lock:
        measurement_status.update(changes)


def get_measurement_status():
    with measurement_lock:
        return dict(measurement_status)


def send_json(connection, data):
    message = json.dumps(data) + "\n"
    connection.sendall(message.encode("utf-8"))


def capture_image(output_path):
    command = [
        "gst-launch-1.0",
        "-q",
        "-e",
        "v4l2src",
        "device=/dev/video1",
        "num-buffers=1",
        "!",
        "video/x-raw,width=1920,height=1080,format=YUY2",
        "!",
        "videoconvert",
        "!",
        "jpegenc",
        "!",
        "filesink",
        f"location={output_path}",
    ]

    subprocess.run(command, check=True)


def run_measurement(connection, images_per_minute, total_images):
    interval_seconds = 60.0 / images_per_minute
    duration_seconds = (total_images - 1) * interval_seconds

    measurement_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    measurement_dir = os.path.join(DATA_DIR, measurement_id)
    os.makedirs(measurement_dir, exist_ok=True)

    with measurement_lock:
        if measurement_status["active"]:
            send_json(connection, {
                "status": "error",
                "message": "measurement_already_active"
            })
            return

        measurement_status.update(
            active=True,
            measurement_id=measurement_id,
            images_captured=0,
            total_images=total_images
        )

    try:
        send_json(connection, {
            "status": "measurement_started",
            "measurement_id": measurement_id,
            "images_per_minute": images_per_minute,
            "total_images": total_images,
            "interval_seconds": interval_seconds,
            "duration_seconds": duration_seconds
        })

        start_time = time.monotonic()

        for image_number in range(1, total_images + 1):
            planned_time = start_time + ((image_number - 1) * interval_seconds)

            sleep_time = planned_time - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)

            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            filename = f"image_{image_number:04d}_{timestamp}.jpg"
            output_path = os.path.join(measurement_dir, filename)

            capture_image(output_path)
            update_measurement_status(images_captured=image_number)

            send_json(connection, {
                "status": "image_captured",
                "image_number": image_number,
                "total_images": total_images,
                "filename": filename
            })

        send_json(connection, {
            "status": "measurement_finished",
            "measurement_id": measurement_id,
            "images_captured": total_images
        })
    finally:
        update_measurement_status(active=False)


def handle_connection(connection):
    data = b""

    while b"\n" not in data:
        chunk = connection.recv(4096)

        if not chunk:
            return

        data += chunk

    request = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
    command = request.get("command")

    if command == "status":
        status = get_measurement_status()
        send_json(connection, {
            "status": "ok",
            "measurement": status
        })
        return

    if command != "start_measurement":
        send_json(connection, {
            "status": "error",
            "message": "unknown_command"
        })
        return

    images_per_minute = float(request["images_per_minute"])
    total_images = int(request["total_images"])

    if images_per_minute <= 0 or total_images <= 0:
        send_json(connection, {
            "status": "error",
            "message": "values_must_be_greater_than_zero"
        })
        return

    run_measurement(
        connection,
        images_per_minute,
        total_images
    )



def handle_client(connection, address):
    with connection:
        print(f"Verbindung von {address}", flush=True)

        try:
            handle_connection(connection)
        except Exception as error:
            print(f"Fehler: {error}", flush=True)

            try:
                send_json(connection, {
                    "status": "error",
                    "message": str(error)
                })
            except Exception:
                pass


os.makedirs(DATA_DIR, exist_ok=True)

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen()

    print(f"Kamera-TCP-Server läuft auf Port {PORT}", flush=True)

    while True:
        connection, address = server.accept()

        threading.Thread(
            target=handle_client,
            args=(connection, address),
            daemon=True
        ).start()
