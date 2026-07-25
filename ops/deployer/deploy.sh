#!/bin/sh

set -u

TARGET_IMAGE=${TARGET_IMAGE:-ghcr.io/allen141/thermostat:main}
DATA_DIR=${DATA_DIR:-/mnt/user/PrivateStorage/projects/thermostat/data}
STATE_DIR=${STATE_DIR:-/state}
POLL_SECONDS=${POLL_SECONDS:-60}
HEALTH_ATTEMPTS=${HEALTH_ATTEMPTS:-45}
HEALTH_POLL_SECONDS=${HEALTH_POLL_SECONDS:-2}
DOCKER_BIN=${DOCKER_BIN:-docker}

PRODUCTION_CONTAINER=thermostat-monitor
SMOKE_CONTAINER=thermostat-monitor-smoke
ROLLBACK_CONTAINER=thermostat-monitor-rollback

case "$TARGET_IMAGE" in
  ghcr.io/allen141/thermostat:main) ;;
  *)
    echo "Refusing unmanaged target image: $TARGET_IMAGE" >&2
    exit 2
    ;;
esac

mkdir -p "$STATE_DIR"
LOG_FILE="$STATE_DIR/deploy.log"
LOCK_DIR="$STATE_DIR/deploy.lock"
CURRENT_FILE="$STATE_DIR/current-image-id"
CURRENT_REF_FILE="$STATE_DIR/current-image-ref"
FAILED_FILE="$STATE_DIR/failed-image-id"

log() {
  timestamp=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  printf '%s %s\n' "$timestamp" "$*" | tee -a "$LOG_FILE"
}

docker_cmd() {
  "$DOCKER_BIN" "$@"
}

container_exists() {
  docker_cmd inspect "$1" >/dev/null 2>&1
}

container_image() {
  docker_cmd inspect --format '{{.Image}}' "$1"
}

container_health() {
  docker_cmd inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1" 2>/dev/null
}

wait_for_health() {
  container_name=$1
  attempt=1
  while [ "$attempt" -le "$HEALTH_ATTEMPTS" ]; do
    status=$(container_health "$container_name" || printf 'missing')
    case "$status" in
      healthy)
        return 0
        ;;
      unhealthy|exited|dead|missing)
        log "$container_name entered terminal state: $status"
        return 1
        ;;
    esac
    sleep "$HEALTH_POLL_SECONDS"
    attempt=$((attempt + 1))
  done
  log "$container_name did not become healthy after $HEALTH_ATTEMPTS checks"
  return 1
}

mark_failed() {
  image_id=$1
  printf '%s\n' "$image_id" > "$FAILED_FILE"
  log "Marked image as failed: $image_id"
}

record_success() {
  image_id=$1
  image_ref=$2
  printf '%s\n' "$image_id" > "$CURRENT_FILE"
  printf '%s\n' "$image_ref" > "$CURRENT_REF_FILE"
  rm -f "$FAILED_FILE"
  log "Deployment succeeded: $image_ref ($image_id)"
}

restore_rollback() {
  if ! container_exists "$ROLLBACK_CONTAINER"; then
    log "CRITICAL: no rollback container is available"
    return 1
  fi
  if ! docker_cmd rename "$ROLLBACK_CONTAINER" "$PRODUCTION_CONTAINER"; then
    log "CRITICAL: could not rename rollback container"
    return 1
  fi
  if ! docker_cmd start "$PRODUCTION_CONTAINER" >/dev/null; then
    log "CRITICAL: could not restart rollback container"
    return 1
  fi
  if ! wait_for_health "$PRODUCTION_CONTAINER"; then
    log "CRITICAL: rollback container did not become healthy"
    return 1
  fi
  log "Rollback restored successfully"
  return 0
}

start_production() {
  image_id=$1
  docker_cmd run --detach \
    --name "$PRODUCTION_CONTAINER" \
    --network host \
    --restart unless-stopped \
    --security-opt no-new-privileges:true \
    --cap-drop ALL \
    --log-opt max-size=50m \
    --log-opt max-file=1 \
    --env HOST=0.0.0.0 \
    --env PORT=8787 \
    --env POLL_SECONDS=300 \
    --env HOMEKIT_POLL_SECONDS=0 \
    --env HOMEKIT_RECONNECT_SECONDS=60 \
    --volume "$DATA_DIR:/app/data" \
    "$image_id" >/dev/null
}

deploy_once() {
  log "Checking $TARGET_IMAGE"

  if ! docker_cmd pull "$TARGET_IMAGE"; then
    log "Image pull failed; production was not changed"
    return 0
  fi

  new_image_id=$(docker_cmd image inspect --format '{{.Id}}' "$TARGET_IMAGE") || {
    log "Could not inspect the pulled image"
    return 0
  }
  new_image_ref=$(docker_cmd image inspect --format '{{index .RepoDigests 0}}' "$TARGET_IMAGE" 2>/dev/null || printf '%s' "$TARGET_IMAGE")

  if container_exists "$PRODUCTION_CONTAINER"; then
    current_image_id=$(container_image "$PRODUCTION_CONTAINER")
    if [ "$current_image_id" = "$new_image_id" ]; then
      record_success "$new_image_id" "$new_image_ref"
      log "Production already uses the current image"
      return 0
    fi
  else
    current_image_id=
  fi

  if [ -f "$FAILED_FILE" ] && [ "$(cat "$FAILED_FILE")" = "$new_image_id" ]; then
    log "Skipping image previously marked failed: $new_image_id"
    return 0
  fi

  if container_exists "$SMOKE_CONTAINER"; then
    docker_cmd rm --force "$SMOKE_CONTAINER" >/dev/null || {
      log "Could not remove stale smoke container"
      return 0
    }
  fi

  log "Starting smoke test for $new_image_ref"
  if ! docker_cmd run --detach \
    --name "$SMOKE_CONTAINER" \
    --network bridge \
    --restart no \
    --env HOMEKIT_POLL_SECONDS=0 \
    --env HOMEKIT_RECONNECT_SECONDS=60 \
    "$new_image_id" >/dev/null; then
    log "Could not start smoke container"
    mark_failed "$new_image_id"
    return 0
  fi

  if ! wait_for_health "$SMOKE_CONTAINER"; then
    docker_cmd logs --tail 100 "$SMOKE_CONTAINER" 2>&1 | tee -a "$LOG_FILE" || true
    docker_cmd rm --force "$SMOKE_CONTAINER" >/dev/null 2>&1 || true
    mark_failed "$new_image_id"
    log "Smoke test failed; production was not changed"
    return 0
  fi
  docker_cmd rm --force "$SMOKE_CONTAINER" >/dev/null || {
    log "Could not remove successful smoke container"
    return 0
  }

  old_rollback_image=
  if container_exists "$ROLLBACK_CONTAINER"; then
    old_rollback_image=$(container_image "$ROLLBACK_CONTAINER")
    if ! docker_cmd rm --force "$ROLLBACK_CONTAINER" >/dev/null; then
      log "Could not remove the superseded rollback container"
      return 0
    fi
  fi

  had_current=false
  if container_exists "$PRODUCTION_CONTAINER"; then
    had_current=true
    log "Stopping current production container"
    if ! docker_cmd stop --time 20 "$PRODUCTION_CONTAINER" >/dev/null; then
      log "Could not stop production; deployment aborted"
      return 0
    fi
    if ! docker_cmd rename "$PRODUCTION_CONTAINER" "$ROLLBACK_CONTAINER"; then
      log "Could not preserve production as rollback"
      docker_cmd start "$PRODUCTION_CONTAINER" >/dev/null 2>&1 || true
      return 0
    fi
  fi

  log "Starting production image $new_image_ref"
  if ! start_production "$new_image_id"; then
    log "Could not start the new production container"
    docker_cmd rm --force "$PRODUCTION_CONTAINER" >/dev/null 2>&1 || true
    mark_failed "$new_image_id"
    if [ "$had_current" = true ]; then
      restore_rollback || true
    fi
    return 0
  fi

  if ! wait_for_health "$PRODUCTION_CONTAINER"; then
    docker_cmd logs --tail 100 "$PRODUCTION_CONTAINER" 2>&1 | tee -a "$LOG_FILE" || true
    docker_cmd rm --force "$PRODUCTION_CONTAINER" >/dev/null 2>&1 || true
    mark_failed "$new_image_id"
    if [ "$had_current" = true ]; then
      restore_rollback || true
    fi
    return 0
  fi

  record_success "$new_image_id" "$new_image_ref"

  if [ -n "$old_rollback_image" ] &&
     [ "$old_rollback_image" != "$new_image_id" ] &&
     [ "$old_rollback_image" != "$current_image_id" ]; then
    docker_cmd image rm "$old_rollback_image" >/dev/null 2>&1 ||
      log "Superseded rollback image remains in use: $old_rollback_image"
  fi
  return 0
}

run_with_lock() {
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "Another deployment check holds the lock; skipping"
    return 0
  fi
  deploy_once
  result=$?
  rmdir "$LOCK_DIR" 2>/dev/null || true
  return "$result"
}

# A container cannot overlap with another container of the same name. Clear a
# lock left by an ungraceful stop before beginning the single process loop.
rmdir "$LOCK_DIR" 2>/dev/null || true

if [ "${RUN_ONCE:-false}" = true ]; then
  run_with_lock
  exit $?
fi

log "Thermostat deployer started; polling every $POLL_SECONDS seconds"
while :; do
  run_with_lock || log "Deployment check returned an unexpected error"
  sleep "$POLL_SECONDS"
done
