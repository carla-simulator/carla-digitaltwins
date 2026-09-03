"""Serve the low-fly tile pyramids and their region feedback over plain HTTP.

    python tools/review_server.py --root out/lowfly --port 8765
    # then open http://localhost:8765/

A whole map at 1 cm/px is ~1 GB of WebP, far past anything that can be embedded in a page, so the
pyramid stays on disk and the viewer (``tools/lowfly_viewer.py``) streams it a tile at a time.
Stdlib only -- ``ThreadingHTTPServer`` -- so it runs anywhere the twin tooling does.

Routes::

    GET    /                                 index of the maps under --root
    GET    /<Map>/                           the viewer page (twin overlays extracted once, cached)
    GET    /tiles/<Map>/z<z>/<i>_<j>.webp    one pyramid tile, cached for an hour
    GET    /l0/<Map>.jpg                     the coarse 20 cm/px mosaic from --l0-root, if there
    GET    /api/<Map>/manifest               manifest.json
    GET    /api/<Map>/regions                every region, oldest key order
    POST   /api/<Map>/regions                create; the server assigns id and created_at
    PUT    /api/<Map>/regions/<id>           merge fields into a region (status, replies, comment…)
    DELETE /api/<Map>/regions/<id>           remove one

Regions live in ``<root>/<Map>/regions.json`` -- one JSON list, rewritten atomically (temp file +
``os.replace``) under a per-map lock, so a crash mid-write cannot truncate the feedback.  The
server derives ``coords_carla`` / the centroids from ``coords_model`` itself (CARLA is ``(x, -y)``),
so a client cannot store an inconsistent pair.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import re
import secrets
import sys
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger("review_server")

MAX_BODY = 4 << 20            # a region is a few kB; anything past this is a mistake
TILE_MAX_AGE = 3600
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
TILE_RE = re.compile(r"^z(\d{1,2})/(\d{1,6})_(\d{1,6})\.webp$")
ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _load_viewer():
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("lowfly_viewer", here / "lowfly_viewer.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


viewer = _load_viewer()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------------------------- state

class MapStore:
    """One map's pyramid directory: the manifest, the cached page and the regions file."""

    def __init__(self, name: str, d: Path, l0_root: Path | None = None):
        self.name = name
        self.dir = d
        self.l0_root = l0_root
        self.manifest: dict[str, Any] = json.loads((d / "manifest.json").read_text())
        self.manifest_mtime = (d / "manifest.json").stat().st_mtime
        self.lock = threading.Lock()
        self.twin: dict[str, Any] | None = None
        self._page: str | None = None

    # ---- page -------------------------------------------------------------------------
    def load_twin(self) -> None:
        raw = self.manifest.get("twin_dir") or ""
        if not raw:
            return
        td = Path(raw)
        if not td.is_absolute():
            td = Path(__file__).resolve().parent.parent / raw    # relative to the TwinModel root
        if not (td / "model.json").exists():
            log.warning("%s: twin dir %s missing, page will be mosaic-only", self.name, td)
            return
        try:
            self.twin = viewer.extract_twin(td, self.name, self.name)
            log.info("%s: twin overlays from %s (%s)", self.name, td,
                     " ".join("%s=%s" % kv for kv in sorted(self.twin["counts"].items())))
        except Exception as exc:                                   # noqa: BLE001 - never fatal
            log.warning("%s: could not read twin %s: %s", self.name, td, exc)

    def l0(self) -> dict | None:
        """The 20 cm/px whole-map mosaic from ``--l0-root``, drawn under the pyramid."""
        if self.l0_root is None:
            return None
        meta = self.l0_root / ("topview_%s.json" % self.name)
        jpg = self.l0_root / ("topview_%s.jpg" % self.name)
        if not (meta.exists() and jpg.exists()):
            return None
        try:
            raw = json.loads(meta.read_text())
            return {"url": "/l0/%s.jpg" % self.name, "bounds": [float(v) for v in raw["bounds"]]}
        except Exception as exc:                                   # noqa: BLE001
            log.warning("%s: unusable l0 mosaic: %s", self.name, exc)
            return None

    def page(self) -> str:
        if self._page is None:
            self._page = viewer.build_page(self.name, self.manifest, self.twin, l0=self.l0())
        return self._page

    # ---- regions ----------------------------------------------------------------------
    @property
    def regions_path(self) -> Path:
        return self.dir / "regions.json"

    def read(self) -> list[dict]:
        p = self.regions_path
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError as exc:
            log.error("%s: regions.json is not JSON (%s); serving empty", self.name, exc)
            return []
        return data if isinstance(data, list) else list(data.get("regions") or [])

    def _write(self, rows: list[dict]) -> None:
        p = self.regions_path
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp%d" % os.getpid())
        tmp.write_text(json.dumps(rows, indent=2))
        os.replace(tmp, p)

    def create(self, doc: dict) -> dict:
        with self.lock:
            rows = self.read()
            rid = "%s-%s" % (self.name, secrets.token_hex(4))
            while any(r.get("id") == rid for r in rows):
                rid = "%s-%s" % (self.name, secrets.token_hex(4))
            stamp = now_iso()
            out = viewer.region_defaults(doc, map_name=self.name, carla_map=self.name)
            out.update({"id": rid, "created_at": stamp, "updated_at": stamp})
            rows.append(out)
            self._write(rows)
            return out

    def update(self, rid: str, fields: dict) -> dict | None:
        with self.lock:
            rows = self.read()
            for n, r in enumerate(rows):
                if r.get("id") != rid:
                    continue
                merged = dict(r)
                merged.update({k: v for k, v in fields.items()
                               if k not in ("id", "created_at")})
                out = viewer.region_defaults(merged, map_name=self.name, carla_map=self.name)
                out.update({"id": rid, "created_at": r.get("created_at") or now_iso(),
                            "updated_at": now_iso()})
                rows[n] = out
                self._write(rows)
                return out
            return None

    def delete(self, rid: str) -> bool:
        with self.lock:
            rows = self.read()
            keep = [r for r in rows if r.get("id") != rid]
            if len(keep) == len(rows):
                return False
            self._write(keep)
            return True


def discover(root: Path, l0_root: Path | None) -> dict[str, MapStore]:
    """Every immediate subdirectory of ``root`` that holds a ``manifest.json``."""
    stores: dict[str, MapStore] = {}
    for d in sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []:
        if not (d / "manifest.json").exists() or not NAME_RE.match(d.name):
            continue
        try:
            stores[d.name] = MapStore(d.name, d, l0_root)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("skipping %s: %s", d, exc)
    return stores


# ----------------------------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "TwinReview/1.0"
    protocol_version = "HTTP/1.1"
    stores: dict[str, MapStore] = {}

    # ---- plumbing ---------------------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:      # noqa: A003 - stdlib hook
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: Any) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _html(self, code: int, text: str) -> None:
        self._send(code, text.encode(), "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def _error(self, code: int, msg: str) -> None:
        log.warning("%s %s -> %d %s", self.command, self.path, code, msg)
        self._json(code, {"error": msg, "status": code})

    def _body(self) -> Any:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("body too large (%d bytes)" % n)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        obj = json.loads(raw.decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("expected a JSON object")
        return obj

    def _refresh(self) -> None:
        """Pick up maps whose manifest.json appeared after start-up (a flight finishing)."""
        root = getattr(self, "root", None)
        if root is None:
            return
        for name, store in discover(root, getattr(self, "l0_root", None)).items():
            if name not in self.stores:
                store.load_twin()
                self.stores[name] = store
                log.info("%s: discovered %s", name, store.dir)

    def _store(self, name: str) -> MapStore | None:
        if not NAME_RE.match(name or ""):
            return None
        if name not in self.stores:
            self._refresh()
        store = self.stores.get(name)
        if store is not None:
            try:  # a re-flight rewrites manifest.json: pick up the new grid/size and rebuild the page
                mtime = (store.dir / "manifest.json").stat().st_mtime
            except OSError:
                mtime = store.manifest_mtime
            if mtime != store.manifest_mtime:
                fresh = MapStore(name, store.dir, store.l0_root)
                fresh.twin = store.twin
                self.stores[name] = store = fresh
                log.info("%s: manifest changed, reloaded", name)
        return store

    @staticmethod
    def _parts(path: str) -> list[str]:
        return [p for p in path.split("?")[0].split("#")[0].split("/") if p]

    # ---- routes -----------------------------------------------------------------------
    def do_GET(self) -> None:            # noqa: N802 - stdlib hook
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass          # the viewer cancels tile requests it has scrolled away from
        except Exception as exc:         # noqa: BLE001 - one bad request must not kill the thread
            log.exception("GET %s failed", self.path)
            self._error(500, str(exc))

    do_HEAD = do_GET

    def _route_get(self) -> None:
        raw = self.path.split("?")[0]
        parts = self._parts(raw)
        if not parts:
            self._refresh()
            rows = [{"name": n, "manifest": s.manifest, "regions": len(s.read())}
                    for n, s in sorted(self.stores.items())]
            self._html(200, viewer.build_index(rows))
            return
        head = parts[0]
        if head == "favicon.ico":       # the pages carry an inline icon; this is for direct hits
            self._send(200, viewer.FAVICON_SVG.encode(), "image/svg+xml",
                       {"Cache-Control": "max-age=%d" % TILE_MAX_AGE})
            return

        if head == "tiles":
            self._tile(parts[1:], raw)
            return
        if head == "l0" and len(parts) == 2:
            self._l0(parts[1])
            return
        if head == "api":
            self._api_get(parts[1:])
            return
        if len(parts) == 1:
            store = self._store(head)
            if store is None:
                self._error(404, "no such map: %s" % head)
                return
            if not raw.endswith("/"):
                self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                self.send_header("Location", "/%s/" % head)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._html(200, store.page())
            return
        self._error(404, "unknown path")

    def _tile(self, rest: list[str], raw: str) -> None:
        if len(rest) != 3:
            self._error(404, "tile path is /tiles/<Map>/z<z>/<i>_<j>.webp")
            return
        store = self._store(rest[0])
        m = TILE_RE.match("%s/%s" % (rest[1], rest[2]))
        if store is None or m is None:
            self._error(404, "bad tile request")
            return
        p = store.dir / ("z%d" % int(m.group(1))) / ("%d_%d.webp" % (int(m.group(2)), int(m.group(3))))
        try:
            resolved = p.resolve()
            resolved.relative_to(store.dir.resolve())
        except (OSError, ValueError):
            self._error(404, "outside the pyramid")
            return
        if not resolved.is_file():
            # a missing tile is normal: the grid is padded out past the edge of the map
            log.debug("tile miss %s", raw)
            self._send(404, b"", "application/octet-stream", {"Cache-Control": "max-age=60"})
            return
        blob = resolved.read_bytes()
        log.debug("tile %s (%d B)", raw, len(blob))
        self._send(200, blob, "image/webp", {"Cache-Control": "max-age=%d" % TILE_MAX_AGE})

    def _l0(self, fname: str) -> None:
        name = fname[:-4] if fname.endswith(".jpg") else fname
        store = self._store(name)
        if store is None or store.l0_root is None:
            self._error(404, "no coarse mosaic")
            return
        p = (store.l0_root / ("topview_%s.jpg" % name)).resolve()
        try:
            p.relative_to(store.l0_root.resolve())
        except ValueError:
            self._error(404, "outside the mosaic root")
            return
        if not p.is_file():
            self._error(404, "no coarse mosaic")
            return
        self._send(200, p.read_bytes(), "image/jpeg",
                   {"Cache-Control": "max-age=%d" % TILE_MAX_AGE})

    def _api_get(self, rest: list[str]) -> None:
        if len(rest) < 2:
            self._error(404, "api path is /api/<Map>/{manifest,regions}")
            return
        store = self._store(rest[0])
        if store is None:
            self._error(404, "no such map: %s" % rest[0])
            return
        if rest[1] == "manifest" and len(rest) == 2:
            self._json(200, store.manifest)
            return
        if rest[1] == "regions" and len(rest) == 2:
            self._json(200, store.read())
            return
        if rest[1] == "regions" and len(rest) == 3:
            for r in store.read():
                if r.get("id") == rest[2]:
                    self._json(200, r)
                    return
            self._error(404, "no such region")
            return
        self._error(404, "unknown api path")

    def do_POST(self) -> None:           # noqa: N802
        self._mutate("POST")

    def do_PUT(self) -> None:            # noqa: N802
        self._mutate("PUT")

    def do_DELETE(self) -> None:         # noqa: N802
        self._mutate("DELETE")

    def _mutate(self, verb: str) -> None:
        try:
            parts = self._parts(self.path)
            if len(parts) < 3 or parts[0] != "api" or parts[2] != "regions":
                self._error(404, "regions live at /api/<Map>/regions")
                return
            store = self._store(parts[1])
            if store is None:
                self._error(404, "no such map: %s" % parts[1])
                return
            rid = parts[3] if len(parts) > 3 else None
            if rid is not None and not ID_RE.match(rid):
                self._error(400, "bad region id")
                return
            if verb == "POST":
                if rid is not None:
                    self._error(405, "POST creates; use PUT to update")
                    return
                doc = store.create(self._body())
                log.info("%s: created %s (%s, %s)", store.name, doc["id"], doc["kind"], doc["title"])
                self._json(201, doc)
                return
            if rid is None:
                self._error(405, "%s wants /api/<Map>/regions/<id>" % verb)
                return
            if verb == "PUT":
                doc = store.update(rid, self._body())
                if doc is None:
                    self._error(404, "no such region")
                    return
                log.info("%s: updated %s (status=%s, %d replies)", store.name, rid, doc["status"],
                         len(doc["replies"]))
                self._json(200, doc)
                return
            if not store.delete(rid):
                self._error(404, "no such region")
                return
            log.info("%s: deleted %s", store.name, rid)
            self._json(200, {"deleted": rid})
        except (ValueError, json.JSONDecodeError) as exc:
            self._error(400, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:         # noqa: BLE001
            log.exception("%s %s failed", verb, self.path)
            self._error(500, str(exc))


def make_server(root: Path, host: str, port: int, l0_root: Path | None = None) -> ThreadingHTTPServer:
    """Bind the server (port 0 picks a free one) with the maps under ``root`` already loaded."""
    stores = discover(root, l0_root)
    for s in stores.values():
        s.load_twin()
    handler = type("BoundHandler", (Handler,), {"stores": stores, "root": root, "l0_root": l0_root})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.stores = stores            # type: ignore[attr-defined]
    return httpd


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default="out/lowfly", help="directory of <Map>/manifest.json pyramids")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--l0-root", default=None,
                    help="carla_topview.py output (out/review); its 20 cm/px mosaic is drawn "
                         "under the pyramid while tiles load")
    ap.add_argument("--verbose", action="store_true", help="log every tile request too")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    here = Path(__file__).resolve().parent.parent
    root = Path(args.root)
    if not root.is_absolute():
        root = here / root
    l0 = None
    if args.l0_root:
        l0 = Path(args.l0_root)
        if not l0.is_absolute():
            l0 = here / l0
    httpd = make_server(root, args.host, args.port, l0)
    stores = httpd.stores                                    # type: ignore[attr-defined]
    if not stores:
        log.warning("no pyramids under %s -- the index will be empty", root)
    for name, s in sorted(stores.items()):
        log.info("%-14s %d levels, %s leaves, %.0f MB, %d regions", name,
                 s.manifest.get("levels", 0), s.manifest.get("leaf_tiles", "?"),
                 (s.manifest.get("bytes") or 0) / 1e6, len(s.read()))
    host = args.host if args.host not in ("0.0.0.0", "::") else "localhost"
    log.info("serving %s on http://%s:%d/", root, host, httpd.server_address[1])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("bye")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
