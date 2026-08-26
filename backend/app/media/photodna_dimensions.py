from __future__ import annotations

import math

MIN_NATIVE_IMAGE_DIMENSION = 50
MIN_CLOUD_IMAGE_DIMENSION = 160
MAX_PHOTODNA_HASH_PIXELS = 25_000_000
MAX_STATIC_SOURCE_PIXELS = 100_000_000


def normalized_photodna_dimensions(width: int, height: int) -> tuple[int, int]:
    """Return bounded full-frame dimensions accepted by MatchHash.

    Small images are enlarged to MatchHash's documented dimension floor. Large
    still images are reduced before the native SDK receives a contiguous pixel
    buffer. The latter keeps ordinary high-resolution photos within the
    PhotoDNA worker's 25-million-pixel working budget without cropping any part
    of the submitted image.
    """

    if width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive")
    minimum = min(width, height)
    if minimum < MIN_CLOUD_IMAGE_DIMENSION:
        normalized = (
            max(
                MIN_CLOUD_IMAGE_DIMENSION,
                (width * MIN_CLOUD_IMAGE_DIMENSION + minimum - 1) // minimum,
            ),
            max(
                MIN_CLOUD_IMAGE_DIMENSION,
                (height * MIN_CLOUD_IMAGE_DIMENSION + minimum - 1) // minimum,
            ),
        )
    else:
        normalized = (width, height)
    if normalized[0] * normalized[1] <= MAX_PHOTODNA_HASH_PIXELS:
        return normalized

    scale = math.sqrt(MAX_PHOTODNA_HASH_PIXELS / (normalized[0] * normalized[1]))
    bounded_width = max(1, math.floor(normalized[0] * scale))
    bounded_height = max(1, math.floor(normalized[1] * scale))
    # Floating-point rounding can put the product a few pixels above the hard
    # bound. Reduce the dimension with the larger relative scale until the
    # invariant is exact; this loop normally runs zero or one time.
    while bounded_width * bounded_height > MAX_PHOTODNA_HASH_PIXELS:
        if bounded_width * normalized[1] >= bounded_height * normalized[0]:
            bounded_width -= 1
        else:
            bounded_height -= 1
    if min(bounded_width, bounded_height) < MIN_CLOUD_IMAGE_DIMENSION:
        raise ValueError("image aspect ratio cannot meet PhotoDNA dimension bounds")
    return bounded_width, bounded_height
