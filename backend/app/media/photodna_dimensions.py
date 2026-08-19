from __future__ import annotations

MIN_NATIVE_IMAGE_DIMENSION = 50
MIN_CLOUD_IMAGE_DIMENSION = 160


def normalized_photodna_dimensions(width: int, height: int) -> tuple[int, int]:
    """Return dimensions that preserve aspect ratio and meet MatchHash's floor."""

    if width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive")
    minimum = min(width, height)
    if minimum >= MIN_CLOUD_IMAGE_DIMENSION:
        return width, height
    return (
        max(
            MIN_CLOUD_IMAGE_DIMENSION, (width * MIN_CLOUD_IMAGE_DIMENSION + minimum - 1) // minimum
        ),
        max(
            MIN_CLOUD_IMAGE_DIMENSION,
            (height * MIN_CLOUD_IMAGE_DIMENSION + minimum - 1) // minimum,
        ),
    )
