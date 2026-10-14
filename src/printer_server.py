"""MCP Printer Server (stdio transport).

Tools
-----
get_printer_diagnostics()
    Polls the printer over IPP (pyipp) and returns state + CMYK ink levels.
execute_advanced_print_job(file_path, range_string, color_mode)
    Prints selected pages. Pipeline order:
      1. IPP Print-Job over the network
           - sends a PDF if the printer advertises application/pdf
           - otherwise rasterizes each page to JPEG (HP Smart Tank models
             commonly do not accept PDF natively)
      2. Windows Print Spooler (pywin32 / GDI) if IPP fails or times out

Rules: stdout belongs to the MCP protocol, so all logging goes to stderr.
No tool ever raises to the client; failures come back as structured dicts
(error_code PRINTER_OFFLINE / COMMUNICATION_TIMEOUT / ...).
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv
from mcp.server import MCPServer
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")  # real environment variables take precedence

logging.basicConfig(
    stream=sys.stderr,
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [printer_server] %(message)s",
)
log = logging.getLogger("printer_server")

mcp = MCPServer("smart-printer")

INK_KEYS = ("black", "cyan", "magenta", "yellow")
LOW_INK_PERCENT = 15


# --------------------------------------------------------------------------- #
# Configuration & errors
# --------------------------------------------------------------------------- #
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Config:
    ip: str
    port: int
    ipp_path: str
    ipp_timeout: int
    job_timeout: int
    windows_printer: str
    upload_dir: Path
    raster_dpi: int
    spooler_dpi: int

    @classmethod
    def from_env(cls) -> "Config":
        path = os.getenv("IPP_PATH", "/ipp/print").strip() or "/ipp/print"
        return cls(
            ip=os.getenv("PRINTER_IP", "").strip(),
            port=_env_int("PRINTER_PORT", 631),
            ipp_path=path if path.startswith("/") else "/" + path,
            ipp_timeout=_env_int("IPP_TIMEOUT_SECONDS", 8),
            job_timeout=_env_int("IPP_JOB_TIMEOUT_SECONDS", 120),
            windows_printer=os.getenv("WINDOWS_PRINTER_NAME", "").strip(),
            upload_dir=Path(os.getenv("UPLOAD_DIR", BASE_DIR / "uploads")).resolve(),
            raster_dpi=_env_int("IPP_RASTER_DPI", 300),
            spooler_dpi=_env_int("SPOOLER_DPI", 200),
        )

    def http_url(self, port: int | None = None) -> str:
        return f"http://{self.ip}:{port or self.port}{self.ipp_path}"

    @property
    def printer_uri(self) -> str:
        return f"ipp://{self.ip}:{self.port}{self.ipp_path}"


class PrinterError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(err: PrinterError, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error_code": err.code, "message": err.message, **extra}


# --------------------------------------------------------------------------- #
# Tool A: diagnostics (pyipp)
# --------------------------------------------------------------------------- #
def _ink_keys_for(marker: Any) -> list[str]:
    name = (getattr(marker, "name", "") or "").lower()
    color = (getattr(marker, "color", "") or "").lstrip("#").upper()
    for key in INK_KEYS:
        if key in name:
            return [key]
    by_hex = {"000000": "black", "00FFFF": "cyan", "FF00FF": "magenta", "FFFF00": "yellow"}
    if color in by_hex:
        return [by_hex[color]]
    if "tri" in name or "colo" in name:  # tri-color cartridge reports one level
        return ["cyan", "magenta", "yellow"]
    return []


async def _fetch_printer(cfg: Config) -> Any:
    from pyipp import IPP  # imported lazily so the server starts even if missing

    ports = [cfg.port] + ([80] if cfg.port == 631 else [])
    last_exc: Exception | None = None
    for port in ports:
        try:
            async with IPP(
                host=cfg.ip,
                port=port,
                base_path=cfg.ipp_path,
                tls=False,
                verify_ssl=False,
                request_timeout=cfg.ipp_timeout,
            ) as ipp:
                return await asyncio.wait_for(ipp.printer(), timeout=cfg.ipp_timeout + 2)
        except Exception as exc:  # noqa: BLE001 - try the next port, then classify
            log.warning("IPP query to %s:%s failed: %s", cfg.ip, port, exc)
            last_exc = exc
    assert last_exc is not None
    raise last_exc


@mcp.tool()
async def get_printer_diagnostics() -> dict[str, Any]:
    """Poll the network printer over IPP.

    Returns ok, status (ONLINE | BUSY | STOPPED | ERROR | OFFLINE | UNKNOWN),
    state_reasons, ink_levels (percent per black/cyan/magenta/yellow, null when
    the printer does not report a value) and, on failure, an error_code
    (PRINTER_OFFLINE, COMMUNICATION_TIMEOUT, CONFIG_ERROR).
    """
    cfg = Config.from_env()
    offline = {"ok": False, "status": "OFFLINE", "ink_levels": dict.fromkeys(INK_KEYS)}
    if not cfg.ip:
        return {**offline, "status": "UNKNOWN", "error_code": "CONFIG_ERROR",
                "message": "PRINTER_IP is not set."}
    try:
        printer = await _fetch_printer(cfg)
    except asyncio.TimeoutError:
        return {**offline, "error_code": "COMMUNICATION_TIMEOUT",
                "message": f"Printer at {cfg.ip} did not answer in time."}
    except Exception as exc:  # noqa: BLE001
        code = "COMMUNICATION_TIMEOUT" if "timeout" in str(exc).lower() else "PRINTER_OFFLINE"
        return {**offline, "error_code": code, "message": f"Cannot reach printer at {cfg.ip}: {exc}"}

    state = getattr(printer, "state", None)
    raw_state = (getattr(state, "printer_state", "") or "").lower()
    raw_reasons = getattr(state, "reasons", None) or []
    if isinstance(raw_reasons, str):
        raw_reasons = raw_reasons.split(",")
    reasons = [r.strip() for r in raw_reasons if r and r.strip() and r.strip() != "none"]

    status = {"idle": "ONLINE", "processing": "BUSY", "stopped": "STOPPED"}.get(raw_state, "UNKNOWN")
    if any(r.endswith("-error") or r in ("offline", "shutdown") for r in reasons):
        status = "ERROR"

    ink: dict[str, int | None] = dict.fromkeys(INK_KEYS)
    detail = []
    for marker in getattr(printer, "markers", None) or []:
        level = getattr(marker, "level", None)
        level = level if isinstance(level, (int, float)) and 0 <= level <= 100 else None
        detail.append({"name": getattr(marker, "name", None), "level": level})
        for key in _ink_keys_for(marker):
            ink[key] = int(level) if level is not None else None

    info = getattr(printer, "info", None)
    return {
        "ok": True,
        "status": status,
        "printer_state": raw_state or None,
        "state_reasons": reasons,
        "state_message": getattr(state, "message", None),
        "printer_name": getattr(info, "name", None),
        "ink_levels": ink,
        "low_ink": [k for k, v in ink.items() if v is not None and v <= LOW_INK_PERCENT],
        "ink_detail": detail,
        "error_code": None,
        "message": "OK",
    }


# --------------------------------------------------------------------------- #
# Minimal IPP/1.1 client for Print-Job (pyipp is query-oriented)
# --------------------------------------------------------------------------- #
OP_PRINT_JOB = 0x0002
OP_GET_PRINTER_ATTRIBUTES = 0x000B


def _attr(tag: int, name: str, value: bytes) -> bytes:
    n = name.encode()
    return bytes([tag]) + struct.pack(">H", len(n)) + n + struct.pack(">H", len(value)) + value


def _build_request(cfg: Config, op: int, op_attrs: bytes = b"", job_attrs: bytes = b"",
                   data: bytes = b"") -> bytes:
    body = struct.pack(">BBHI", 1, 1, op, 1) + b"\x01"
    body += _attr(0x47, "attributes-charset", b"utf-8")
    body += _attr(0x48, "attributes-natural-language", b"en")
    body += _attr(0x45, "printer-uri", cfg.printer_uri.encode())
    body += op_attrs
    if job_attrs:
        body += b"\x02" + job_attrs
    return body + b"\x03" + data


def _parse_response(data: bytes) -> tuple[int, dict[str, list[Any]]]:
    try:
        status = struct.unpack(">H", data[2:4])[0]
        pos, current = 8, None
        attrs: dict[str, list[Any]] = {}
        while pos < len(data):
            tag = data[pos]
            pos += 1
            if tag == 0x03:
                break
            if tag <= 0x0F:
                continue  # group delimiter
            (nlen,) = struct.unpack(">H", data[pos:pos + 2]); pos += 2
            name = data[pos:pos + nlen].decode("utf-8", "replace"); pos += nlen
            (vlen,) = struct.unpack(">H", data[pos:pos + 2]); pos += 2
            raw = data[pos:pos + vlen]; pos += vlen
            if name:
                current = name
                attrs.setdefault(name, [])
            if current is None:
                continue
            if tag in (0x21, 0x23) and vlen == 4:
                value: Any = struct.unpack(">i", raw)[0]
            elif tag == 0x22:
                value = bool(raw[0]) if raw else False
            elif 0x40 <= tag <= 0x4F:
                value = raw.decode("utf-8", "replace")
            else:
                value = raw
            attrs[current].append(value)
        return status, attrs
    except (struct.error, IndexError) as exc:
        raise PrinterError("IPP_PROTOCOL_ERROR", f"Malformed IPP response: {exc}") from exc


async def _ipp_post(cfg: Config, body: bytes, timeout: int) -> bytes:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(cfg.http_url(), data=body,
                                    headers={"Content-Type": "application/ipp"}) as resp:
                if resp.status != 200:
                    raise PrinterError("IPP_HTTP_ERROR", f"Printer answered HTTP {resp.status}")
                return await resp.read()
    except asyncio.TimeoutError as exc:
        raise PrinterError("COMMUNICATION_TIMEOUT", f"Printer at {cfg.ip} timed out") from exc
    except (aiohttp.ClientError, OSError) as exc:
        raise PrinterError("PRINTER_OFFLINE", f"Cannot reach printer at {cfg.ip}: {exc}") from exc


async def _supported_formats(cfg: Config) -> list[str]:
    ops = _attr(0x44, "requested-attributes", b"document-format-supported")
    status, attrs = _parse_response(
        await _ipp_post(cfg, _build_request(cfg, OP_GET_PRINTER_ATTRIBUTES, ops), cfg.ipp_timeout))
    if status >= 0x0100:
        raise PrinterError("IPP_REQUEST_REJECTED", f"Get-Printer-Attributes failed (0x{status:04x})")
    return [str(v) for v in attrs.get("document-format-supported", [])]


async def _send_print_job(cfg: Config, data: bytes, mime: str, color_mode: str, job_name: str) -> None:
    ops = (_attr(0x42, "requesting-user-name", b"smart-printer-assistant")
           + _attr(0x42, "job-name", job_name.encode("utf-8"))
           + _attr(0x49, "document-format", mime.encode()))
    job = _attr(0x44, "print-color-mode", color_mode.encode())
    status, attrs = _parse_response(
        await _ipp_post(cfg, _build_request(cfg, OP_PRINT_JOB, ops, job, data), cfg.job_timeout))
    if status >= 0x0100:
        msg = (attrs.get("status-message") or [""])[0]
        raise PrinterError("IPP_JOB_REJECTED", f"Printer rejected the job (0x{status:04x}) {msg}".strip())


# --------------------------------------------------------------------------- #
# Document helpers
# --------------------------------------------------------------------------- #
_RANGE_RE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$")


def parse_page_ranges(range_string: str, total: int) -> list[int]:
    text = (range_string or "").strip().lower()
    if not text:
        raise PrinterError("INVALID_RANGE", "range_string is empty.")
    if text == "all":
        return list(range(1, total + 1))
    pages: list[int] = []
    for part in text.split(","):
        m = _RANGE_RE.match(part)
        if not m:
            raise PrinterError("INVALID_RANGE", f"Cannot parse page range '{part.strip()}'.")
        start = int(m.group(1))
        end = int(m.group(2) or start)
        if start < 1 or end < start or end > total:
            raise PrinterError("INVALID_RANGE", f"Range '{part.strip()}' is outside 1-{total}.")
        pages.extend(range(start, end + 1))
    return list(dict.fromkeys(pages))  # de-duplicate, keep order


def _validate_file(file_path: str, cfg: Config) -> tuple[Path, int]:
    from pypdf import PdfReader

    path = Path(file_path).expanduser().resolve()
    if cfg.upload_dir not in path.parents:
        raise PrinterError("PATH_NOT_ALLOWED", "File must be inside the configured upload directory.")
    if not path.is_file():
        raise PrinterError("FILE_NOT_FOUND", f"File not found: {path.name}")
    if path.suffix.lower() != ".pdf":
        raise PrinterError("UNSUPPORTED_FILE_TYPE", "Only PDF files can be printed.")
    with open(path, "rb") as fh:
        if fh.read(5) != b"%PDF-":
            raise PrinterError("UNSUPPORTED_FILE_TYPE", "File is not a valid PDF.")
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise PrinterError("ENCRYPTED_PDF", "Encrypted PDFs cannot be printed.")
        return path, len(reader.pages)
    except PrinterError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PrinterError("INVALID_PDF", f"Cannot read PDF: {exc}") from exc


def _extract_pdf_pages(path: Path, pages: list[int]) -> bytes:
    from pypdf import PdfReader, PdfWriter

    reader, writer = PdfReader(str(path)), PdfWriter()
    for p in pages:
        writer.add_page(reader.pages[p - 1])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _render_page(path: Path, page_number: int, dpi: int, mono: bool):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        page = pdf[page_number - 1]
        try:
            image = page.render(scale=dpi / 72).to_pil().convert("RGB")
        finally:
            page.close()
    finally:
        pdf.close()
    return image.convert("L") if mono else image


def _page_jpeg(path: Path, page_number: int, dpi: int, mono: bool) -> bytes:
    buf = io.BytesIO()
    _render_page(path, page_number, dpi, mono).save(buf, "JPEG", quality=92, dpi=(dpi, dpi))
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Pipelines
# --------------------------------------------------------------------------- #
async def _print_via_ipp(cfg: Config, path: Path, pages: list[int], color_mode: str) -> str:
    formats = await _supported_formats(cfg)
    mono = color_mode == "monochrome"
    job_name = f"{path.name} p.{pages[0]}-{pages[-1]}"
    if not formats or "application/pdf" in formats:
        data = await asyncio.to_thread(_extract_pdf_pages, path, pages)
        await _send_print_job(cfg, data, "application/pdf", color_mode, job_name)
        return "ipp-pdf"
    if "image/jpeg" in formats:
        for n in pages:  # one IPP job per page: Print-Job carries a single document
            jpeg = await asyncio.to_thread(_page_jpeg, path, n, cfg.raster_dpi, mono)
            await _send_print_job(cfg, jpeg, "image/jpeg", color_mode, f"{path.name} p.{n}")
        return "ipp-raster"
    raise PrinterError("UNSUPPORTED_FORMAT",
                       f"Printer accepts neither PDF nor JPEG over IPP (offers: {', '.join(formats[:8])}).")


def _print_via_spooler(cfg: Config, path: Path, pages: list[int], color_mode: str) -> None:
    try:
        import win32con
        import win32gui
        import win32print
        import win32ui
        from PIL import ImageWin
    except ImportError as exc:
        raise PrinterError("SPOOLER_UNAVAILABLE", f"pywin32 is not available: {exc}") from exc

    mono = color_mode == "monochrome"
    try:
        name = cfg.windows_printer or win32print.GetDefaultPrinter()
        handle = win32print.OpenPrinter(name)
        try:
            devmode = win32print.GetPrinter(handle, 2).get("pDevMode")
            if devmode is not None:
                devmode.Fields |= win32con.DM_COLOR
                devmode.Color = win32con.DMCOLOR_MONOCHROME if mono else win32con.DMCOLOR_COLOR
        finally:
            win32print.ClosePrinter(handle)
        hdc = win32gui.CreateDC("WINSPOOL", name, devmode)
        dc = win32ui.CreateDCFromHandle(hdc)
    except Exception as exc:  # noqa: BLE001 - pywintypes.error etc.
        raise PrinterError("SPOOLER_FAILED", f"Cannot open Windows printer: {exc}") from exc

    started = False
    try:
        dc.StartDoc(f"{path.name} p.{pages[0]}-{pages[-1]}")
        started = True
        horz, vert = dc.GetDeviceCaps(win32con.HORZRES), dc.GetDeviceCaps(win32con.VERTRES)
        for n in pages:
            img = _render_page(path, n, cfg.spooler_dpi, mono)
            scale = min(horz / img.width, vert / img.height)
            w, h = int(img.width * scale), int(img.height * scale)
            x, y = (horz - w) // 2, (vert - h) // 2
            dc.StartPage()
            ImageWin.Dib(img).draw(dc.GetHandleOutput(), (x, y, x + w, y + h))
            dc.EndPage()
        dc.EndDoc()
    except Exception as exc:  # noqa: BLE001
        if started:
            try:
                dc.AbortDoc()
            except Exception:  # noqa: BLE001
                pass
        raise PrinterError("SPOOLER_FAILED", f"Windows spooler failed: {exc}") from exc
    finally:
        dc.DeleteDC()


# --------------------------------------------------------------------------- #
# Tool B: print job
# --------------------------------------------------------------------------- #
@mcp.tool()
async def execute_advanced_print_job(file_path: str, range_string: str, color_mode: str) -> dict[str, Any]:
    """Print selected pages of a PDF.

    Args:
        file_path: Absolute path of a PDF inside the upload directory.
        range_string: 1-based pages/ranges such as '1-2, 5' (or 'all').
        color_mode: 'color' or 'monochrome'.

    Tries IPP first; on any IPP failure/timeout falls back to the native
    Windows Print Spooler. Returns ok, pipeline, pages, warnings, error_code.
    """
    cfg = Config.from_env()
    mode = (color_mode or "").strip().lower()
    mode = {"grayscale": "monochrome", "greyscale": "monochrome", "bw": "monochrome"}.get(mode, mode)
    if mode not in ("color", "monochrome"):
        return _fail(PrinterError("INVALID_COLOR_MODE", "color_mode must be 'color' or 'monochrome'."))
    try:
        path, total = await asyncio.to_thread(_validate_file, file_path, cfg)
        pages = parse_page_ranges(range_string, total)
    except PrinterError as err:
        return _fail(err)

    warnings: list[str] = []
    if not cfg.ip:
        warnings.append("PRINTER_IP not set; skipping IPP and using the Windows spooler.")
        log.warning(warnings[-1])
    else:
        try:
            pipeline = await _print_via_ipp(cfg, path, pages, mode)
            return {"ok": True, "pipeline": pipeline, "pages": pages, "color_mode": mode,
                    "warnings": warnings, "error_code": None, "message": "Print job submitted."}
        except PrinterError as err:
            msg = f"IPP failed ({err.code}): {err.message}. Falling back to Windows spooler."
        except Exception as exc:  # noqa: BLE001
            msg = f"IPP failed unexpectedly ({exc}). Falling back to Windows spooler."
        log.warning(msg)
        warnings.append(msg)

    try:
        await asyncio.to_thread(_print_via_spooler, cfg, path, pages, mode)
    except PrinterError as err:
        return _fail(err, pipeline="windows-spooler", pages=pages, warnings=warnings)
    except Exception as exc:  # noqa: BLE001
        return _fail(PrinterError("SPOOLER_FAILED", str(exc)), pipeline="windows-spooler",
                     pages=pages, warnings=warnings)
    return {"ok": True, "pipeline": "windows-spooler", "pages": pages, "color_mode": mode,
            "warnings": warnings, "error_code": None, "message": "Print job sent to the Windows spooler."}


if __name__ == "__main__":
    mcp.run(transport="stdio")
