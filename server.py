#!/usr/bin/env python3
"""Local Resideo T10 flight recorder. Standard-library only."""

from __future__ import annotations

import base64
import csv
import hashlib
import http.server
import io
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB_PATH = DATA / "thermostat.sqlite"
STATIC = ROOT / "static"
API_BASE = "https://api.honeywellhome.com"
HOMEKIT_MANAGER = None


def load_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_env()
CLIENT_ID = os.getenv("RESIDEO_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("RESIDEO_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv(
    "RESIDEO_REDIRECT_URI", "http://127.0.0.1:8787/auth/callback"
)
POLL_SECONDS = max(300, int(os.getenv("POLL_SECONDS", "300")))
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8787"))

DATA.mkdir(exist_ok=True)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv (
              key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS samples (
              id INTEGER PRIMARY KEY,
              captured_at TEXT NOT NULL,
              device_id TEXT NOT NULL,
              indoor_temp REAL,
              outdoor_temp REAL,
              indoor_humidity REAL,
              outdoor_humidity REAL,
              cool_setpoint REAL,
              heat_setpoint REAL,
              system_mode TEXT,
              operation_mode TEXT,
              fan_request INTEGER,
              circulation_fan_request INTEGER,
              is_alive INTEGER,
              raw_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS samples_time
              ON samples(captured_at);
            CREATE TABLE IF NOT EXISTS transitions (
              id INTEGER PRIMARY KEY,
              occurred_at TEXT NOT NULL,
              field TEXT NOT NULL,
              from_value TEXT,
              to_value TEXT,
              sample_id INTEGER REFERENCES samples(id)
            );
            CREATE TABLE IF NOT EXISTS observations (
              id INTEGER PRIMARY KEY,
              occurred_at TEXT NOT NULL,
              kind TEXT NOT NULL,
              note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS auxiliary (
              id INTEGER PRIMARY KEY,
              captured_at TEXT NOT NULL,
              kind TEXT NOT NULL,
              raw_json TEXT NOT NULL
            );
            """
        )


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def put_kv(key: str, value: object) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def get_kv(key: str, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def api_request(path: str, token: str, params=None):
    query = dict(params or {})
    query["apikey"] = CLIENT_ID
    url = API_BASE + path + "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def token_request(form: dict):
    auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    request = urllib.request.Request(
        API_BASE + "/oauth2/token",
        data=urllib.parse.urlencode(form).encode(),
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def valid_token() -> str:
    tokens = get_kv("tokens")
    if not tokens:
        raise RuntimeError("Resideo account is not connected")
    if time.time() < tokens.get("expires_at", 0) - 60:
        return tokens["access_token"]
    refreshed = token_request(
        {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
    )
    refreshed.setdefault("refresh_token", tokens["refresh_token"])
    refreshed["expires_at"] = time.time() + int(refreshed.get("expires_in", 600))
    put_kv("tokens", refreshed)
    return refreshed["access_token"]


def discover() -> dict:
    token = valid_token()
    locations = api_request("/v2/locations", token)
    thermostats = []
    for location in locations:
        location_id = location.get("locationID") or location.get("locationId")
        devices = api_request("/v2/devices", token, {"locationId": location_id})
        for device in devices:
            if device.get("deviceClass") == "Thermostat":
                thermostats.append(
                    {
                        "location_id": location_id,
                        "location_name": location.get("name", "Home"),
                        "device_id": device.get("deviceID"),
                        "device_name": device.get("userDefinedDeviceName")
                        or device.get("name", "Thermostat"),
                    }
                )
    if not thermostats:
        raise RuntimeError("No thermostat was found in the connected account")
    selected = thermostats[0]
    put_kv("devices", thermostats)
    put_kv("selected_device", selected)
    return selected


def store_aux(kind: str, payload: object) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO auxiliary(captured_at,kind,raw_json) VALUES(?,?,?)",
            (now(), kind, json.dumps(payload, separators=(",", ":"))),
        )


def poll_once() -> dict:
    selected = get_kv("selected_device") or discover()
    token = valid_token()
    params = {"locationId": selected["location_id"]}
    device_id = urllib.parse.quote(selected["device_id"], safe="")
    payload = api_request(f"/v2/devices/thermostats/{device_id}", token, params)
    cv = payload.get("changeableValues") or {}
    op = payload.get("operationStatus") or {}
    captured = now()
    values = (
        captured,
        selected["device_id"],
        payload.get("indoorTemperature"),
        payload.get("outdoorTemperature"),
        payload.get("indoorHumidity"),
        payload.get("displayedOutdoorHumidity"),
        cv.get("coolSetpoint"),
        cv.get("heatSetpoint"),
        cv.get("mode"),
        op.get("mode"),
        bool(op.get("fanRequest")),
        bool(op.get("circulationFanRequest")),
        bool(payload.get("isAlive")),
        json.dumps(payload, separators=(",", ":")),
    )
    with db() as conn:
        previous = conn.execute(
            "SELECT * FROM samples ORDER BY id DESC LIMIT 1"
        ).fetchone()
        cursor = conn.execute(
            """INSERT INTO samples(
              captured_at,device_id,indoor_temp,outdoor_temp,indoor_humidity,
              outdoor_humidity,cool_setpoint,heat_setpoint,system_mode,
              operation_mode,fan_request,circulation_fan_request,is_alive,raw_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        )
        sample_id = cursor.lastrowid
        if previous:
            for field in (
                "operation_mode",
                "fan_request",
                "circulation_fan_request",
                "system_mode",
                "cool_setpoint",
                "is_alive",
            ):
                old, new = previous[field], dict(
                    zip(
                        (
                            "captured_at", "device_id", "indoor_temp", "outdoor_temp",
                            "indoor_humidity", "outdoor_humidity", "cool_setpoint",
                            "heat_setpoint", "system_mode", "operation_mode",
                            "fan_request", "circulation_fan_request", "is_alive",
                            "raw_json",
                        ),
                        values,
                    )
                )[field]
                if old != new:
                    conn.execute(
                        """INSERT INTO transitions(
                          occurred_at,field,from_value,to_value,sample_id
                        ) VALUES(?,?,?,?,?)""",
                        (captured, field, str(old), str(new), sample_id),
                    )
    last_config = get_kv("last_config_poll", 0)
    if time.time() - last_config > 86400:
        try:
            config = api_request(
                f"/v2/devices/thermostats/{device_id}/thermostatconfiguration",
                token,
                params,
            )
            store_aux("configuration", config)
            put_kv("last_config_poll", time.time())
        except Exception as exc:
            put_kv("last_aux_error", f"{now()} configuration: {exc}")
    put_kv("last_poll", {"at": captured, "ok": True})
    return payload


def collector() -> None:
    while True:
        try:
            if get_kv("tokens"):
                poll_once()
        except Exception as exc:
            put_kv("last_poll", {"at": now(), "ok": False, "error": str(exc)})
        time.sleep(POLL_SECONDS)


def rows_as_dict(rows):
    return [dict(row) for row in rows]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"{self.log_date_time_string()} {fmt % args}")

    def send_json(self, value, status=200):
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, target):
        self.send_response(302)
        self.send_header("Location", target)
        self.end_headers()

    def body_json(self):
        length = min(int(self.headers.get("Content-Length", "0")), 65536)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/homekit/history":
                if not HOMEKIT_MANAGER:
                    return self.send_json({"error": "HomeKit unavailable"}, 503)
                hours = max(1, min(24 * 90, int(query.get("hours", ["24"])[0])))
                return self.send_json(HOMEKIT_MANAGER.history(hours))
            if path == "/api/homekit/status":
                if not HOMEKIT_MANAGER:
                    return self.send_json({"ready": False, "error": "HomeKit unavailable"}, 503)
                return self.send_json({**HOMEKIT_MANAGER.status(), "events": HOMEKIT_MANAGER.latest_events(30)})
            if path == "/api/homekit/discover":
                if not HOMEKIT_MANAGER:
                    return self.send_json({"error": "HomeKit unavailable"}, 503)
                return self.send_json({"devices": HOMEKIT_MANAGER.discover()})
            if path == "/auth/login":
                if not CLIENT_ID or not CLIENT_SECRET:
                    return self.send_json(
                        {"error": "Set RESIDEO_CLIENT_ID and RESIDEO_CLIENT_SECRET"}, 400
                    )
                state = secrets.token_urlsafe(24)
                put_kv("oauth_state_hash", hashlib.sha256(state.encode()).hexdigest())
                url = API_BASE + "/oauth2/authorize?" + urllib.parse.urlencode(
                    {
                        "response_type": "code",
                        "client_id": CLIENT_ID,
                        "redirect_uri": REDIRECT_URI,
                        "state": state,
                    }
                )
                return self.redirect(url)
            if path == "/auth/callback":
                state = query.get("state", [""])[0]
                expected = get_kv("oauth_state_hash", "")
                if not secrets.compare_digest(
                    hashlib.sha256(state.encode()).hexdigest(), expected
                ):
                    return self.send_json({"error": "Invalid OAuth state"}, 400)
                token = token_request(
                    {
                        "grant_type": "authorization_code",
                        "code": query.get("code", [""])[0],
                        "redirect_uri": REDIRECT_URI,
                    }
                )
                token["expires_at"] = time.time() + int(token.get("expires_in", 600))
                put_kv("tokens", token)
                discover()
                poll_once()
                return self.redirect("/")
            if path == "/api/status":
                with db() as conn:
                    latest = conn.execute(
                        "SELECT * FROM samples ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    config = conn.execute(
                        """SELECT raw_json FROM auxiliary
                           WHERE kind='configuration' ORDER BY id DESC LIMIT 1"""
                    ).fetchone()
                return self.send_json(
                    {
                        "connected": bool(get_kv("tokens")),
                        "device": get_kv("selected_device"),
                        "latest": dict(latest) if latest else None,
                        "configuration": json.loads(config["raw_json"]) if config else None,
                        "last_poll": get_kv("last_poll"),
                        "poll_seconds": POLL_SECONDS,
                    }
                )
            if path == "/api/history":
                hours = max(1, min(24 * 90, int(query.get("hours", ["24"])[0])))
                cutoff = datetime.fromtimestamp(
                    time.time() - hours * 3600, timezone.utc
                ).isoformat(timespec="seconds")
                with db() as conn:
                    samples = rows_as_dict(
                        conn.execute(
                            """SELECT id,captured_at,indoor_temp,outdoor_temp,
                               indoor_humidity,cool_setpoint,operation_mode,
                               fan_request,is_alive FROM samples
                               WHERE captured_at>=? ORDER BY id""",
                            (cutoff,),
                        )
                    )
                    transitions = rows_as_dict(
                        conn.execute(
                            "SELECT * FROM transitions WHERE occurred_at>=? ORDER BY id",
                            (cutoff,),
                        )
                    )
                    observations = rows_as_dict(
                        conn.execute(
                            "SELECT * FROM observations WHERE occurred_at>=? ORDER BY id",
                            (cutoff,),
                        )
                    )
                return self.send_json(
                    {
                        "samples": samples,
                        "transitions": transitions,
                        "observations": observations,
                    }
                )
            if path == "/api/export.csv":
                output = io.StringIO()
                with db() as conn:
                    rows = conn.execute("SELECT * FROM samples ORDER BY id")
                    writer = csv.writer(output)
                    writer.writerow([d[0] for d in rows.description])
                    writer.writerows(rows)
                body = output.getvalue().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header(
                    "Content-Disposition", 'attachment; filename="t10-history.csv"'
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if path == "/api/raw/latest":
                with db() as conn:
                    row = conn.execute(
                        "SELECT raw_json FROM samples ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                return self.send_json(json.loads(row["raw_json"]) if row else {})
            return self.static(path)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            self.send_json({"error": f"Resideo API {exc.code}", "detail": detail}, 502)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def do_POST(self):
        try:
            if self.path == "/api/homekit/pair":
                if not HOMEKIT_MANAGER:
                    return self.send_json({"error": "HomeKit unavailable"}, 503)
                body = self.body_json()
                result = HOMEKIT_MANAGER.pair(str(body.get("device_id", "")), str(body.get("code", "")))
                return self.send_json(result, 201)
            if self.path == "/api/poll":
                return self.send_json(poll_once())
            if self.path == "/api/observations":
                body = self.body_json()
                kind = str(body.get("kind", "note"))[:40]
                note = str(body.get("note", ""))[:500]
                occurred = str(body.get("occurred_at") or now())
                with db() as conn:
                    cursor = conn.execute(
                        """INSERT INTO observations(occurred_at,kind,note)
                           VALUES(?,?,?)""",
                        (occurred, kind, note),
                    )
                return self.send_json(
                    {"id": cursor.lastrowid, "occurred_at": occurred}, 201
                )
            self.send_json({"error": "Not found"}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def static(self, path):
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC / relative).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            return self.send_json({"error": "Not found"}, 404)
        mime = {
            ".html": "text/html",
            ".css": "text/css",
            ".js": "text/javascript",
            ".svg": "image/svg+xml",
        }.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    init_db()
    try:
        from homekit_manager import HomeKitManager
        HOMEKIT_MANAGER = HomeKitManager(DATA)
        HOMEKIT_MANAGER.start()
    except Exception as exc:
        print(f"HomeKit disabled: {exc}")
    threading.Thread(target=collector, daemon=True).start()
    server = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"T10 Flight Recorder listening on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")

