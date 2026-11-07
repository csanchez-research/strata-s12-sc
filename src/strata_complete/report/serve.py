"""Live provenance report (``serve`` command).

The page of ``report`` over HTTP, kept up to date: a background thread re-reads the
tiles every ``refresh`` seconds and the page fetches new data when its signature
changes. Detail and previews are made on request, only for tiles in the current data.
A flag or note goes to ``review.json`` on the output folder, and to ``sc_notes.jsonld``
(``notes.py``) on a release.

Routes: ``GET /``, ``/signature``, ``/data.json``, ``/tile?path=``,
``/preview?path=&kind=rgb|swir|s1``, ``/prov/<tile>.prov.json``; ``POST /review``
(``{"name", "flag", "note"}``) and ``/shutdown``. It listens on all interfaces inside
the container; ``docker-compose.yml`` publishes it on the host's loopback only.
"""

from __future__ import annotations

import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import parse_qs, unquote, urlsplit

from strata_complete import notes
from strata_complete.report import page
from strata_complete.report.source import open_source
from strata_complete.runtime.progress import say

if TYPE_CHECKING:
    from pathlib import Path

MAX_BODY = 64 * 1024


class _State:
    """Current data, rebuilt in the background; shared by the request threads."""

    def __init__(self, folder: Path, refresh: float) -> None:
        self.folder, self.refresh = folder, refresh
        self.source = open_source(folder)
        self.cache: page.TileCache = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.data = page.payload(self.source, self.cache)
        self.paths = {t["path"] for t in self.data["tiles"]}

    def rebuild(self) -> None:
        data = page.payload(self.source, self.cache, progress=False)
        with self.lock:
            before = len(self.data["tiles"])
            self.data = data
            self.paths = {t["path"] for t in data["tiles"]}
        if len(data["tiles"]) != before:
            say(f"[serve] {len(data['tiles'])} tiles ({before} before)")

    def loop(self) -> None:
        while not self.stop.wait(self.refresh):
            try:
                self.rebuild()
            except Exception as exc:  # noqa: BLE001 - keep serving the last good data
                say(f"[serve] refresh failed, keeping the last data: {type(exc).__name__}: {exc}")

    def snapshot(self) -> dict:
        with self.lock:
            return self.data


class _Handler(BaseHTTPRequestHandler):
    state: ClassVar[_State]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass  # no access log on the console

    def _send(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(json.dumps(obj).encode("utf-8"), "application/json", status)

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query)
        if path == "/":
            html = page.render(self.state.snapshot(), live=True)
            self._send(html.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/signature":
            self._json({"signature": self.state.snapshot()["signature"]})
        elif path.startswith("/prov/"):
            self._prov(unquote(path.removeprefix("/prov/")))
        elif path in ("/tile", "/preview"):
            self._tile(path, query)
        elif path == "/data.json":
            self._json(self.state.snapshot())
        else:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        path = self.path.split("?", 1)[0]
        if path == "/review":
            self._review()
        elif path == "/shutdown":
            self._json({"stopping": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _prov(self, rel: str) -> None:
        """A tile's PROV-JSON document, for a tile in the current data only."""
        tif = rel.removesuffix(".prov.json") + ".tif"
        if not rel.endswith(".prov.json") or tif not in self.state.paths:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            self._send(self.state.source.read(rel), "application/json")
        except (OSError, KeyError):
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _tile(self, route: str, query: dict[str, list[str]]) -> None:
        """Detail (JSON) or one preview (JPEG) of a tile in the current data, made now."""
        tif = (query.get("path") or [""])[0]
        kind = (query.get("kind") or ["rgb"])[0]
        if tif not in self.state.paths or kind not in page.PREVIEW_KINDS:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            if route == "/tile":
                self._json(page.detail(self.state.source, tif))
                return
            jpeg = page.preview(self.state.source, tif, kind)
        except (OSError, KeyError, ValueError) as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if jpeg is None:
            self._json({"error": "no preview for this tile"}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpeg)))
        self.send_header("Cache-Control", "no-store")  # made for this page only
        self.end_headers()
        self.wfile.write(jpeg)

    def _review(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            self._json({"error": "empty or too large"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            body = json.loads(self.rfile.read(length))
            name, flag, note = body["name"], bool(body.get("flag")), str(body.get("note", ""))
        except (ValueError, KeyError, TypeError):
            self._json({"error": "expected {name, flag, note}"}, HTTPStatus.BAD_REQUEST)
            return
        tiles = {t["name"]: t for t in self.state.snapshot()["tiles"]}
        if name not in tiles:
            self._json({"error": f"unknown tile {name}"}, HTTPStatus.NOT_FOUND)
            return
        if self.state.source.writable:  # output folder: curator marks, used by package
            mark, changed = page.save_review(self.state.folder, name, flag=flag, note=note)
        else:  # release: notes next to the archives, never inside them (notes.py)
            mark, changed = notes.add(self.state.folder, tiles[name]["path"], flag=flag, note=note)
        if changed:
            self.state.rebuild()
            say(
                f"[review] {name}: {'flagged' if flag else 'not flagged'}"
                + (" + note" if note else "")
            )
        self._json({"saved": changed, "review": mark})


def serve(folder: Path, port: int, refresh: float, host: str = "0.0.0.0") -> None:  # noqa: S104 - see module docstring
    """Serve the live report of ``folder`` until Ctrl+C or Stop server in the page."""
    state = _State(folder, refresh)
    _Handler.state = state
    server = ThreadingHTTPServer((host, port), _Handler)
    worker = threading.Thread(target=state.loop, daemon=True)
    worker.start()
    say(
        f"[serve] {len(state.data['tiles'])} tiles from {folder}; open http://localhost:{port} "
        f"on this machine. Refresh every {refresh:g} s. Ctrl+C or 'Stop server' in the page to end."
    )
    started = time.monotonic()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop.set()
        server.server_close()
        say(f"[serve] stopped after {time.monotonic() - started:.0f} s")
