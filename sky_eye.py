#!/usr/bin/env python3
"""Sky Eye Tile Generator — download and preview map tiles from satellite imagery sources."""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import signal
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── tile source registry ──────────────────────────────────────────────────────

TILE_SOURCES: dict[str, dict] = {
    "esri": {
        "name": "Esri World Imagery",
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "attr": "© Esri, DigitalGlobe, GeoEye, Earthstar Geographics",
        "resolution": "~0.5m urban / ~1m rural",
        "update_freq": "Monthly",
        "max_zoom": 19,
        "license": "Non-commercial use",
    },
    "usgs": {
        "name": "USGS Imagery (US only)",
        "url": "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
        "attr": "USGS, The National Map",
        "resolution": "~1m",
        "update_freq": "Varies by region",
        "max_zoom": 16,
        "license": "Public domain (US Gov)",
    },
    "osm": {
        "name": "OpenStreetMap",
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attr": "© OpenStreetMap contributors",
        "resolution": "Vector (rendered)",
        "update_freq": "Continuous",
        "max_zoom": 19,
        "license": "ODbL — bulk download discouraged",
    },
}

DEFAULT_SSD_DIR = Path.home() / "tiles" / "sky_eye"

# ── slippy tile math ──────────────────────────────────────────────────────────

def deg2tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    lat_r = math.radians(lat)
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def tiles_in_bbox(lat_min: float, lat_max: float, lon_min: float, lon_max: float,
                  zoom: int) -> list[tuple[int, int, int]]:
    """(z, x, y) tiles covering bbox at zoom; OSM slippy convention."""
    x1, y1 = deg2tile(lat_max, lon_min, zoom)   # NW (higher lat → lower y)
    x2, y2 = deg2tile(lat_min, lon_max, zoom)   # SE
    x1, x2 = sorted([x1, x2])
    y1, y2 = sorted([y1, y2])
    max_t = 2 ** zoom - 1
    x1, x2 = max(0, x1), min(max_t, x2)
    y1, y2 = max(0, y1), min(max_t, y2)
    return [(zoom, x, y) for x in range(x1, x2 + 1) for y in range(y1, y2 + 1)]


def count_tiles(lat_min: float, lat_max: float, lon_min: float, lon_max: float,
                zoom_min: int, zoom_max: int) -> int:
    return sum(len(tiles_in_bbox(lat_min, lat_max, lon_min, lon_max, z))
               for z in range(zoom_min, zoom_max + 1))


# ── removable media detection ─────────────────────────────────────────────────

def find_removable_mounts() -> list[dict]:
    user = os.environ.get("USER", os.environ.get("LOGNAME", ""))
    results = []
    for base in [f"/run/media/{user}", f"/media/{user}", "/media"]:
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            full = os.path.join(base, name)
            if not os.path.ismount(full):
                continue
            try:
                s = os.statvfs(full)
                free_gb = round(s.f_bavail * s.f_frsize / 1e9, 1)
                total_gb = round(s.f_blocks * s.f_frsize / 1e9, 1)
            except OSError:
                free_gb = total_gb = 0.0
            results.append({"path": full, "label": name,
                             "free_gb": free_gb, "total_gb": total_gb})
    return results


# ── download job ──────────────────────────────────────────────────────────────

class DownloadJob:
    def __init__(self, source: str,
                 lat_min: float, lat_max: float, lon_min: float, lon_max: float,
                 zoom_min: int, zoom_max: int,
                 dest_dir: str | Path,
                 rate_delay: float = 0.05) -> None:
        self.source = source
        self.dest_dir = Path(dest_dir)
        self.rate_delay = rate_delay
        self._cancel = threading.Event()

        self.tile_list: list[tuple[int, int, int]] = []
        for z in range(zoom_min, zoom_max + 1):
            self.tile_list.extend(tiles_in_bbox(lat_min, lat_max, lon_min, lon_max, z))

        self.total = len(self.tile_list)
        self._done = 0
        self._skipped = 0
        self._errors = 0
        self._bytes = 0
        self._start: float | None = None
        self._finished = False
        self._lock = threading.Lock()

    @property
    def finished(self) -> bool:
        return self._finished

    def cancel(self) -> None:
        self._cancel.set()

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = time.time() - (self._start or time.time())
            processed = self._done + self._skipped + self._errors
            rate = processed / elapsed if elapsed > 1 else 0
            remaining = self.total - processed
            eta = round(remaining / rate) if rate > 0.1 else None
            return {
                "total": self.total,
                "done": self._done,
                "skipped": self._skipped,
                "errors": self._errors,
                "bytes": self._bytes,
                "elapsed": round(elapsed),
                "rate": round(rate, 1),
                "eta": eta,
                "finished": self._finished,
                "cancelled": self._cancel.is_set(),
                "dest": str(self.dest_dir),
            }

    def run(self) -> None:
        self._start = time.time()
        url_tmpl = TILE_SOURCES[self.source]["url"]

        for z, x, y in self.tile_list:
            if self._cancel.is_set():
                break

            dest = self.dest_dir / str(z) / str(x) / f"{y}.png"
            if dest.exists():
                with self._lock:
                    self._skipped += 1
                continue

            url = url_tmpl.format(z=z, x=x, y=y)
            data: bytes | None = None

            for attempt in range(3):
                try:
                    req = urllib.request.Request(
                        url, headers={"User-Agent": "SkyEye-TileGen/1.0"})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        data = r.read()
                    break
                except Exception:
                    if attempt < 2:
                        time.sleep(1.0)

            if data:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                with self._lock:
                    self._done += 1
                    self._bytes += len(data)
            else:
                with self._lock:
                    self._errors += 1

            time.sleep(self.rate_delay)

        self._finished = True


# ── tile proxy ────────────────────────────────────────────────────────────────

_tile_cache: dict[str, bytes] = {}
_tile_lock = threading.Lock()
_preview_dir: Path | None = None


def fetch_remote_tile(z: int, x: int, y: int, source: str) -> bytes | None:
    src = TILE_SOURCES.get(source, TILE_SOURCES["esri"])
    url = src["url"].format(z=z, x=x, y=y)
    key = f"{source}/{z}/{x}/{y}"
    with _tile_lock:
        if key in _tile_cache:
            return _tile_cache[key]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SkyEye-TileGen/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = r.read()
        with _tile_lock:
            if len(_tile_cache) < 3000:
                _tile_cache[key] = data
        return data
    except Exception:
        return None


# ── HTTP handler ──────────────────────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"


class Handler(BaseHTTPRequestHandler):
    current_job: DownloadJob | None = None
    ssd_dir: Path = DEFAULT_SSD_DIR

    def log_message(self, fmt, *args):
        pass

    def send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data: bytes, mime: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self.send_bytes((STATIC_DIR / "index.html").read_bytes(),
                            "text/html; charset=utf-8")
            return

        if path == "/api/sources":
            self.send_json([
                {"id": k, "name": v["name"], "resolution": v["resolution"],
                 "update_freq": v["update_freq"], "max_zoom": v["max_zoom"],
                 "license": v["license"], "attr": v["attr"]}
                for k, v in TILE_SOURCES.items()
            ])
            return

        if path == "/api/estimate":
            try:
                n = count_tiles(
                    float(params["lat_min"][0]), float(params["lat_max"][0]),
                    float(params["lon_min"][0]), float(params["lon_max"][0]),
                    int(params["zoom_min"][0]), int(params["zoom_max"][0]),
                )
                self.send_json({
                    "tiles": n,
                    "est_mb": round(n * 15 / 1024, 1),
                    "est_sec": round(n * 0.07),
                })
            except (KeyError, ValueError, TypeError) as e:
                self.send_json({"error": str(e)}, 400)
            return

        if path == "/api/mounts":
            self.send_json({
                "ssd_dir": str(self.ssd_dir),
                "mounts": find_removable_mounts(),
            })
            return

        if path == "/api/progress":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    job = Handler.current_job
                    if job is None:
                        msg = b"data: " + json.dumps({"idle": True}).encode() + b"\n\n"
                    else:
                        msg = b"data: " + json.dumps(job.snapshot()).encode() + b"\n\n"
                    self.wfile.write(msg)
                    self.wfile.flush()
                    if job and job.finished:
                        break
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if path.startswith("/tiles/"):
            parts = path.split("/")
            try:
                if len(parts) != 6:
                    raise ValueError("expected /tiles/{source}/{z}/{x}/{y}.png")
                source = parts[2]
                z, x = int(parts[3]), int(parts[4])
                y = int(parts[5].replace(".png", ""))
            except (IndexError, ValueError):
                self.send_response(400)
                self.end_headers()
                return

            if source == "preview":
                dest = (_preview_dir / str(z) / str(x) / f"{y}.png"
                        if _preview_dir else None)
                if dest and dest.exists():
                    self.send_bytes(dest.read_bytes(), "image/png")
                else:
                    self.send_response(404)
                    self.end_headers()
                return

            data = fetch_remote_tile(z, x, y, source)
            if data:
                self.send_bytes(data, "image/png")
            else:
                self.send_response(404)
                self.end_headers()
            return

        fpath = STATIC_DIR / path.lstrip("/")
        if fpath.is_file():
            mime, _ = mimetypes.guess_type(str(fpath))
            self.send_bytes(fpath.read_bytes(), mime or "application/octet-stream")
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        global _preview_dir
        if urlparse(self.path).path == "/api/download":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            try:
                job = DownloadJob(
                    source=body["source"],
                    lat_min=float(body["lat_min"]),
                    lat_max=float(body["lat_max"]),
                    lon_min=float(body["lon_min"]),
                    lon_max=float(body["lon_max"]),
                    zoom_min=int(body["zoom_min"]),
                    zoom_max=int(body["zoom_max"]),
                    dest_dir=body.get("dest", str(self.ssd_dir)),
                    rate_delay=float(body.get("rate_delay", 0.05)),
                )
                _preview_dir = job.dest_dir
                Handler.current_job = job
                threading.Thread(target=job.run, daemon=True).start()
                self.send_json({"status": "started", "total": job.total})
            except (KeyError, ValueError) as e:
                self.send_json({"error": str(e)}, 400)
            return
        self.send_response(404)
        self.end_headers()

    def do_DELETE(self) -> None:
        if urlparse(self.path).path == "/api/download":
            if Handler.current_job:
                Handler.current_job.cancel()
                self.send_json({"status": "cancelled"})
            else:
                self.send_json({"status": "no_job"})
            return
        self.send_response(404)
        self.end_headers()


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Sky Eye Tile Generator")
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--ssd-dir", default=str(DEFAULT_SSD_DIR),
                    help=f"Default SSD tile destination (default: {DEFAULT_SSD_DIR})")
    ap.add_argument("--no-browser", action="store_true",
                    help="Do not open browser on startup")
    args = ap.parse_args()

    Handler.ssd_dir = Path(args.ssd_dir)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(0))

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"[sky_eye] Listening on {url}")
    print(f"[sky_eye] Default tile dir: {Handler.ssd_dir}")

    if not args.no_browser:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    server.serve_forever()


if __name__ == "__main__":
    main()
