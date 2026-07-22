import base64
import json
import os
import socket
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


HOST = "127.0.0.1"
PORT = 5001
BOARD_ID = os.environ.get("BOARD_ID", socket.gethostname())
STORAGE_PATH = os.environ.get("STORAGE_PATH", "/mnt/camera-storage")
MEASUREMENTS_PATH = os.environ.get("MEASUREMENTS_PATH", "/mnt/camera-storage/measurements")
PROCESSED_RESULTS_PATH = os.environ.get("PROCESSED_RESULTS_PATH", "/mnt/camera-storage/image-processing-results")
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "10"))

THINGSBOARD_ENABLED = os.environ.get("THINGSBOARD_ENABLED", "false").lower() == "true"
THINGSBOARD_URL = os.environ.get("THINGSBOARD_URL", "").rstrip("/")
THINGSBOARD_TOKEN = os.environ.get("THINGSBOARD_TOKEN", "")

MONITORED_CONTAINERS = {
    "camera-capture": "camera-capture-server",
    "image-processing": "image-processing-server",
}

LAST_SENT_IMAGE_PATH = None
LAST_SENT_PROCESSED_IMAGE_PATH = None


def get_uptime_seconds():
    with open("/proc/uptime", "r", encoding="utf-8") as file:
        return int(float(file.read().split()[0]))


def get_storage_status():
    total, used, free = shutil.disk_usage(STORAGE_PATH)
    return {
        "path": STORAGE_PATH,
        "total_gb": round(total / (1024 ** 3), 2),
        "used_gb": round(used / (1024 ** 3), 2),
        "free_gb": round(free / (1024 ** 3), 2),
        "used_percent": round((used / total) * 100, 1),
    }


def get_latest_image():
    try:
        base = Path(MEASUREMENTS_PATH)
        images = list(base.glob("*/*.jpg"))

        if not images:
            return {
                "available": False,
                "path": None,
                "measurement_id": None,
                "filename": None,
                "size_bytes": None,
                "modified_timestamp": None,
            }

        latest = max(images, key=lambda path: path.stat().st_mtime)
        stat = latest.stat()

        return {
            "available": True,
            "path": str(latest),
            "measurement_id": latest.parent.name,
            "filename": latest.name,
            "size_bytes": stat.st_size,
            "modified_timestamp": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        }

    except Exception as error:
        return {
            "available": False,
            "error": str(error),
        }



def get_latest_processed_image():
    try:
        base = Path(PROCESSED_RESULTS_PATH)
        images = list(base.glob("*/debug/*.jpg"))

        if not images:
            return {
                "available": False,
                "path": None,
                "measurement_id": None,
                "filename": None,
                "size_bytes": None,
                "modified_timestamp": None,
            }

        latest = max(images, key=lambda image: image.stat().st_mtime)
        stat = latest.stat()

        return {
            "available": True,
            "path": str(latest),
            "measurement_id": latest.parent.parent.name,
            "filename": latest.name,
            "size_bytes": stat.st_size,
            "modified_timestamp": datetime.fromtimestamp(
                stat.st_mtime,
                timezone.utc,
            ).isoformat(),
        }

    except Exception as error:
        return {
            "available": False,
            "error": str(error),
        }


def get_measurement_storage_status():
    try:
        base = Path(MEASUREMENTS_PATH)
        measurement_directories = [
            path for path in base.iterdir()
            if path.is_dir()
        ]
        total_images = sum(1 for _ in base.glob("*/*.jpg"))

        return {
            "total_measurements": len(measurement_directories),
            "total_images": total_images,
        }

    except Exception as error:
        return {
            "total_measurements": 0,
            "total_images": 0,
            "error": str(error),
        }


def get_camera_status():
    try:
        with socket.create_connection((HOST, PORT), timeout=5) as sock:
            sock.sendall(b'{"command":"status"}\n')

            response = b""
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                response += data

        return json.loads(response.decode().strip())

    except Exception as error:
        return {
            "status": "unreachable",
            "error": str(error),
        }


def get_container_info(container_name):
    try:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Status}}|{{.RestartCount}}",
                container_name,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode != 0:
            return {
                "status": "not_found",
                "restart_count": None,
            }

        container_status, restart_count = result.stdout.strip().split("|")

        return {
            "status": container_status,
            "restart_count": int(restart_count),
        }

    except Exception as error:
        return {
            "status": "error",
            "restart_count": None,
            "error": str(error),
        }


def get_docker_status():
    containers = {}

    for service_name, container_name in MONITORED_CONTAINERS.items():
        containers[service_name] = get_container_info(container_name)

    return containers


def calculate_health(status):
    issues = []

    if status["storage"]["used_percent"] >= 95:
        issues.append("storage_critical")
    elif status["storage"]["used_percent"] >= 90:
        issues.append("storage_warning")

    if status["camera"].get("status") != "ok":
        issues.append("camera_unreachable")

    for service_name, container in status["docker"].items():
        if container.get("status") != "running":
            issues.append(f"{service_name}_container_{container.get('status')}")

    if any("critical" in issue or "container" in issue or "unreachable" in issue for issue in issues):
        health = "error"
    elif issues:
        health = "warning"
    else:
        health = "ok"

    return health, issues


def build_image_data_url_if_new(latest_image):
    global LAST_SENT_IMAGE_PATH

    if not latest_image.get("available"):
        return None

    image_path = latest_image.get("path")

    if not image_path or image_path == LAST_SENT_IMAGE_PATH:
        return None

    data = Path(image_path).read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    LAST_SENT_IMAGE_PATH = image_path

    return "data:image/jpeg;base64," + encoded



def build_processed_image_data_url_if_new(latest_processed_image):
    global LAST_SENT_PROCESSED_IMAGE_PATH

    if not latest_processed_image.get("available"):
        return None

    image_path = latest_processed_image.get("path")

    if (
        not image_path
        or image_path == LAST_SENT_PROCESSED_IMAGE_PATH
    ):
        return None

    data = Path(image_path).read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    LAST_SENT_PROCESSED_IMAGE_PATH = image_path

    return "data:image/jpeg;base64," + encoded



def get_latest_analysis():
    analysis_path = Path(PROCESSED_RESULTS_PATH) / "latest_analysis.json"

    try:
        if not analysis_path.is_file():
            return {
                "available": False,
                "measurement_id": None,
                "images_analyzed": 0,
                "calibration_configured": False,
                "millimeters_per_pixel": None,
                "summary": {},
            }

        with analysis_path.open("r", encoding="utf-8") as file:
            analysis = json.load(file)

        return {
            "available": True,
            "measurement_id": analysis.get("measurement_id"),
            "images_analyzed": analysis.get("images_analyzed", 0),
            "calibration_configured": analysis.get(
                "calibration_configured",
                False,
            ),
            "millimeters_per_pixel": analysis.get(
                "millimeters_per_pixel"
            ),
            "summary": analysis.get("summary", {}),
        }

    except Exception as error:
        return {
            "available": False,
            "measurement_id": None,
            "images_analyzed": 0,
            "calibration_configured": False,
            "millimeters_per_pixel": None,
            "summary": {},
            "error": str(error),
        }


def build_telemetry(status):
    measurement = status["camera"].get("measurement", {})
    camera_container = status["docker"].get("camera-capture", {})
    image_processing_container = status["docker"].get("image-processing", {})
    image_processing_container = status["docker"].get("image-processing", {})
    latest_image = status["latest_image"]
    latest_processed_image = status["latest_processed_image"]
    measurement_storage = status["measurement_storage"]
    latest_analysis = status["latest_analysis"]
    analysis_summary = latest_analysis.get("summary", {})

    telemetry = {
        "board_id": status["board_id"],
        "health": status["health"],
        "issue_count": len(status["issues"]),
        "uptime_seconds": status["uptime_seconds"],

        "storage_total_gb": status["storage"]["total_gb"],
        "storage_used_gb": status["storage"]["used_gb"],
        "storage_free_gb": status["storage"]["free_gb"],
        "storage_used_percent": status["storage"]["used_percent"],

        "camera_status": status["camera"].get("status"),
        "measurement_active": measurement.get("active", False),
        "measurement_id": measurement.get("measurement_id"),
        "measurement_images_captured": measurement.get("images_captured", 0),
        "measurement_total_images": measurement.get("total_images", 0),
        "measurement_test_mode": measurement.get("test_mode", 0),
        "stored_measurements_total": measurement_storage.get("total_measurements", 0),
        "stored_images_total": measurement_storage.get("total_images", 0),

        "latest_image_available": latest_image.get("available", False),
        "latest_image_measurement_id": latest_image.get("measurement_id"),
        "latest_image_filename": latest_image.get("filename"),
        "latest_image_size_bytes": latest_image.get("size_bytes"),
        "latest_image_modified_timestamp": latest_image.get("modified_timestamp"),

        "processed_image_available": latest_processed_image.get("available", False),
        "processed_image_measurement_id": latest_processed_image.get("measurement_id"),
        "processed_image_filename": latest_processed_image.get("filename"),
        "processed_image_size_bytes": latest_processed_image.get("size_bytes"),
        "processed_image_modified_timestamp": latest_processed_image.get("modified_timestamp"),

        "analysis_available": latest_analysis.get("available", False),
        "analysis_measurement_id": latest_analysis.get("measurement_id"),
        "analysis_images_analyzed": latest_analysis.get("images_analyzed", 0),
        "analysis_calibration_configured": latest_analysis.get("calibration_configured", False),
        "analysis_millimeters_per_pixel": latest_analysis.get("millimeters_per_pixel"),
        "phase_contrast_average": analysis_summary.get("phase_contrast_average"),
        "particle_size_average_px": analysis_summary.get("particle_size_average_px"),
        "particle_size_average_mm": analysis_summary.get("particle_size_average_mm"),
        "settling_velocity_average_px_s": analysis_summary.get("settling_velocity_average_px_s"),
        "settling_velocity_average_mm_s": analysis_summary.get("settling_velocity_average_mm_s"),

        "docker_camera_capture_status": camera_container.get("status"),
        "docker_camera_capture_restart_count": camera_container.get("restart_count"),
        "docker_image_processing_status": image_processing_container.get("status"),
        "docker_image_processing_restart_count": image_processing_container.get("restart_count"),
        "docker_image_processing_status": image_processing_container.get("status"),
        "docker_image_processing_restart_count": image_processing_container.get("restart_count"),
    }

    image_data_url = build_image_data_url_if_new(latest_image)

    if image_data_url:
        telemetry["latest_image_data_url"] = image_data_url

    processed_image_data_url = build_processed_image_data_url_if_new(
        latest_processed_image
    )

    if processed_image_data_url:
        telemetry["processed_image_data_url"] = processed_image_data_url

    return telemetry


def send_to_thingsboard(telemetry):
    if not THINGSBOARD_ENABLED:
        return {
            "enabled": False,
            "sent": False,
            "status": "disabled",
        }

    if not THINGSBOARD_URL or not THINGSBOARD_TOKEN:
        return {
            "enabled": True,
            "sent": False,
            "status": "missing_config",
        }

    url = f"{THINGSBOARD_URL}/api/v1/{THINGSBOARD_TOKEN}/telemetry"
    payload = json.dumps(telemetry).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return {
                "enabled": True,
                "sent": True,
                "status": "ok",
                "http_status": response.status,
                "payload_bytes": len(payload),
                "included_image": "latest_image_data_url" in telemetry,
            }

    except urllib.error.HTTPError as error:
        return {
            "enabled": True,
            "sent": False,
            "status": "http_error",
            "http_status": error.code,
            "error": str(error),
            "payload_bytes": len(payload),
            "included_image": "latest_image_data_url" in telemetry,
        }

    except Exception as error:
        return {
            "enabled": True,
            "sent": False,
            "status": "error",
            "error": str(error),
            "payload_bytes": len(payload),
            "included_image": "latest_image_data_url" in telemetry,
        }


def collect_status():
    status = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "board_id": BOARD_ID,
        "uptime_seconds": get_uptime_seconds(),
        "storage": get_storage_status(),
        "latest_image": get_latest_image(),
        "latest_processed_image": get_latest_processed_image(),
        "latest_analysis": get_latest_analysis(),
        "measurement_storage": get_measurement_storage_status(),
        "camera": get_camera_status(),
        "docker": get_docker_status(),
    }

    status["health"], status["issues"] = calculate_health(status)
    status["telemetry"] = build_telemetry(status)
    status["thingsboard"] = send_to_thingsboard(status["telemetry"])

    return status


def main():
    while True:
        status = collect_status()

        log_status = json.loads(json.dumps(status))
        image_data = log_status.get("telemetry", {}).get("latest_image_data_url")
        if image_data:
            log_status["telemetry"]["latest_image_data_url"] = "<omitted>"
            log_status["telemetry"]["latest_image_data_url_length"] = len(image_data)

        processed_image_data = log_status.get("telemetry", {}).get(
            "processed_image_data_url"
        )
        if processed_image_data:
            log_status["telemetry"]["processed_image_data_url"] = "<omitted>"
            log_status["telemetry"]["processed_image_data_url_length"] = len(
                processed_image_data
            )

        print(json.dumps(log_status), flush=True)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("board-agent stopped")
