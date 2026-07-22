import json
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "5001"))
DATA_DIR = "/data/measurements"
TEST_DATA_DIR = os.environ.get("TEST_DATA_DIR", "/data/test-images")
TEST_SERIES_DIR = os.path.join(TEST_DATA_DIR, "synthetic_sedimentation")

measurement_lock = threading.Lock()
measurement_status = {
    "active": False,
    "measurement_id": None,
    "images_captured": 0,
    "total_images": 0,
    "test_mode": 0,
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
        "video/x-raw,format=GRAY8,width=2592,height=1944",
        "!",
        "queue",
        "!",
        "videoconvert",
        "n-threads=4",
        "!",
        "jpegenc",
        "!",
        "filesink",
        f"location={output_path}",
    ]

    subprocess.run(command, check=True)


def get_test_images():
    if not os.path.isdir(TEST_SERIES_DIR):
        raise RuntimeError("test_image_directory_not_available")

    test_images = sorted(
        os.path.join(TEST_SERIES_DIR, filename)
        for filename in os.listdir(TEST_SERIES_DIR)
        if filename.lower().endswith((".jpg", ".jpeg"))
    )

    if not test_images:
        raise RuntimeError("no_test_images_available")

    return test_images


def create_measurement_directory():
    base_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    for suffix in range(1000):
        measurement_id = (
            base_id if suffix == 0 else f"{base_id}_{suffix:03d}"
        )
        measurement_dir = os.path.join(DATA_DIR, measurement_id)

        try:
            os.mkdir(measurement_dir)
            return measurement_id, measurement_dir
        except FileExistsError:
            continue

    raise RuntimeError("unable_to_create_unique_measurement_directory")


def write_measurement_metadata(measurement_dir, metadata):
    metadata["updated_at"] = (
        datetime.now().astimezone().isoformat()
    )

    final_path = os.path.join(
        measurement_dir,
        "measurement.json",
    )
    temporary_path = final_path + ".tmp"

    with open(
        temporary_path,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            metadata,
            file,
            indent=2,
            ensure_ascii=False,
        )
        file.write("\n")

    os.replace(temporary_path, final_path)


def run_measurement(
    connection,
    images_per_minute,
    total_images,
    test_mode,
):
    interval_seconds = 60.0 / images_per_minute
    duration_seconds = (total_images - 1) * interval_seconds
    test_images = None

    if test_mode == 1:
        test_images = get_test_images()

        if total_images > len(test_images):
            send_json(connection, {
                "status": "error",
                "message": "not_enough_test_images",
                "requested_images": total_images,
                "available_images": len(test_images),
            })
            return

    with measurement_lock:
        if measurement_status["active"]:
            send_json(connection, {
                "status": "error",
                "message": "measurement_already_active",
            })
            return

        measurement_id, measurement_dir = (
            create_measurement_directory()
        )

        measurement_status.update(
            active=True,
            measurement_id=measurement_id,
            images_captured=0,
            total_images=total_images,
            test_mode=test_mode,
        )

    metadata = {
        "measurement_id": measurement_id,
        "status": "running",
        "mode": "camera" if test_mode == 0 else "test",
        "test_mode": test_mode,
        "images_per_minute": images_per_minute,
        "interval_seconds": interval_seconds,
        "duration_seconds": duration_seconds,
        "total_images": total_images,
        "images_captured": 0,
        "started_at": datetime.now().astimezone().isoformat(),
        "finished_at": None,
        "error": None,
    }
    write_measurement_metadata(
        measurement_dir,
        metadata,
    )

    try:
        send_json(connection, {
            "status": "measurement_started",
            "measurement_id": measurement_id,
            "images_per_minute": images_per_minute,
            "total_images": total_images,
            "interval_seconds": interval_seconds,
            "duration_seconds": duration_seconds,
            "test_mode": test_mode,
        })

        start_time = time.monotonic()

        for image_number in range(1, total_images + 1):
            planned_time = (
                start_time
                + ((image_number - 1) * interval_seconds)
            )

            sleep_time = planned_time - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)

            timestamp = datetime.now().strftime(
                "%Y-%m-%d_%H-%M-%S"
            )
            filename = (
                f"image_{image_number:04d}_{timestamp}.jpg"
            )
            output_path = os.path.join(
                measurement_dir,
                filename,
            )

            if test_mode == 0:
                capture_image(output_path)
            else:
                shutil.copyfile(
                    test_images[image_number - 1],
                    output_path,
                )

            update_measurement_status(
                images_captured=image_number
            )

            metadata["images_captured"] = image_number
            write_measurement_metadata(
                measurement_dir,
                metadata,
            )

            send_json(connection, {
                "status": "image_captured",
                "image_number": image_number,
                "total_images": total_images,
                "filename": filename,
                "test_mode": test_mode,
            })

        metadata.update(
            status="finished",
            images_captured=total_images,
            finished_at=(
                datetime.now().astimezone().isoformat()
            ),
        )
        write_measurement_metadata(
            measurement_dir,
            metadata,
        )

        send_json(connection, {
            "status": "measurement_finished",
            "measurement_id": measurement_id,
            "images_captured": total_images,
            "test_mode": test_mode,
        })
    except Exception as error:
        metadata.update(
            status="failed",
            finished_at=(
                datetime.now().astimezone().isoformat()
            ),
            error=str(error),
        )
        write_measurement_metadata(
            measurement_dir,
            metadata,
        )
        raise
    finally:
        update_measurement_status(active=False)

def handle_connection(connection):
    data = b""

    while b"\n" not in data:
        chunk = connection.recv(4096)

        if not chunk:
            return

        data += chunk

    request = json.loads(
        data.split(b"\n", 1)[0].decode("utf-8")
    )
    command = request.get("command")

    if command == "status":
        status = get_measurement_status()
        send_json(connection, {
            "status": "ok",
            "measurement": status,
        })
        return

    if command != "start_measurement":
        send_json(connection, {
            "status": "error",
            "message": "unknown_command",
        })
        return

    images_per_minute = float(
        request["images_per_minute"]
    )
    total_images = int(request["total_images"])
    test_mode = int(request.get("test_mode", 0))

    if images_per_minute <= 0 or total_images <= 0:
        send_json(connection, {
            "status": "error",
            "message": "values_must_be_greater_than_zero",
        })
        return

    if test_mode not in (0, 1):
        send_json(connection, {
            "status": "error",
            "message": "test_mode_must_be_0_or_1",
        })
        return

    run_measurement(
        connection,
        images_per_minute,
        total_images,
        test_mode,
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
                    "message": str(error),
                })
            except Exception:
                pass


os.makedirs(DATA_DIR, exist_ok=True)

with socket.socket(
    socket.AF_INET,
    socket.SOCK_STREAM,
) as server:
    server.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_REUSEADDR,
        1,
    )
    server.bind((HOST, PORT))
    server.listen()

    print(
        f"Kamera-TCP-Server läuft auf Port {PORT}",
        flush=True,
    )

    while True:
        connection, address = server.accept()

        threading.Thread(
            target=handle_client,
            args=(connection, address),
            daemon=True,
        ).start()
