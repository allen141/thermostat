"""Local HomeKit discovery, pairing, and event recorder."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from aiohomekit import Controller
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf
from aiohomekit.zeroconf import HAP_TYPE_TCP, HAP_TYPE_UDP


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class HomeKitManager:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.pairings_file = data_dir / "homekit-pairings.json"
        self.db_path = data_dir / "thermostat.sqlite"
        self.loop = None
        self.controller = None
        self.discoveries = {}
        self.characteristics = {}
        self.last_error = None
        self.pairings = {}
        self.last_poll_at = None
        self.last_poll_error = None
        self.poll_seconds = max(0, int(os.environ.get("HOMEKIT_POLL_SECONDS", "30")))
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._thread_main, daemon=True)

    def start(self):
        self.thread.start()
        self.ready.wait(15)

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self):
        try:
            self.zeroconf = AsyncZeroconf()
            self.zeroconf_browser = AsyncServiceBrowser(
                self.zeroconf.zeroconf, [HAP_TYPE_TCP, HAP_TYPE_UDP],
                handlers=[lambda **kwargs: None]
            )
            self.controller = Controller(async_zeroconf_instance=self.zeroconf)
            await self.controller.async_start()
            self.controller.load_data(str(self.pairings_file))
            for alias, pairing in list(self.controller.aliases.items()):
                await self._setup_pairing(alias, pairing)
        except Exception as exc:
            self.last_error = str(exc)
        finally:
            if self.poll_seconds:
                self.loop.create_task(self._heartbeat())
            self.ready.set()
        await asyncio.Event().wait()

    def run(self, coroutine, timeout=30):
        if not self.loop or not self.controller:
            raise RuntimeError("HomeKit controller is not ready")
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    def status(self):
        aliases = list(self.controller.aliases) if self.controller else []
        return {
            "ready": self.ready.is_set(),
            "paired": bool(aliases),
            "aliases": aliases,
            "characteristic_count": len(self.characteristics),
            "current": [dict(value) for value in self.characteristics.values()],
            "last_error": self.last_error,
            "last_poll_at": self.last_poll_at,
            "last_poll_error": self.last_poll_error,
            "collection_mode": "events_and_polling" if self.poll_seconds else "events_only",
            "poll_seconds": self.poll_seconds,
        }

    async def _discover(self):
        found = []
        self.discoveries = {}
        async for discovery in self.controller.async_discover(timeout=8):
            description = discovery.description
            device_id = str(getattr(description, "id", ""))
            self.discoveries[device_id] = discovery
            found.append({
                "id": device_id,
                "name": str(getattr(description, "name", "HomeKit accessory")),
                "model": str(getattr(description, "model", "")),
                "paired": discovery.paired,
                "category": str(getattr(description, "category", "")),
            })
        return found

    def discover(self):
        return self.run(self._discover(), timeout=20)

    async def _pair(self, device_id, code):
        discovery = self.discoveries.get(device_id)
        if not discovery:
            await self._discover()
            discovery = self.discoveries.get(device_id)
        if not discovery:
            raise RuntimeError("Accessory is no longer discoverable; reopen Connect HomeKit")
        if discovery.paired:
            raise RuntimeError("Accessory reports that it is already paired")
        alias = "resideo-t10"
        finish_pairing = await discovery.async_start_pairing(alias)
        pairing = await finish_pairing(code)
        self.controller.aliases[alias] = pairing
        self.controller.pairings[pairing.id] = pairing
        self.controller.save_data(str(self.pairings_file))
        await self._setup_pairing(alias, pairing)
        return self.status()

    def pair(self, device_id, code):
        digits = re.sub(r"\D", "", code)
        if not re.fullmatch(r"\d{8}", digits):
            raise ValueError("Enter the eight digits shown on the thermostat")
        code = f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
        return self.run(self._pair(device_id, code), timeout=90)

    async def _setup_pairing(self, alias, pairing):
        self.pairings[alias] = pairing
        accessories = await pairing.list_accessories_and_characteristics()
        event_chars = set()
        self.characteristics = {}
        for accessory in accessories:
            aid = accessory["aid"]
            for service in accessory.get("services", []):
                for char in service.get("characteristics", []):
                    key = (aid, char["iid"])
                    meta = {
                        "aid": aid, "iid": char["iid"], "type": char.get("type"),
                        "description": char.get("description") or char.get("type"),
                        "service_type": service.get("type"),
                        "value": char.get("value"), "perms": char.get("perms", []),
                    }
                    self.characteristics[key] = meta
                    self._store_event(alias, meta, "snapshot")
                    if "ev" in char.get("perms", []):
                        event_chars.add(key)
        self._store_sample("snapshot")
        pairing.dispatcher_connect(lambda changes: self._handle_changes(alias, changes))
        if event_chars:
            await pairing.subscribe(event_chars)
        self.last_error = None

    def _handle_changes(self, alias, changes):
        for key, change in changes.items():
            meta = dict(self.characteristics.get(key, {
                "aid": key[0], "iid": key[1], "description": "Unknown characteristic"
            }))
            if isinstance(change, dict):
                meta.update(change)
                if "value" in change:
                    self.characteristics.setdefault(key, {}).update(value=change["value"])
            else:
                meta["value"] = change
            self._store_event(alias, meta, "event")

        self._store_sample("event")

    def _store_event(self, alias, payload, source):
        safe = json.loads(json.dumps(payload, default=str))
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS homekit_events (
                id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL, alias TEXT NOT NULL,
                aid INTEGER, iid INTEGER, characteristic TEXT, value_json TEXT,
                source TEXT NOT NULL, raw_json TEXT NOT NULL)""")
            conn.execute("""INSERT INTO homekit_events(
                occurred_at,alias,aid,iid,characteristic,value_json,source,raw_json)
                VALUES(?,?,?,?,?,?,?,?)""", (
                utcnow(), alias, safe.get("aid"), safe.get("iid"),
                safe.get("description") or safe.get("type"),
                json.dumps(safe.get("value")), source,
                json.dumps(safe, separators=(",", ":")),
            ))

    async def _poll_pairing(self, alias, pairing):
        read_keys = {
            key for key, meta in self.characteristics.items()
            if str(meta.get("service_type", "")).startswith("0000004A")
            and "pr" in meta.get("perms", [])
        }
        if not read_keys:
            raise RuntimeError("No readable thermostat characteristics found")
        readings = await pairing.get_characteristics(read_keys)
        errors = []
        updated = 0
        for key, result in readings.items():
            status = result.get("status", 0)
            if status:
                errors.append(f"{key[0]}.{key[1]} status {status}")
                continue
            if "value" in result and key in self.characteristics:
                self.characteristics[key]["value"] = result["value"]
                updated += 1
        if not updated:
            raise RuntimeError("Thermostat read returned no values" + (f": {', '.join(errors)}" if errors else ""))
        self._store_sample("poll")
        self.last_poll_at = utcnow()
        self.last_poll_error = "; ".join(errors) if errors else None

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(self.poll_seconds)
            for alias, pairing in list(self.pairings.items()):
                try:
                    await self._poll_pairing(alias, pairing)
                except Exception as exc:
                    self.last_poll_error = str(exc)

    def _thermostat_values(self):
        chars = [v for v in self.characteristics.values() if str(v.get("service_type", "")).startswith("0000004A")]
        def value(prefix):
            item = next((x for x in chars if str(x.get("type", "")).startswith(prefix)), None)
            return item.get("value") if item else None
        return {
            "current_temp_c": value("00000011"),
            "humidity": value("00000010"),
            "target_temp_c": value("00000035"),
            "cooling_threshold_c": value("0000000D"),
            "heating_threshold_c": value("00000012"),
            "current_hvac": value("0000000F"),
            "target_hvac": value("00000033"),
            "display_units": value("00000036"),
        }

    def _store_sample(self, source):
        values = self._thermostat_values()
        if values["current_temp_c"] is None:
            return
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS homekit_samples (
                id INTEGER PRIMARY KEY, captured_at TEXT NOT NULL, source TEXT NOT NULL,
                current_temp_c REAL, humidity REAL, target_temp_c REAL,
                cooling_threshold_c REAL, heating_threshold_c REAL,
                current_hvac INTEGER, target_hvac INTEGER, display_units INTEGER)""")
            conn.execute("""INSERT INTO homekit_samples(
                captured_at,source,current_temp_c,humidity,target_temp_c,
                cooling_threshold_c,heating_threshold_c,current_hvac,target_hvac,display_units)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (utcnow(), source,
                values["current_temp_c"], values["humidity"], values["target_temp_c"],
                values["cooling_threshold_c"], values["heating_threshold_c"],
                values["current_hvac"], values["target_hvac"], values["display_units"]))

    def history(self, hours=24):
        cutoff = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - hours * 3600, timezone.utc).isoformat()
        try:
            with sqlite3.connect(self.db_path, timeout=30) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT * FROM homekit_samples WHERE captured_at>=? ORDER BY id", (cutoff,)).fetchall()
                try:
                    event_rows = conn.execute("""SELECT occurred_at, characteristic, value_json, source
                        FROM homekit_events
                        WHERE occurred_at>=? AND source='event'
                        ORDER BY id""", (cutoff,)).fetchall()
                except sqlite3.OperationalError:
                    event_rows = []
            samples = []
            for row in rows:
                r = dict(row)
                convert = (lambda v: None if v is None else v * 9 / 5 + 32) if r["display_units"] == 1 else (lambda v: v)
                target = r["cooling_threshold_c"] if r["target_hvac"] == 3 else r["target_temp_c"]
                samples.append({
                    "captured_at": r["captured_at"], "indoor_temp": convert(r["current_temp_c"]),
                    "indoor_humidity": r["humidity"], "cool_setpoint": convert(target),
                    "operation_mode": ["EquipmentOff", "Heating", "Cooling"][r["current_hvac"]] if r["current_hvac"] in (0,1,2) else "Unknown",
                    "fan_request": r["current_hvac"] in (1,2), "fan_source": "inferred_from_hvac", "is_alive": True,
                })
            observations = [dict(row) for row in event_rows]
            return {"samples": samples, "transitions": [], "observations": observations}
        except sqlite3.OperationalError:
            return {"samples": [], "transitions": [], "observations": []}

    def latest_events(self, limit=100):
        try:
            with sqlite3.connect(self.db_path, timeout=30) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("""SELECT * FROM homekit_events
                    ORDER BY id DESC LIMIT ?""", (min(limit, 500),)).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.OperationalError:
            return []
