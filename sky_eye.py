#!/usr/bin/env python3
"""Sky Eye Tile Generator — download and preview map tiles from satellite imagery sources."""

from __future__ import annotations

import argparse
import io
import json
import math
import mimetypes
import os
import signal
import sqlite3
import sys
import threading
import time
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from PIL import Image
    _PILLOW = True
except ImportError:
    _PILLOW = False

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
    "noaa": {
        "name": "NOAA Nautical Charts (US)",
        "url": "https://tileservice.charts.noaa.gov/tiles/50000_1/{z}/{x}/{y}.png",
        "attr": "NOAA, Office of Coast Survey",
        "resolution": "Official nautical charts",
        "update_freq": "Periodic",
        "max_zoom": 17,
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

# ── overlay source registry ───────────────────────────────────────────────────

OVERLAY_SOURCES: dict[str, dict] = {
    "openseamap": {
        "name": "OpenSeaMap — Nautical Marks",
        "url": "https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png",
        "attr": "© OpenSeaMap contributors",
        "desc": "Buoys, lights, wrecks, depth contours, shipping lanes — global",
        "blend": "transparent",
        "max_zoom": 18,
    },
    "osm_streets": {
        "name": "OSM Streets & Labels",
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attr": "© OpenStreetMap contributors",
        "desc": "Road network and place labels blended over base imagery",
        "blend": "50pct",
        "max_zoom": 19,
    },
    "opentopomap": {
        "name": "OpenTopoMap — Contours & Terrain",
        "url": "https://tile.opentopomap.org/{z}/{x}/{y}.png",
        "attr": "© OpenTopoMap contributors (CC-BY-SA)",
        "desc": "Topographic contours, elevation shading, terrain — global",
        "blend": "50pct",
        "max_zoom": 17,
    },
    "usgs_topo": {
        "name": "USGS Topo (US only)",
        "url": "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
        "attr": "USGS, The National Map",
        "desc": "Official US topographic map with contours and land cover",
        "blend": "50pct",
        "max_zoom": 16,
    },
    "esri_topo": {
        "name": "Esri World Topo",
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        "attr": "© Esri, HERE, Garmin, FAO, NOAA, USGS",
        "desc": "Global topographic map with contours, land cover, and terrain — non-commercial use",
        "blend": "50pct",
        "max_zoom": 19,
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

# Output format constants
FMT_LOOSE    = "loose"     # z/x/y.png directory tree  — Dragon GUI / any Leaflet app
FMT_MBTILES  = "mbtiles"   # .mbtiles SQLite            — ATAK, WinTAK, QGIS, Mapbox
FMT_ATAK_ZIP = "atak_zip"  # .zip bundle with manifest  — direct ATAK sideload


class DownloadJob:
    def __init__(self, source: str,
                 lat_min: float, lat_max: float, lon_min: float, lon_max: float,
                 zoom_min: int, zoom_max: int,
                 dest_dir: str | Path,
                 output_format: str = FMT_LOOSE,
                 overlays: list[str] | None = None,
                 rate_delay: float = 0.05) -> None:
        self.source = source
        self.dest_dir = Path(dest_dir)
        self.output_format = output_format
        self.overlays = [o for o in (overlays or []) if o in OVERLAY_SOURCES]
        self.zoom_min = zoom_min
        self.zoom_max = zoom_max
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
        self._output_path: str = ""

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
                "output_path": self._output_path,
                "format": self.output_format,
            }

    # ── overlay compositing ───────────────────────────────────────────────────

    def _composite(self, base_data: bytes, z: int, x: int, y: int) -> bytes:
        """Fetch and composite all active overlay tiles onto base_data in order."""
        if not _PILLOW or not self.overlays:
            return base_data
        try:
            base_img = Image.open(io.BytesIO(base_data)).convert("RGBA")
        except Exception:
            return base_data
        for ov_id in self.overlays:
            ov_cfg = OVERLAY_SOURCES[ov_id]
            url = ov_cfg["url"].format(z=z, x=x, y=y)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "SkyEye-TileGen/1.0"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    ov_data = r.read()
            except Exception:
                continue
            try:
                ov_img = Image.open(io.BytesIO(ov_data)).convert("RGBA")
                if ov_img.size != base_img.size:
                    ov_img = ov_img.resize(base_img.size, Image.LANCZOS)
                if ov_cfg["blend"] == "transparent":
                    base_img.paste(ov_img, mask=ov_img.split()[3])
                else:
                    r2, g2, b2, a2 = ov_img.split()
                    a2 = a2.point(lambda v: int(v * 0.5))
                    ov_img = Image.merge("RGBA", (r2, g2, b2, a2))
                    base_img = Image.alpha_composite(base_img, ov_img)
            except Exception:
                continue
        out = io.BytesIO()
        base_img.convert("RGB").save(out, format="PNG")
        return out.getvalue()

    # ── internal writers ──────────────────────────────────────────────────────

    def _save_loose(self, z: int, x: int, y: int, data: bytes) -> bool:
        """Returns True if written, False if skipped (exists)."""
        dest = self.dest_dir / str(z) / str(x) / f"{y}.png"
        if dest.exists():
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True

    def _init_mbtiles(self) -> tuple[sqlite3.Connection, str]:
        self.dest_dir.mkdir(parents=True, exist_ok=True)
        path = str(self.dest_dir / f"{self.source}_z{self.zoom_min}-{self.zoom_max}.mbtiles")
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("""CREATE TABLE IF NOT EXISTS tiles (
            zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER,
            tile_data BLOB,
            UNIQUE(zoom_level, tile_column, tile_row))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT)""")
        src_meta = TILE_SOURCES.get(self.source, {})
        for k, v in [
            ("name",        f"Sky Eye – {src_meta.get('name', self.source)}"),
            ("format",      "png"),
            ("minzoom",     str(self.zoom_min)),
            ("maxzoom",     str(self.zoom_max)),
            ("type",        "overlay"),
            ("description", f"Generated by Sky Eye Tile Generator from {self.source}"),
            ("attribution", src_meta.get("attr", "")),
        ]:
            conn.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (k, v))
        conn.commit()
        return conn, path

    def _write_mbtiles(self, conn: sqlite3.Connection, z: int, x: int, y: int,
                       data: bytes) -> None:
        # MBTiles uses TMS y convention (y flipped)
        tms_y = (2 ** z - 1) - y
        conn.execute("INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)",
                     (z, x, tms_y, sqlite3.Binary(data)))

    def _init_atak_zip(self) -> tuple[zipfile.ZipFile, str]:
        self.dest_dir.mkdir(parents=True, exist_ok=True)
        path = str(self.dest_dir / f"{self.source}_z{self.zoom_min}-{self.zoom_max}_atak.zip")
        src_meta = TILE_SOURCES.get(self.source, {})
        zf = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1)
        # ATAK bundle manifest
        manifest = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<TileSource type="LocalTileServer">\n'
            f'  <Title>Sky Eye – {src_meta.get("name", self.source)}</Title>\n'
            f'  <Attribution>{src_meta.get("attr", "")}</Attribution>\n'
            f'  <MinZoom>{self.zoom_min}</MinZoom>\n'
            f'  <MaxZoom>{self.zoom_max}</MaxZoom>\n'
            '  <TileFormat>png</TileFormat>\n'
            '  <TileLayout>slippy</TileLayout>\n'
            '</TileSource>\n'
        )
        zf.writestr("bundle.xml", manifest)
        return zf, path

    # ── download thread ───────────────────────────────────────────────────────

    def run(self) -> None:
        self._start = time.time()
        url_tmpl = TILE_SOURCES[self.source]["url"]

        # Initialise output based on format
        db_conn: sqlite3.Connection | None = None
        zf: zipfile.ZipFile | None = None
        db_commit_counter = 0

        if self.output_format == FMT_MBTILES:
            db_conn, self._output_path = self._init_mbtiles()
        elif self.output_format == FMT_ATAK_ZIP:
            zf, self._output_path = self._init_atak_zip()
        else:
            self._output_path = str(self.dest_dir)

        try:
            for z, x, y in self.tile_list:
                if self._cancel.is_set():
                    break

                # Loose skip-check
                if self.output_format == FMT_LOOSE:
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
                    if self.overlays:
                        data = self._composite(data, z, x, y)
                    if self.output_format == FMT_LOOSE:
                        self._save_loose(z, x, y, data)
                    elif db_conn is not None:
                        self._write_mbtiles(db_conn, z, x, y, data)
                        db_commit_counter += 1
                        if db_commit_counter >= 200:
                            db_conn.commit()
                            db_commit_counter = 0
                    elif zf is not None:
                        zf.writestr(f"tiles/{z}/{x}/{y}.png", data)
                    with self._lock:
                        self._done += 1
                        self._bytes += len(data)
                else:
                    with self._lock:
                        self._errors += 1

                time.sleep(self.rate_delay)

        finally:
            if db_conn:
                db_conn.commit()
                db_conn.close()
            if zf:
                zf.close()

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

        if path == "/api/overlays":
            self.send_json([
                {"id": "none", "name": "None", "desc": "No overlay", "blend": "none"},
            ] + [
                {"id": k, "name": v["name"], "desc": v["desc"],
                 "blend": v["blend"], "max_zoom": v["max_zoom"]}
                for k, v in OVERLAY_SOURCES.items()
            ])
            return

        if path == "/api/formats":
            self.send_json([
                {"id": FMT_LOOSE,
                 "name": "Loose tiles  (z/x/y.png)",
                 "desc": "Directory tree — use directly with Dragon GUI, kraken_adsb, sky_eye, or any Leaflet app",
                 "ext": "directory"},
                {"id": FMT_MBTILES,
                 "name": "MBTiles  (.mbtiles)",
                 "desc": "Single SQLite file — import into ATAK, WinTAK, QGIS, Mapbox, or RTAK clients",
                 "ext": ".mbtiles"},
                {"id": FMT_ATAK_ZIP,
                 "name": "ATAK Data Package  (.zip)",
                 "desc": "Zipped tile bundle with bundle.xml manifest — sideload directly into ATAK or WinTAK",
                 "ext": ".zip"},
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

        if path.startswith("/overlay/"):
            parts = path.split("/")
            try:
                source = parts[2]
                z, x = int(parts[3]), int(parts[4])
                y = int(parts[5].replace(".png", ""))
            except (IndexError, ValueError):
                self.send_response(400); self.end_headers(); return
            ov = OVERLAY_SOURCES.get(source)
            if not ov:
                self.send_response(404); self.end_headers(); return
            url = ov["url"].format(z=z, x=x, y=y)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "SkyEye-TileGen/1.0"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = r.read()
                self.send_bytes(data, "image/png")
            except Exception:
                self.send_response(404); self.end_headers()
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
                    output_format=body.get("format", FMT_LOOSE),
                    overlays=body.get("overlays") or [],
                    rate_delay=float(body.get("rate_delay", 0.05)),
                )
                # preview always reads from the loose tile directory
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
