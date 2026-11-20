from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from .domain import InspectionError, integer
from .images import png_from_array


MIN_WINDOW = 2
MAX_WINDOW = 16384
MASK_COLOR = np.array([4, 175, 145], dtype=np.float32)
CONTOUR_COLOR = np.array([55, 233, 192], dtype=np.uint8)
MASK_ALPHA = 54
_CROSS = ndimage.generate_binary_structure(2, 1)


@dataclass(frozen=True)
class DetailWindow:
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def parse(cls, document, width, height):
        if not isinstance(document, dict):
            raise InspectionError("细节取景框必须是对象。")
        x = integer(document.get("x"), "取景框横坐标", 0, width-1)
        y = integer(document.get("y"), "取景框纵坐标", 0, height-1)
        w = integer(document.get("width"), "取景框宽度", MIN_WINDOW, width)
        h = integer(document.get("height"), "取景框高度", MIN_WINDOW, height)
        if x+w > width or y+h > height:
            raise InspectionError("取景框需要位于原图内。", status=416, code="window-outside")
        if w > MAX_WINDOW or h > MAX_WINDOW:
            raise InspectionError(f"取景框每边最多 {MAX_WINDOW} 像素。", code="window-too-large")
        return cls(x, y, w, h)

    def slices(self):
        return slice(self.y, self.y+self.height), slice(self.x, self.x+self.width)

    def data(self):
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


def pixel_value(raster, mask, x, y):
    """Report the stored pixel value and whether it is foreground or boundary."""
    height, width = mask.shape
    if not (0 <= x < width and 0 <= y < height):
        raise InspectionError("坐标不在原图内。", status=416, code="pixel-outside")
    x, y = int(x), int(y)
    foreground = bool(mask[y, x])
    contour = foreground and not _solid_neighbourhood(mask, x, y)
    if raster.ndim == 3:
        return {
            "x": x, "y": y, "mode": "rgb8", "bits": 8,
            "value": {"r": int(raster[y, x, 0]), "g": int(raster[y, x, 1]), "b": int(raster[y, x, 2])},
            "mask": foreground, "contour": contour,
        }
    sixteen = raster.dtype == np.uint16
    level = int(raster[y, x])
    return {
        "x": x, "y": y, "mode": "gray16" if sixteen else "gray8", "bits": 16 if sixteen else 8,
        "value": {"level": level, "fraction": round(level/(65535 if sixteen else 255), 6)},
        "mask": foreground, "contour": contour,
    }


def _solid_neighbourhood(mask, x, y):
    # Must match mask_contour (four-connected erosion), so a pixel reported as
    # contour is exactly one of the pixels drawn green in the overlay.
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x+dx, y+dy
        if 0 <= ny < mask.shape[0] and 0 <= nx < mask.shape[1] and not mask[ny, nx]:
            return False
    return True


def window_image(raster, window, *, mask=None, overlay=False, long_edge=1200):
    """Crop the stored image to one window; only this window is ever converted."""
    if overlay and mask is not None and raster.shape[:2] != mask.shape:
        raise InspectionError("轮廓与原图尺寸不一致。", code="frame-size")
    patch = raster[window.slices()]
    out_w, out_h, factor = _output_size(window, long_edge)
    base = _display_copy(_box_reduce(patch, out_w, out_h))
    if overlay and mask is not None:
        base = _tint(base, mask[window.slices()], out_w, out_h)
    return png_from_array(base), {
        "window": window.data(), "factor": round(factor, 6),
        "width": out_w, "height": out_h, "downsampled": factor < 1,
    }


def overlay_frame(raster, mask, long_edge=1600):
    """Full-picture original with contour tint, aggregated per displayed pixel.

    Blocks are judged by their share of foreground and by whether any source
    contour pixel falls inside, so a thin edge survives shrinking instead of
    being averaged away into a flat dark tone.
    """
    height, width = mask.shape
    if raster.shape[:2] != (height, width):
        raise InspectionError("轮廓与原图尺寸不一致。", code="frame-size")
    out_w, out_h, _ = _output_size(DetailWindow(0, 0, width, height), long_edge)
    base = _display_copy(_box_reduce(raster, out_w, out_h))
    return png_from_array(_tint(base, mask, out_w, out_h))


def mask_contour(mask):
    # border_value=1 treats the picture edge as background so a shape that
    # touches the frame still gets an outline.
    return mask & ~ndimage.binary_erosion(mask, structure=_CROSS, border_value=1)


def _output_size(window, long_edge):
    long_edge = max(MIN_WINDOW, int(long_edge))
    factor = min(1.0, long_edge/max(window.width, window.height))
    return (max(1, int(round(window.width*factor))),
            max(1, int(round(window.height*factor))), factor)


def _display_copy(patch):
    """8-bit display copy; 16-bit sources are scaled with the same 257 divisor as previews."""
    if patch.ndim == 3:
        return patch.astype(np.uint8, copy=False)
    if patch.dtype == np.uint16:
        return np.rint(patch.astype(np.float32)/257).astype(np.uint8)
    return patch.astype(np.uint8, copy=False)


def _box_reduce(patch, out_w, out_h):
    in_h, in_w = patch.shape[:2]
    if (out_w, out_h) == (in_w, in_h):
        return patch
    row_index = np.clip(np.floor(np.linspace(0, in_h, out_h+1)).astype(int), 0, in_h-1)
    col_index = np.clip(np.floor(np.linspace(0, in_w, out_w+1)).astype(int), 0, in_w-1)
    sums = np.add.reduceat(
        np.add.reduceat(patch.astype(np.float64, copy=False), row_index[:-1], axis=0),
        col_index[:-1], axis=1,
    )
    counts = np.outer(np.diff(row_index), np.diff(col_index))
    if patch.ndim == 3:
        counts = counts[..., None]
    return np.rint(sums/np.maximum(counts, 1)).astype(patch.dtype)


def _area_sums(array, out_w, out_h):
    in_h, in_w = array.shape
    row_index = np.clip(np.floor(np.linspace(0, in_h, out_h+1)).astype(int), 0, in_h-1)
    col_index = np.clip(np.floor(np.linspace(0, in_w, out_w+1)).astype(int), 0, in_w-1)
    sums = np.add.reduceat(
        np.add.reduceat(array.astype(np.int64, copy=False), row_index[:-1], axis=0),
        col_index[:-1], axis=1,
    )
    return sums, np.outer(np.diff(row_index), np.diff(col_index))


def _tint(base, mask_patch, out_w, out_h):
    if base.ndim == 2:
        canvas = np.repeat(base[..., None], 3, axis=2).astype(np.float32)
    else:
        canvas = base[..., :3].astype(np.float32)
    contour = mask_contour(mask_patch)
    mask_sums, counts = _area_sums(mask_patch, out_w, out_h)
    contour_sums, _ = _area_sums(contour, out_w, out_h)
    coverage = mask_sums/np.maximum(counts, 1)
    # Tint is proportional to how much of the displayed pixel was foreground.
    # A block that is only partly covered must not read as a full workpiece pixel.
    alpha = coverage[..., None]*(MASK_ALPHA/255)
    canvas = canvas*(1-alpha)+MASK_COLOR*alpha
    # Any source contour inside the block marks that displayed pixel, so a thin
    # edge still shows up when a screen pixel covers many source pixels.
    canvas[contour_sums > 0] = CONTOUR_COLOR
    return np.rint(np.clip(canvas, 0, 255)).astype(np.uint8)
