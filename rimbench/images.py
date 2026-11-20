from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import PurePath
import warnings

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from .domain import Asset, InspectionError, MAX_IMAGE_BYTES, MAX_PIXELS, utc_now


INPUT_FORMATS = {"PNG", "JPEG", "TIFF", "BMP"}


def file_title(filename):
    base = PurePath((filename or "影像").replace("\\", "/")).stem.strip()
    base = "".join(char for char in base if ord(char) >= 32)
    return base[:120] or "影像"


def ingest(payload):
    if not 0 < len(payload) <= MAX_IMAGE_BYTES:
        raise InspectionError("单张影像不能超过 18 MiB。", status=413, code="image-size")
    mode, image = _open_image(payload)
    canonical = BytesIO()
    image.save(canonical, "PNG", compress_level=4)
    png = canonical.getvalue()
    if len(png) > MAX_IMAGE_BYTES:
        raise InspectionError("无损展开后的影像不能超过 18 MiB。", status=413, code="image-size")
    return Asset(sha256(png).hexdigest(), image.width, image.height, mode, len(png), utc_now()), png


def _open_image(payload):
    try:
        with warnings.catch_warnings(), Image.open(BytesIO(payload)) as opened:
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            if opened.format not in INPUT_FORMATS:
                raise InspectionError("请使用 PNG、JPEG、TIFF 或 BMP。", code="format")
            if opened.width*opened.height > MAX_PIXELS or min(opened.size) < 24:
                raise InspectionError("影像边长至少 24 像素，面积不超过六百万像素。", code="image-size")
            if getattr(opened, "n_frames", 1) != 1:
                raise InspectionError("请上传独立的单页影像。", code="multipage")
            normalized = ImageOps.exif_transpose(opened)
            if normalized.mode in ("I;16", "I;16B", "I;16L", "I;16N", "I"):
                native = np.asarray(normalized, dtype=np.int32)
                if native.min() < 0 or native.max() > 65535:
                    raise InspectionError("灰度数值必须在 16 位无符号范围内。", code="pixel-format")
                return "gray16", Image.fromarray(np.asarray(native, dtype=np.uint16))
            if normalized.mode in ("1", "L"):
                return "gray8", normalized.convert("L")
            if "A" in normalized.getbands() or "transparency" in normalized.info:
                as_rgba = normalized.convert("RGBA")
                if as_rgba.getchannel("A").getextrema()[0] < 255:
                    raise InspectionError("计量影像需要完整的实拍范围，请去掉透明区域。", code="alpha")
            if normalized.mode not in ("P", "RGB", "RGBA", "CMYK", "LA", "YCbCr"):
                raise InspectionError("请使用 8 位灰度、16 位灰度或彩色图像。", code="pixel-format")
            return "rgb8", normalized.convert("RGB")
    except InspectionError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning,
            OSError, SyntaxError, ValueError) as exc:
        raise InspectionError("无法解码影像，请检查格式与文件完整性。", code="decode") from exc


def gray_from_png(payload):
    with Image.open(BytesIO(payload)) as image:
        if image.mode.startswith("I"):
            return np.asarray(image, dtype=np.float32)/65535
        if image.mode == "L":
            return np.asarray(image, dtype=np.float32)/255
        rgb = np.asarray(image.convert("RGB"), dtype=np.float32)
        return (rgb[..., 0]*0.2126+rgb[..., 1]*0.7152+rgb[..., 2]*0.0722)/255


def raster_from_png(payload):
    """Native display raster: uint16 for 16-bit gray, uint8 for 8-bit and colour.

    Storage is lossless, so the returned values are the actual pixel values and
    must not be scaled before reporting them.
    """
    with Image.open(BytesIO(payload)) as image:
        if image.mode.startswith("I"):
            return np.asarray(image, dtype=np.uint16)
        if image.mode == "L":
            return np.asarray(image, dtype=np.uint8)
        return np.asarray(image.convert("RGB"), dtype=np.uint8)


def image_stats(gray):
    height, width = gray.shape
    stride = max(1, int(np.sqrt(height*width/220_000)))
    sample = gray[::stride, ::stride]
    percentile = np.percentile(sample, (1, 10, 50, 90, 99))
    histogram, _ = np.histogram(sample, bins=32, range=(0, 1))
    return {
        "quantiles": [round(float(value), 5) for value in percentile],
        "dark_clipped": round(float(np.mean(sample <= 0.005)), 5),
        "bright_clipped": round(float(np.mean(sample >= 0.995)), 5),
        "histogram": histogram.tolist(),
        "sample_stride": stride,
    }


def png_from_array(array):
    image = Image.fromarray(array)
    stream = BytesIO()
    image.save(stream, "PNG", compress_level=4)
    return stream.getvalue()


def thumbnail(payload, long_edge):
    with Image.open(BytesIO(payload)) as image:
        if image.mode.startswith("I"):
            raster = np.asarray(image, dtype=np.float32)
            image = Image.fromarray(np.round(raster/257).astype(np.uint8))
        else:
            image = image.copy()
        image.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
        stream = BytesIO()
        image.save(stream, "PNG", compress_level=4)
        return stream.getvalue()


def reference_stats(gray):
    coarse = image_stats(gray)
    height, width = gray.shape
    grid = []
    for yi in range(4):
        line = []
        top = yi*height//4
        bottom = (yi+1)*height//4
        for xi in range(6):
            left = xi*width//6
            right = (xi+1)*width//6
            patch = gray[top:bottom, left:right]
            line.append(round(float(np.median(patch)), 4))
        grid.append(line)
    coarse["grid"] = grid
    coarse["dark_fraction"] = round(float(np.mean(gray < 0.24)), 5)
    coarse["median_spread"] = round(max(map(max, grid))-min(map(min, grid)), 5)
    return coarse


def reference_faults(gray):
    stats = reference_stats(gray)
    faults = []
    if stats["quantiles"][2] < 0.44:
        faults.append("空场太暗，不能作为背光归一参考。")
    if stats["dark_fraction"] > 0.06:
        faults.append("空场有大面积暗区，请检查是否留有夹具或样件。")
    if stats["median_spread"] > 0.28:
        faults.append("空场亮度变化很大，请检查背光是否均匀。")
    return stats, faults
