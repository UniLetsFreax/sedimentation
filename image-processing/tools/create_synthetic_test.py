import json
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np


OUTPUT_DIR = Path("/test-output/synthetic_sedimentation")
IMAGE_WIDTH = 800
IMAGE_HEIGHT = 600
IMAGE_COUNT = 12
INTERVAL_SECONDS = 2
PARTICLE_MOVEMENT_PX_PER_IMAGE = 6

CLEAR_GRAY = 185
TURBID_GRAY = 165
PARTICLE_GRAY = 55
INTERFACE_Y = 260

PARTICLES = [
    (90, 310, 4),
    (150, 350, 5),
    (210, 390, 6),
    (270, 430, 7),
    (330, 470, 8),
    (390, 325, 4),
    (450, 365, 5),
    (510, 405, 6),
    (570, 445, 7),
    (630, 485, 8),
    (690, 335, 4),
    (735, 375, 5),
]


def create_image(frame_number):
    image = np.full(
        (IMAGE_HEIGHT, IMAGE_WIDTH),
        CLEAR_GRAY,
        dtype=np.uint8,
    )

    image[INTERFACE_Y:, :] = TURBID_GRAY

    rng = np.random.default_rng(1000 + frame_number)
    noise = rng.normal(0, 2, image.shape)
    image = np.clip(
        image.astype(np.float32) + noise,
        0,
        255,
    ).astype(np.uint8)

    movement = frame_number * PARTICLE_MOVEMENT_PX_PER_IMAGE

    for x, start_y, radius in PARTICLES:
        current_y = start_y + movement

        cv2.circle(
            image,
            (x, current_y),
            radius,
            PARTICLE_GRAY,
            thickness=-1,
            lineType=cv2.LINE_AA,
        )

    return image


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    start_time = datetime(2026, 1, 1, 12, 0, 0)

    for frame_number in range(IMAGE_COUNT):
        timestamp = start_time + timedelta(
            seconds=frame_number * INTERVAL_SECONDS
        )

        filename = (
            f"image_{frame_number + 1:04d}_"
            f"{timestamp:%Y-%m-%d_%H-%M-%S}.jpg"
        )

        image = create_image(frame_number)

        success = cv2.imwrite(
            str(OUTPUT_DIR / filename),
            image,
            [cv2.IMWRITE_JPEG_QUALITY, 95],
        )

        if not success:
            raise RuntimeError(f"Bild konnte nicht gespeichert werden: {filename}")

    diameters = [radius * 2 for _, _, radius in PARTICLES]

    expected = {
        "description": "Synthetischer Sedimentationstest",
        "image_width_px": IMAGE_WIDTH,
        "image_height_px": IMAGE_HEIGHT,
        "image_count": IMAGE_COUNT,
        "interval_seconds": INTERVAL_SECONDS,
        "interface_y_px": INTERFACE_Y,
        "clear_gray": CLEAR_GRAY,
        "turbid_gray": TURBID_GRAY,
        "expected_absolute_phase_contrast": abs(
            CLEAR_GRAY - TURBID_GRAY
        ),
        "expected_normalized_phase_contrast": round(
            abs(CLEAR_GRAY - TURBID_GRAY)
            / max(CLEAR_GRAY, TURBID_GRAY),
            6,
        ),
        "particle_count": len(PARTICLES),
        "particle_diameters_px": diameters,
        "expected_particle_diameter_average_px": round(
            float(np.mean(diameters)),
            4,
        ),
        "particle_movement_px_per_image": (
            PARTICLE_MOVEMENT_PX_PER_IMAGE
        ),
        "expected_settling_velocity_px_s": round(
            PARTICLE_MOVEMENT_PX_PER_IMAGE
            / INTERVAL_SECONDS,
            5,
        ),
    }

    with (OUTPUT_DIR / "expected.json").open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            expected,
            file,
            indent=2,
            ensure_ascii=False,
        )

    print(
        json.dumps(
            {
                "status": "synthetic_test_created",
                "output": str(OUTPUT_DIR),
                "images": IMAGE_COUNT,
                "expected": expected,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
