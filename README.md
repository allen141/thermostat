# T10 Flight Recorder

A local, read-only diagnostic dashboard for a Resideo/Honeywell Home T10
thermostat. It polls at the API-supported interval, stores every response in
SQLite, detects equipment-state transitions, and lets you timestamp physical
observations such as dimming lights or hearing the compressor try to start.

The local HomeKit collector is configured for event-only monitoring by default
(`HOMEKIT_POLL_SECONDS=0` in `compose.yaml`). It records one startup snapshot
and then stores only notifications sent by the thermostat. Set the variable to
a positive number of seconds to restore periodic HomeKit reads.

## What it can and cannot prove

`operationStatus.mode` is the thermostat's equipment relay/request status. It
can show that cooling was requested; it cannot prove that the outdoor
contactor, compressor, or either physical cooling stage actually energized.
The API's thermostat-configuration endpoint reports the installed number of
cooling stages, but the public API may not identify the active stage separately
on every model/firmware combination. Raw API payloads are retained so any
additional fields your T10 exposes are preserved.

Visible light dimming and repeated compressor start attempts can indicate a
high-current electrical or compressor-start problem. Do not open energized HVAC
equipment. If attempts repeat, the unit buzzes, wiring smells hot, or a breaker
trips, turn the cooling system off and contact a licensed HVAC technician (and
an electrician if the voltage supply is suspect).

## Setup

1. Create an application at the Resideo developer portal.
2. Set its callback URL to `http://10.0.0.117:8787/auth/callback`.
3. Copy `.env.example` to `.env` and enter the API key and secret.
4. Run:

```bash
python3 server.py
```

5. Open `http://127.0.0.1:8787`, select **Connect Resideo**, and authorize the
   thermostat account.

No third-party Python packages are required. The server binds to localhost by
default. OAuth tokens and history are stored in `data/thermostat.sqlite`;
protect and back up that file.

## Thermostat detail module

The dashboard detail view is rooted at `[data-component="thermostat-detail"]`. Its browser API is exposed as `window.ThermostatDetailModule` and accepts a normalized object with `device`, `snapshot`, `history.samples`, and `source`. Runtime samples require only `captured_at` and `operation_mode`; temperature, humidity, setpoint, location, and device-name fields are optional. This keeps the detail view independent of Resideo or HomeKit and allows a multi-thermostat overview to supply the selected thermostat as data.

Runtime can be viewed over 7, 30, 90, 180, or 365 days and grouped by day or month. Long sample gaps are excluded from totals and reflected in the coverage metric.

## Review previews

Every push to a non-`main` branch runs `.github/workflows/preview-pages.yml`. The workflow builds a fixture-backed static dashboard, stores it as the **thermostat-review-preview** Actions artifact for 14 days, and adds or updates the download link on the associated pull request. After download, unzip the artifact and open `index.html`.

Hosted GitHub Pages deployment is additionally available when the repository supports Pages and the Actions variable `ENABLE_PAGES_PREVIEW` is set to `true`. Private repositories require a GitHub plan with private Pages support. The preview never receives Resideo credentials, HomeKit pairing material, the SQLite database, or live thermostat data, and it does not replace or update the Docker-based live deployment.

## Production deployments

Updates to `main` are validated and published as Linux/AMD64 images at `ghcr.io/allen141/thermostat`. Production uses the locally built, pull-based updater documented in [`ops/deployer/README.md`](ops/deployer/README.md). The updater smoke-tests each image, retains one known-good rollback, preserves the bind-mounted `data/` directory, and does not store a GitHub credential on the server.

## Diagnostic workflow

- Leave the collector running continuously.
- Press **Mark observation** immediately when lights dim, you hear a start
  attempt, or you verify the outdoor unit is or is not running.
- Export CSV from the dashboard before a service visit.
- Compare `Cooling` transitions with temperature slope and observation markers.
  A cooling request followed by no temperature decrease is a useful clue, but
  is not by itself a diagnosis.

For higher-resolution electrical evidence, use a qualified technician's clamp
meter/data logger or a suitable energy monitor. Never connect improvised
instrumentation to compressor or mains wiring.

