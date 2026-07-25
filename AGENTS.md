# Developer and operations guide

This file is the repository-level instruction set for developers and coding
agents. Keep changes small, reviewable, and safe for a dashboard that has a live
production deployment and persistent thermostat data.

## Repository purpose

This project is a read-only flight recorder and dashboard for the Resideo T10
and Copeland Sensi HVAC systems. It:

- serves a Python HTTP application from `server.py`;
- maintains stable `t10` and `sensi` unit identities with unit-aware telemetry,
  transitions, observations, exports, and comparison APIs;
- integrates HomeKit through `homekit_manager.py`;
- stores OAuth state, observations, and history in `data/thermostat.sqlite`,
  with HomeKit pairing state alongside it in `data/homekit-pairings.json`;
- serves the browser interface from `static/`;
- creates fixture-backed review previews with `preview/preview.js`; and
- publishes production application images through GitHub Actions.

The dashboard can show that the thermostat requested cooling, but it cannot
prove that physical HVAC equipment energized. Do not turn software observations
into electrical or HVAC safety claims.

## Repository map

- `server.py`: HTTP server, Resideo OAuth/API integration, legacy and
  normalized SQLite schemas, idempotent data migration, per-unit polling,
  history, observations, exports, comparison, and API routes.
- `homekit_manager.py`: HomeKit discovery, pairing, subscriptions, polling, and
  persistence.
- `static/`: production HTML, CSS, and browser JavaScript.
- `preview/preview.js`: fixture API used only by review previews.
- `Dockerfile`: non-root production application image and health check.
- `compose.yaml`: manual single-host application definition; the automated
  production updater does not use Compose.
- `.github/workflows/preview-pages.yml`: non-`main` review preview pipeline.
- `.github/workflows/container-release.yml`: PR validation and `main` image
  publication.
- `ops/deployer/`: fixed, locally built production updater, mock Docker harness,
  state-machine tests, and operator runbook.
- `data/`: runtime state. It is not source code and must never be committed.

## Development workflow

Start work from the latest `origin/main` on a dedicated branch. Use the
`codex/` prefix for Codex-created branches. Do not develop directly in the
production checkout.

Before editing:

```bash
git fetch origin main
git switch --create codex/<short-topic> origin/main
git status --short --branch
```

Preserve unrelated user changes in dirty worktrees. Never reset, overwrite, or
delete them to make a task easier. Keep commits scoped to one concern and write
commit messages that describe the outcome.

For UI work:

- keep the per-thermostat detail interface modular;
- pass normalized thermostat data into `window.ThermostatDetailModule`;
- keep the fixture-backed preview representative of new API fields and states;
- test narrow and mobile layouts when browser tooling is available; and
- never put production data, pairing material, or credentials in fixtures.

For backend or data work:

- preserve compatibility with existing SQLite files and legacy T10 endpoints;
- keep the stable `t10` and `sensi` identities and source attribution intact;
- use additive, idempotent migrations and tolerate older rows or missing
  optional fields;
- preserve raw upstream payloads when practical;
- keep HomeKit and Resideo-specific behavior out of generic UI contracts; and
- do not reduce the minimum Resideo polling interval below 300 seconds.

## Local configuration and secrets

Local development can load `.env` through `server.py`. Relevant settings are:

- `RESIDEO_CLIENT_ID`
- `RESIDEO_CLIENT_SECRET`
- `RESIDEO_REDIRECT_URI`
- `HOST`
- `PORT`
- `POLL_SECONDS`
- `HOMEKIT_POLL_SECONDS`
- `HOMEKIT_RECONNECT_SECONDS`

Never commit `.env`, OAuth tokens, `data/thermostat.sqlite`, SQLite WAL/SHM
files, `homekit-pairings.json`, API responses containing account data, or logs
containing credentials. Do not print secret values during diagnostics. Prefer
reporting whether a variable is present rather than its contents.

Set up an isolated Python environment outside the repository when HomeKit
development is required, then create the ignored local configuration:

```bash
python3 -m venv ../thermostat-venv
. ../thermostat-venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
python3 server.py
```

`server.py` can start without the optional HomeKit dependency, but HomeKit is
disabled in that case.

The default endpoint is `http://127.0.0.1:8787`. Use an alternate `PORT` when
the live application already owns 8787.

On an isolated development host, the containerized application can also be
built and started with:

```bash
docker compose up --build
```

`compose.yaml` uses the production container name and host port. Never run that
command on the live server or any host where `thermostat-monitor` already
exists.

## Required validation

Run checks proportional to the change. The baseline validation used by CI is:

```bash
python3 -m unittest discover -s tests -v
node --check static/app.js
node --check preview/preview.js
python3 -m py_compile server.py homekit_manager.py
sh -n ops/deployer/deploy.sh
sh -n ops/deployer/tests/mock-docker
sh -n ops/deployer/tests/test-deploy.sh
ops/deployer/tests/test-deploy.sh
docker build --tag thermostat-ci:local .
git diff --check
```

For application-image changes, start the built image without production data
and wait for Docker health to become `healthy`. Use an isolated bridge network
and a uniquely named temporary container. Always remove the temporary container
afterward.

For updater changes, extend the mock Docker harness and state-machine tests.
At minimum preserve coverage for:

- unchanged images causing no restart;
- smoke-test failure leaving production untouched;
- successful replacement retaining one rollback; and
- production-health failure restoring the previous container.

Do not test deployment logic against the live container unless the user has
explicitly authorized a production rollout.

## Review previews

Every push to a non-`main` branch runs
`.github/workflows/preview-pages.yml`. It builds the fixture-backed static site,
uploads a 14-day `thermostat-review-preview` artifact, and adds its link to the
associated pull request. If the repository variable
`ENABLE_PAGES_PREVIEW=true`, the same fixture site is deployed to GitHub Pages.

The preview is a separate review environment. It must never:

- receive Resideo credentials;
- receive HomeKit pairing files;
- mount or copy the production SQLite database; or
- replace, restart, or otherwise control the production container.

## Application release pipeline

`.github/workflows/container-release.yml` has three modes:

- pull request: validate, test, build, and smoke-test without publishing;
- push to `main`: validate, test, build, smoke-test, and publish; and
- manual dispatch: publish only when the selected ref is `main`.

Successful `main` runs publish Linux/AMD64 images to:

- `ghcr.io/allen141/thermostat:main`
- `ghcr.io/allen141/thermostat:sha-<full-commit-sha>`

The workflow uses `GITHUB_TOKEN` only inside GitHub Actions. The public GHCR
package is pulled anonymously by production. Images disable provenance and SBOM
attachments for compatibility with the server's Docker 20.10 daemon and carry
OCI source/revision labels.

Every update to `main`, including a direct push, is a production release.
Treat merging or pushing to `main` as authorization-sensitive: do it only when
the user asked for the merge or production rollout and the required checks pass.

## Live production architecture

The live host is an Unraid Linux/AMD64 server using Docker 20.10. Important
paths and resources are:

- production checkout:
  `/mnt/user/PrivateStorage/projects/thermostat`
- persistent application data:
  `/mnt/user/PrivateStorage/projects/thermostat/data`
- updater state:
  `/mnt/user/appdata/thermostat-deployer`
- application endpoint:
  `http://127.0.0.1:8787`
- production container:
  `thermostat-monitor`
- smoke container:
  `thermostat-monitor-smoke`
- retained rollback container:
  `thermostat-monitor-rollback`
- updater container:
  `thermostat-deployer`
- locally built updater image:
  `thermostat-deployer:local`

The production application runs:

- pinned to the pulled application image ID;
- with host networking;
- with `/mnt/user/PrivateStorage/projects/thermostat/data:/app/data`;
- with `HOST=0.0.0.0`, `PORT=8787`, `POLL_SECONDS=300`,
  `HOMEKIT_POLL_SECONDS=0`, and `HOMEKIT_RECONNECT_SECONDS=60`;
- with restart policy `unless-stopped`;
- with `no-new-privileges` and all Linux capabilities dropped; and
- with one 50 MB JSON log file.

The updater polls `ghcr.io/allen141/thermostat:main` every 60 seconds. It first
runs the candidate without production data on isolated bridge networking. Only
after that container becomes healthy does it stop and preserve production,
start the candidate with production configuration, and wait for production
health. A failed candidate is recorded and is not retried until `main` points to
a different image.

Exactly one known-good rollback container is retained. Successful swaps usually
cause a short dashboard interruption. Persistent database, OAuth, and HomeKit
pairing state survive because they remain in the host data directory rather
than the container filesystem. Back up and restore `thermostat.sqlite` and
`homekit-pairings.json` together so unit mappings and pairing credentials remain
consistent.

## Production safety rules

Routine development, review, and diagnosis do not authorize production
mutation. Without explicit authorization, do not:

- merge or push to `main`;
- stop, rename, remove, restart, or recreate live containers;
- rebuild or replace `thermostat-deployer`;
- edit the production checkout;
- modify files under either persistent state directory;
- change GHCR package visibility; or
- perform a server reboot.

When production work is authorized:

- confirm the exact container names and clean checkout before changing state;
- take a read-only baseline of health, API response, database row counts, and
  pairing-file existence or hash;
- never display pairing contents, OAuth tokens, or credentials;
- use only the explicit thermostat container/image names;
- never run broad cleanup such as `docker image prune`;
- verify rollback availability before replacing production; and
- verify health, API response, data continuity, and updater state afterward.

The Docker socket mounted into the updater is equivalent to host-level control.
Do not add network-fetched scripts, dynamic code execution, arbitrary container
names, or general cleanup behavior to the updater.

## Managing the production updater

The updater source is version-controlled in `ops/deployer/`, but the running
updater is deliberately fixed. Publishing a new application image does not
update the updater. This prevents an application commit from replacing the
host-level deployment control plane.

Inspect it without changing production:

```bash
docker logs --tail 200 thermostat-deployer
docker inspect thermostat-deployer --format '{{.State.Status}} {{.Config.Image}}'
docker inspect thermostat-monitor --format '{{json .State.Health}}'
curl -fsS http://127.0.0.1:8787/api/status
```

Inspect persistent updater state:

```bash
sed -n '1p' /mnt/user/appdata/thermostat-deployer/current-image-id
sed -n '1p' /mnt/user/appdata/thermostat-deployer/current-image-ref
test ! -f /mnt/user/appdata/thermostat-deployer/failed-image-id ||
  sed -n '1p' /mnt/user/appdata/thermostat-deployer/failed-image-id
tail -n 200 /mnt/user/appdata/thermostat-deployer/deploy.log
```

Pause and resume automatic application updates:

```bash
docker stop thermostat-deployer
docker start thermostat-deployer
```

Pausing the updater does not stop production. Before resuming, inspect the
current `main` digest and any recorded failed image. Resuming triggers an
immediate check.

After an updater code change has been reviewed and merged, deliberately rebuild
it from the clean production checkout:

```bash
git -C /mnt/user/PrivateStorage/projects/thermostat fetch origin main
git -C /mnt/user/PrivateStorage/projects/thermostat merge --ff-only origin/main
docker stop thermostat-deployer
docker rm thermostat-deployer
docker build \
  --tag thermostat-deployer:local \
  /mnt/user/PrivateStorage/projects/thermostat/ops/deployer
docker run --detach \
  --name thermostat-deployer \
  --restart unless-stopped \
  --env DOCKER_API_VERSION=1.41 \
  --env POLL_SECONDS=60 \
  --volume /var/run/docker.sock:/var/run/docker.sock \
  --volume /mnt/user/appdata/thermostat-deployer:/state \
  thermostat-deployer:local
```

The state mount must be preserved. Confirm the updater is running, its image was
built from the intended commit, and an unchanged application digest does not
restart `thermostat-monitor`.

## Rollback and recovery

Automatic rollback occurs when a candidate passes isolated smoke testing but
does not become healthy with production configuration. The updater removes the
failed candidate, restores `thermostat-monitor-rollback` as
`thermostat-monitor`, verifies health, and records the candidate as failed.

For an explicitly authorized manual rollback:

```bash
docker stop thermostat-deployer
docker rm --force thermostat-monitor
docker rename thermostat-monitor-rollback thermostat-monitor
docker start thermostat-monitor
docker inspect thermostat-monitor --format '{{json .State.Health}}'
curl -fsS http://127.0.0.1:8787/api/status
```

Keep the updater stopped until `main` points to a corrected image. A manual
rollback is destructive to the failed container, so verify all names and
rollback availability before running it.

## Documentation maintenance

When development commands, workflows, server paths, container names, image
tags, environment variables, or updater behavior change, update this file and
the relevant human-facing documentation in the same pull request. The detailed
operator runbook remains `ops/deployer/README.md`; keep the two documents
consistent.
