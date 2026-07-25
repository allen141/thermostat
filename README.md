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

