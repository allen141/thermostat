# Multi-Thermostat Flight Recorder

A local, read-only diagnostic dashboard for two HVAC systems:

- Resideo/Honeywell Home T10, collected through local HomeKit with the Resideo API as a fallback.
- Copeland Sensi 1F95U-42WF, collected through local HomeKit.

Telemetry is retained in SQLite. The dashboard provides independent **T10** and **Sensi** tabs plus a **Compare** tab that overlays both systems on one time axis. It records temperatures, humidity, setpoints, thermostat equipment requests, HomeKit events, and unit-specific or house-wide physical observations.

## Setup

1. Copy `.env.example` to `.env` and enter Resideo credentials if the T10 cloud fallback is required.
2. Run `docker compose up -d --build` or `python3 server.py` after installing `requirements.txt`.
3. Open `http://127.0.0.1:8787`.
4. For each local thermostat, put the accessory into HomeKit pairing mode, select **Add / link thermostat**, choose the dashboard unit, and enter its eight-digit code.

For the Sensi 1F95U-42WF, the HomeKit code is available from the thermostat's Wi-Fi/HomeKit setup screen. If discovery says the accessory is already paired, the recorder deliberately does not reset or unpair it; enable pairing from its existing controller or remove it there first.

The server uses host networking because HomeKit discovery relies on local multicast DNS. Pairing credentials remain in `data/homekit-pairings.json`, and telemetry remains in `data/thermostat.sqlite`; protect and back up both files.
Pairing credentials and unit mappings survive service restarts and image replacements because the production container bind-mounts the same `data/` directory. On startup, each accessory reconnects independently. A non-recording health check runs every `HOMEKIT_RECONNECT_SECONDS` in event-only mode; failures are shown as **Reconnecting** and retried with bounded exponential backoff without requiring another service restart. Restore `homekit-pairings.json` and `thermostat.sqlite` together when recovering from backup.


## Data migration

Startup creates stable `t10` and `sensi` unit identities and unit-aware telemetry tables. Existing Resideo samples, local T10 HomeKit samples, transitions, and observations are copied into the normalized model using idempotent source identifiers. Original tables are retained. Existing observations are assigned to the T10; new observations can be assigned to either unit or marked House-wide.

Back up `data/thermostat.sqlite` and `data/homekit-pairings.json` before deploying a new build.

## Dashboard and API

- Unit tabs show the latest state, active data source, graph, event timeline, raw sample, per-unit polling, observations, and CSV export.
- Compare shows measured temperature and setpoint for both units by default. Humidity, equipment calls, and events can be enabled from the legend.
- Chart range, zoom, visibility settings, and selected tab persist while navigating and across browser refreshes.
- `GET /api/units` lists units, sources, current readings, and health.
- `GET /api/units/{unit_id}/history?hours=24` returns one unit's history.
- `GET /api/compare?hours=24` returns both labelled histories.
- `POST /api/units/{unit_id}/poll` requests an immediate read.
- `GET /api/export.csv?unit_id=t10|sensi|all` exports attributed telemetry.

Legacy T10 endpoints remain available as compatibility wrappers.

## Diagnostic limits

A thermostat's operating state shows an equipment or relay request; it cannot prove that the outdoor contactor, compressor, blower, or a specific physical stage energized. A cooling request followed by no temperature decrease is useful evidence, not a diagnosis.

Do not open energized HVAC equipment. If start attempts repeat, the unit buzzes, wiring smells hot, or a breaker trips, turn the system off and contact a licensed HVAC technician. Use a qualified technician's clamp meter or a suitable energy monitor for higher-resolution electrical evidence.

## Reusable thermostat detail module

The selected unit view is rooted at `[data-component="thermostat-detail"]`. Its browser API remains available as `window.ThermostatDetailModule`, accepting normalized `device`, `snapshot`, `history.samples`, and `source` data. `window.aggregateThermostatRuntime` provides the coverage-aware daily/monthly runtime calculation. T10 and Sensi tabs feed this same module; Compare uses the shared overlay chart.

## Review previews and production deployment

Every push to a non-`main` branch runs `.github/workflows/preview-pages.yml`, producing a fixture-backed static review artifact without credentials, pairing data, or live telemetry. Hosted Pages previews remain opt-in through `ENABLE_PAGES_PREVIEW`.

Updates merged to `main` are validated and published as Linux/AMD64 images by `.github/workflows/container-release.yml`. Production uses the pull-based updater documented in [`ops/deployer/README.md`](ops/deployer/README.md), preserving the bind-mounted `data/` directory and retaining rollback state.

## Developer guide

Development conventions, required validation, release behavior, live-server architecture, and updater-management safety rules are documented in [`AGENTS.md`](AGENTS.md).

## Tests

Run:

```bash
python3 -m unittest discover -s tests -v
node --check static/app.js
```
