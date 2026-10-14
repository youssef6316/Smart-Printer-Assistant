"""Document Color Scanner Utility.

Classifies every page of a PDF as "color" or "monochrome" *before* anything is
sent to the LLM.

Primary method : render each page at low resolution with pypdfium2 and measure
                 per-pixel chroma (max(R,G,B) - min(R,G,B)). This catches colored
                 text, vector graphics and images alike.
Fallback method: a pypdf content-stream / image-XObject heuristic, used only if
                 rendering is unavailable or fails.

CLI:  python document_scanner.py path/to/file.pdf
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ScannerError(Exception):
    """Raised when a document cannot be scanned (missing, corrupt, encrypted...)."""


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def compress_ranges(pages: list[int]) -> str:
    """[1, 2, 3, 5] -> '1-3,5'."""
    if not pages:
        return ""
    pages = sorted(set(pages))
    out: list[str] = []
    start = prev = pages[0]
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        out.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = p
    out.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(out)


# --------------------------------------------------------------------------- #
# Primary: pixel analysis
# --------------------------------------------------------------------------- #
def _scan_with_render(path: Path) -> dict[int, dict[str, Any]]:
    import numpy as np
    import pypdfium2 as pdfium

    chroma_threshold = _env_float("COLOR_CHROMA_THRESHOLD", 28.0)
    pixel_fraction = _env_float("COLOR_PIXEL_FRACTION", 0.0002)
    scale = _env_float("SCAN_RENDER_SCALE", 0.75)

    results: dict[int, dict[str, Any]] = {}
    pdf = pdfium.PdfDocument(str(path))
    try:
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                image = page.render(scale=scale).to_pil().convert("RGB")
            finally:
                page.close()
            arr = np.asarray(image, dtype=np.int16)
            chroma = arr.max(axis=2) - arr.min(axis=2)
            ratio = float((chroma > chroma_threshold).mean())
            results[index + 1] = {
                "is_color": ratio > pixel_fraction,
                "colored_pixel_ratio": round(ratio, 6),
            }
    finally:
        pdf.close()
    return results


# --------------------------------------------------------------------------- #
# Fallback: structural heuristic
# --------------------------------------------------------------------------- #
_RGB_OP = re.compile(rb"([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+(?:rg|RG)\b")
_CMYK_OP = re.compile(rb"([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+(?:k|K)\b")


def _scan_with_pypdf(path: Path) -> dict[int, dict[str, Any]]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        raise ScannerError("The PDF is encrypted/password protected.")

    results: dict[int, dict[str, Any]] = {}
    for index, page in enumerate(reader.pages):
        is_color = False
        try:
            content = page.get_contents()
            data = content.get_data() if content is not None else b""
            for m in _RGB_OP.finditer(data):
                r, g, b = (float(v) for v in m.groups())
                if not (r == g == b):
                    is_color = True
                    break
            if not is_color:
                for m in _CMYK_OP.finditer(data):
                    c, mm, y, _k = m.groups()
                    if any(float(v) > 0 for v in (c, mm, y)):
                        is_color = True
                        break
            if not is_color:
                xobjects = (page.get("/Resources") or {}).get("/XObject")
                if xobjects:
                    for obj in xobjects.get_object().values():
                        obj = obj.get_object()
                        if obj.get("/Subtype") != "/Image":
                            continue
                        cs = obj.get("/ColorSpace")
                        cs_name = str(cs[0] if isinstance(cs, list) else cs)
                        if cs_name != "/DeviceGray" and "Gray" not in cs_name:
                            is_color = True
                            break
        except Exception as exc:  # one bad page must not abort the scan
            logger.warning("Heuristic scan failed on page %d: %s", index + 1, exc)
        results[index + 1] = {"is_color": is_color, "colored_pixel_ratio": None}
    return results


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def scan_pdf_colors(file_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Return a color map for the PDF at ``file_path``.

    {
      "file": "invoice.pdf", "total_pages": 4, "method": "render",
      "pages": {"1": "color", "2": "color", "3": "monochrome", "4": "monochrome"},
      "color_pages": [1, 2], "monochrome_pages": [3, 4],
      "color_ranges": "1-2", "monochrome_ranges": "3-4",
      "details": {"1": {"colored_pixel_ratio": 0.0412}, ...}
    }
    """
    path = Path(file_path)
    if not path.is_file():
        raise ScannerError(f"File not found: {path.name}")
    if path.suffix.lower() != ".pdf":
        raise ScannerError("Only PDF files are supported.")

    method = "render"
    try:
        raw = _scan_with_render(path)
    except ScannerError:
        raise
    except Exception as exc:
        logger.warning("Pixel analysis unavailable (%s); using structural heuristic.", exc)
        method = "heuristic"
        try:
            raw = _scan_with_pypdf(path)
        except ScannerError:
            raise
        except Exception as exc2:
            raise ScannerError(f"Could not read the PDF: {exc2}") from exc2

    if not raw:
        raise ScannerError("The PDF contains no pages.")

    color_pages = [p for p, v in raw.items() if v["is_color"]]
    mono_pages = [p for p, v in raw.items() if not v["is_color"]]
    return {
        "file": path.name,
        "total_pages": len(raw),
        "method": method,
        "pages": {str(p): ("color" if v["is_color"] else "monochrome") for p, v in raw.items()},
        "color_pages": color_pages,
        "monochrome_pages": mono_pages,
        "color_ranges": compress_ranges(color_pages),
        "monochrome_ranges": compress_ranges(mono_pages),
        "details": {str(p): {"colored_pixel_ratio": v["colored_pixel_ratio"]} for p, v in raw.items()},
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python document_scanner.py <file.pdf>", file=sys.stderr)
        sys.exit(2)
    try:
        print(json.dumps(scan_pdf_colors(sys.argv[1]), indent=2))
    except ScannerError as err:
        print(f"error: {err}", file=sys.stderr)
        sys.exit(1)
