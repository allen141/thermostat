# Production deployer

The production deployer is a locally built, fixed control container. It polls the
public `ghcr.io/allen141/thermostat:main` image every 60 seconds and manages
only these application containers:

- `thermostat-monitor`
- `thermostat-monitor-smoke`
- `thermostat-monitor-rollback`

The source is version-controlled here, but the running deployer does not update
itself. Updating the deployer requires an intentional local rebuild and restart.

## Architecture

Every push to `main` runs the container release workflow. The workflow validates
the source, exercises this deployer's state-machine tests, smoke-tests the
application image, and publishes these Linux/AMD64 tags:

- `ghcr.io/allen141/thermostat:main`
- `ghcr.io/allen141/thermostat:sha-<commit-sha>`

The server pulls the public image anonymously. No GitHub credential is stored on
the server. Deployment status is recorded locally rather than written back to
GitHub.

The deployer mounts the Docker socket read-write. Access to that socket is
equivalent to host-level control, so the deployer image is built locally from a
reviewed commit and is never automatically replaced.

## Manual release workflow

In GitHub, open **Actions**, select **Validate and publish container**, choose
**Run workflow**, and select a branch. A manual run against `main` validates,
builds, smoke-tests, and publishes both production tags. A run against any other
branch performs validation and the local image build only; it does not publish.

## One-time installation

Do this only after the release workflow has published the first `main` image
and an anonymous pull succeeds.

1. Update the clean production checkout:

   ```bash
   git -C /mnt/user/PrivateStorage/projects/thermostat fetch origin main
   git -C /mnt/user/PrivateStorage/projects/thermostat merge --ff-only origin/main
   ```

2. Build the fixed updater:

   ```bash
   docker build \
     --tag thermostat-deployer:local \
     /mnt/user/PrivateStorage/projects/thermostat/ops/deployer
   ```

3. Create persistent state with restricted permissions:

   ```bash
   mkdir -p /mnt/user/appdata/thermostat-deployer
   chmod 700 /mnt/user/appdata/thermostat-deployer
   ```

4. Start the updater:

   ```bash
   docker run --detach \
     --name thermostat-deployer \
     --restart unless-stopped \
     --env DOCKER_API_VERSION=1.41 \
     --env POLL_SECONDS=60 \
     --volume /var/run/docker.sock:/var/run/docker.sock \
     --volume /mnt/user/appdata/thermostat-deployer:/state \
     thermostat-deployer:local
   ```

The first successful update migrates the existing
`thermostat-monitor:homekit` container to the GHCR image. Its data remains at
`/mnt/user/PrivateStorage/projects/thermostat/data`. A successful swap normally
interrupts the dashboard for 5–20 seconds.

## Monitoring

Inspect the updater and application:

```bash
docker logs --tail 200 thermostat-deployer
docker inspect thermostat-monitor --format '{{json .State.Health}}'
curl -fsS http://127.0.0.1:8787/api/status
```

Persistent deployment state is stored in:

- `/mnt/user/appdata/thermostat-deployer/current-image-id`
- `/mnt/user/appdata/thermostat-deployer/current-image-ref`
- `/mnt/user/appdata/thermostat-deployer/failed-image-id`
- `/mnt/user/appdata/thermostat-deployer/deploy.log`

A failed digest is not retried until the `main` tag points to a different
image. Restarting the updater preserves this state.

## Pause and resume

Pause automatic deployments without affecting production:

```bash
docker stop thermostat-deployer
```

Resume polling:

```bash
docker start thermostat-deployer
```

## Manual rollback

The updater keeps exactly one stopped known-good container. To restore it
manually:

```bash
docker stop thermostat-deployer
docker rm --force thermostat-monitor
docker rename thermostat-monitor-rollback thermostat-monitor
docker start thermostat-monitor
docker inspect thermostat-monitor --format '{{json .State.Health}}'
```

Leave the updater stopped until the bad `main` image has been replaced by a new
commit.

## Updating the deployer itself

Updater changes do not activate when application images are published. After an
updater change is reviewed and merged:

```bash
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

The persistent state mount prevents a deployer rebuild from causing an
unnecessary application restart.
