import json
import math
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import yaml


CONFIG_PATH = Path("/config/config.yaml")


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def image_timestamp(path):
    try:
        name = path.stem
        timestamp_text = name[-19:]
        return datetime.strptime(timestamp_text, "%Y-%m-%d_%H-%M-%S").timestamp()
    except ValueError:
        return path.stat().st_mtime


def get_roi(image, config):
    roi_config = config["roi"]

    if not roi_config.get("configured", False):
        return image, (0, 0)

    x = int(roi_config["x"])
    y = int(roi_config["y"])
    width = int(roi_config["width"])
    height = int(roi_config["height"])

    return image[y:y + height, x:x + width], (x, y)


def detect_interface(gray):
    row_brightness = np.mean(gray, axis=1)
    smoothed = np.convolve(row_brightness, np.ones(15) / 15, mode="same")

    margin = max(10, int(len(smoothed) * 0.1))
    search_area = smoothed[margin:-margin]

    if len(search_area) == 0:
        return gray.shape[0] // 2

    gradient = np.abs(np.gradient(search_area))
    return int(np.argmax(gradient) + margin)


def calculate_contrast(gray, interface_y, config):
    height = gray.shape[0]
    clear_fraction = float(config["contrast"]["clear_region_fraction"])
    turbid_fraction = float(config["contrast"]["turbid_region_fraction"])

    clear_height = max(1, int(height * clear_fraction))
    turbid_height = max(1, int(height * turbid_fraction))

    clear_end = max(1, interface_y - 5)
    clear_start = max(0, clear_end - clear_height)

    turbid_start = min(height - 1, interface_y + 5)
    turbid_end = min(height, turbid_start + turbid_height)

    clear_region = gray[clear_start:clear_end]
    turbid_region = gray[turbid_start:turbid_end]

    if clear_region.size == 0 or turbid_region.size == 0:
        return None

    clear_mean = float(np.mean(clear_region))
    turbid_mean = float(np.mean(turbid_region))

    absolute_contrast = abs(clear_mean - turbid_mean)
    normalized_contrast = absolute_contrast / max(clear_mean, turbid_mean, 1.0)

    return {
        "clear_mean_gray": round(clear_mean, 3),
        "turbid_mean_gray": round(turbid_mean, 3),
        "absolute_contrast": round(absolute_contrast, 3),
        "normalized_contrast": round(normalized_contrast, 6),
        "clear_region": [clear_start, clear_end],
        "turbid_region": [turbid_start, turbid_end],
    }


def detect_particles(gray, config):
    particle_config = config["particles"]

    background_kernel = int(
        particle_config.get("background_kernel_pixels", 31)
    )
    if background_kernel < 3:
        background_kernel = 3
    if background_kernel % 2 == 0:
        background_kernel += 1

    threshold_value = int(
        particle_config.get("local_contrast_threshold", 20)
    )

    blurred = cv2.GaussianBlur(gray, (3, 3), 0)

    local_background = cv2.GaussianBlur(
        blurred,
        (background_kernel, background_kernel),
        0,
    )

    difference = cv2.subtract(local_background, blurred)

    _, mask = cv2.threshold(
        difference,
        threshold_value,
        255,
        cv2.THRESH_BINARY,
    )

    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (3, 3),
    )
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        kernel,
    )

    minimum_area = float(
        particle_config["minimum_area_pixels"]
    )
    maximum_area = float(
        particle_config["maximum_area_pixels"]
    )
    maximum_dimension = int(
        particle_config.get("maximum_dimension_pixels", 100)
    )
    maximum_aspect_ratio = float(
        particle_config.get("maximum_aspect_ratio", 3.5)
    )

    contours, _ = cv2.findContours(
        mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    particles = []
    image_height, image_width = gray.shape

    for contour in contours:
        area = float(cv2.contourArea(contour))

        if area < minimum_area or area > maximum_area:
            continue

        x, y, width, height = cv2.boundingRect(contour)

        if (
            width > maximum_dimension
            or height > maximum_dimension
        ):
            continue

        if width == 0 or height == 0:
            continue

        aspect_ratio = max(
            width / height,
            height / width,
        )

        if aspect_ratio > maximum_aspect_ratio:
            continue

        if (
            x <= 0
            or y <= 0
            or x + width >= image_width
            or y + height >= image_height
        ):
            continue

        moments = cv2.moments(contour)

        if moments["m00"] == 0:
            continue

        center_x = float(
            moments["m10"] / moments["m00"]
        )
        center_y = float(
            moments["m01"] / moments["m00"]
        )
        equivalent_diameter = math.sqrt(
            4.0 * area / math.pi
        )

        particles.append({
            "x": center_x,
            "y": center_y,
            "area_px": area,
            "diameter_px": equivalent_diameter,
            "contour": contour,
        })

    return particles, mask

def calculate_particle_statistics(particles, millimeters_per_pixel):
    if not particles:
        return {
            "particle_count": 0,
            "average_diameter_px": None,
            "median_diameter_px": None,
            "average_diameter_mm": None,
            "median_diameter_mm": None,
        }

    diameters = np.array(
        [particle["diameter_px"] for particle in particles],
        dtype=np.float64,
    )

    average_px = float(np.mean(diameters))
    median_px = float(np.median(diameters))

    return {
        "particle_count": len(particles),
        "average_diameter_px": round(average_px, 4),
        "median_diameter_px": round(median_px, 4),
        "average_diameter_mm": (
            round(average_px * millimeters_per_pixel, 4)
            if millimeters_per_pixel is not None
            else None
        ),
        "median_diameter_mm": (
            round(median_px * millimeters_per_pixel, 4)
            if millimeters_per_pixel is not None
            else None
        ),
    }


def calculate_velocity(
    previous_particles,
    current_particles,
    time_difference,
    config,
    millimeters_per_pixel,
):
    if (
        not previous_particles
        or not current_particles
        or time_difference <= 0
    ):
        return {
            "tracked_particle_count": 0,
            "average_velocity_px_s": None,
            "average_velocity_mm_s": None,
        }

    maximum_distance = float(
        config["velocity"]["maximum_tracking_distance_pixels"]
    )

    used_current_particles = set()
    velocities = []

    for previous in previous_particles:
        best_index = None
        best_distance = None
        best_vertical_distance = None

        for index, current in enumerate(current_particles):
            if index in used_current_particles:
                continue

            dx = current["x"] - previous["x"]
            dy = current["y"] - previous["y"]

            if dy < 0:
                continue

            distance = math.hypot(dx, dy)

            if distance > maximum_distance:
                continue

            if best_distance is None or distance < best_distance:
                best_index = index
                best_distance = distance
                best_vertical_distance = dy

        if best_index is not None:
            used_current_particles.add(best_index)
            velocities.append(best_vertical_distance / time_difference)

    if not velocities:
        return {
            "tracked_particle_count": 0,
            "average_velocity_px_s": None,
            "average_velocity_mm_s": None,
        }

    average_velocity_px_s = float(np.mean(velocities))

    return {
        "tracked_particle_count": len(velocities),
        "average_velocity_px_s": round(average_velocity_px_s, 5),
        "average_velocity_mm_s": (
            round(
                average_velocity_px_s * millimeters_per_pixel,
                5,
            )
            if millimeters_per_pixel is not None
            else None
        ),
    }


def save_debug_image(
    image,
    roi_offset,
    interface_y,
    particles,
    output_path,
):
    debug_image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    offset_x, offset_y = roi_offset

    cv2.line(
        debug_image,
        (offset_x, offset_y + interface_y),
        (
            debug_image.shape[1] - 1,
            offset_y + interface_y,
        ),
        (0, 0, 255),
        2,
    )

    for particle in particles:
        contour = particle["contour"] + np.array(
            [[[offset_x, offset_y]]],
            dtype=np.int32,
        )
        cv2.drawContours(debug_image, [contour], -1, (0, 255, 0), 1)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path), debug_image)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp")

    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)

    temporary_path.replace(path)


def process_measurement(measurement_dir, config):
    extensions = {
        extension.lower()
        for extension in config["input"]["image_extensions"]
    }

    image_paths = sorted(
        path
        for path in measurement_dir.iterdir()
        if path.is_file() and path.suffix.lower() in extensions
    )

    if not image_paths:
        return

    results_root = Path(config["output"]["results_dir"])
    measurement_output = results_root / measurement_dir.name

    calibration = config["calibration"]
    millimeters_per_pixel = None

    if calibration.get("configured", False):
        millimeters_per_pixel = float(
            calibration["millimeters_per_pixel"]
        )

    previous_particles = None
    previous_timestamp = None
    image_results = []

    for image_path in image_paths:
        image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)

        if image is None:
            continue

        roi_image, roi_offset = get_roi(image, config)
        interface_y = detect_interface(roi_image)

        contrast_result = calculate_contrast(
            roi_image,
            interface_y,
            config,
        )

        particles, _ = detect_particles(roi_image, config)

        particle_result = calculate_particle_statistics(
            particles,
            millimeters_per_pixel,
        )

        timestamp = image_timestamp(image_path)

        velocity_result = calculate_velocity(
            previous_particles,
            particles,
            timestamp - previous_timestamp
            if previous_timestamp is not None
            else 0,
            config,
            millimeters_per_pixel,
        )

        result = {
            "filename": image_path.name,
            "timestamp": datetime.fromtimestamp(
                timestamp
            ).isoformat(),
            "interface_y_px": interface_y,
            "interface_height_mm": (
                round(interface_y * millimeters_per_pixel, 4)
                if millimeters_per_pixel is not None
                else None
            ),
            "contrast": contrast_result,
            "particles": particle_result,
            "velocity": velocity_result,
        }

        image_results.append(result)

        if config["output"].get("save_debug_images", False):
            save_debug_image(
                image,
                roi_offset,
                interface_y,
                particles,
                measurement_output
                / "debug"
                / image_path.name,
            )

        previous_particles = particles
        previous_timestamp = timestamp

    if not image_results:
        return

    contrast_values = [
        item["contrast"]["normalized_contrast"]
        for item in image_results
        if item["contrast"] is not None
    ]

    particle_sizes_px = [
        item["particles"]["average_diameter_px"]
        for item in image_results
        if item["particles"]["average_diameter_px"] is not None
    ]

    particle_sizes_mm = [
        item["particles"]["average_diameter_mm"]
        for item in image_results
        if item["particles"]["average_diameter_mm"] is not None
    ]

    velocities_px = [
        item["velocity"]["average_velocity_px_s"]
        for item in image_results
        if item["velocity"]["average_velocity_px_s"] is not None
    ]

    velocities_mm = [
        item["velocity"]["average_velocity_mm_s"]
        for item in image_results
        if item["velocity"]["average_velocity_mm_s"] is not None
    ]

    output = {
        "measurement_id": measurement_dir.name,
        "calibration_configured": (
            millimeters_per_pixel is not None
        ),
        "millimeters_per_pixel": millimeters_per_pixel,
        "images_analyzed": len(image_results),
        "summary": {
            "phase_contrast_average": (
                round(float(np.mean(contrast_values)), 6)
                if contrast_values
                else None
            ),
            "particle_size_average_px": (
                round(float(np.mean(particle_sizes_px)), 4)
                if particle_sizes_px
                else None
            ),
            "particle_size_average_mm": (
                round(float(np.mean(particle_sizes_mm)), 4)
                if particle_sizes_mm
                else None
            ),
            "settling_velocity_average_px_s": (
                round(float(np.mean(velocities_px)), 5)
                if velocities_px
                else None
            ),
            "settling_velocity_average_mm_s": (
                round(float(np.mean(velocities_mm)), 5)
                if velocities_mm
                else None
            ),
        },
        "images": image_results,
    }

    save_json(measurement_output / "analysis.json", output)
    save_json(results_root / "latest_analysis.json", output)

    print(
        json.dumps(
            {
                "status": "measurement_analyzed",
                "measurement_id": measurement_dir.name,
                "images_analyzed": len(image_results),
                "summary": output["summary"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def main():
    config = load_config()
    measurements_dir = Path(
        config["input"]["measurements_dir"]
    )
    poll_interval = float(
        config["processing"]["poll_interval_seconds"]
    )
    file_stable_age = float(
        config["processing"].get("file_stable_age_seconds", 2)
    )

    print(
        json.dumps(
            {
                "status": "image_processing_started",
                "measurements_dir": str(measurements_dir),
                "calibration_configured": config[
                    "calibration"
                ].get("configured", False),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    processed_state_path = (
        Path(config["output"]["results_dir"])
        / "processed_state.json"
    )

    try:
        with processed_state_path.open("r", encoding="utf-8") as file:
            processed_state = json.load(file)

        if not isinstance(processed_state, dict):
            processed_state = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        processed_state = {}

    print(
        json.dumps(
            {
                "status": "processed_state_loaded",
                "measurement_count": len(processed_state),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    while True:
        if measurements_dir.exists():
            measurement_dirs = sorted(
                path
                for path in measurements_dir.iterdir()
                if path.is_dir()
            )

            for measurement_dir in measurement_dirs:
                newest_modification = max(
                    (
                        path.stat().st_mtime
                        for path in measurement_dir.iterdir()
                        if path.is_file()
                    ),
                    default=0,
                )

                if time.time() - newest_modification < file_stable_age:
                    continue

                if (
                    processed_state.get(measurement_dir.name)
                    == newest_modification
                ):
                    continue

                try:
                    process_measurement(measurement_dir, config)
                    processed_state[
                        measurement_dir.name
                    ] = newest_modification
                    save_json(
                        processed_state_path,
                        processed_state,
                    )
                except Exception as error:
                    print(
                        json.dumps(
                            {
                                "status": "analysis_error",
                                "measurement_id": (
                                    measurement_dir.name
                                ),
                                "error": str(error),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
