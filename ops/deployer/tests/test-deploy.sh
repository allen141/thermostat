#!/bin/sh

set -eu

TEST_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
DEPLOYER=$(CDPATH= cd -- "$TEST_DIR/.." && pwd)/deploy.sh
MOCK_DOCKER="$TEST_DIR/mock-docker"
TEST_ROOT=$(mktemp -d /tmp/thermostat-deployer-tests.XXXXXX)

cleanup() {
  rm -rf "$TEST_ROOT"
}
trap cleanup EXIT HUP INT TERM

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

new_case() {
  CASE_DIR="$TEST_ROOT/$1"
  MOCK_DIR="$CASE_DIR/mock"
  STATE_DIR="$CASE_DIR/state"
  mkdir -p "$MOCK_DIR/containers" "$STATE_DIR"
  printf '%s\n' "sha256:old" > "$MOCK_DIR/containers/thermostat-monitor"
  export CASE_DIR MOCK_DIR STATE_DIR
}

run_deployer() {
  env \
    MOCK_DIR="$MOCK_DIR" \
    MOCK_NEW_IMAGE_ID="${MOCK_NEW_IMAGE_ID:-sha256:new}" \
    MOCK_PULL_FAIL="${MOCK_PULL_FAIL:-false}" \
    MOCK_SMOKE_FAIL="${MOCK_SMOKE_FAIL:-false}" \
    MOCK_PRODUCTION_FAIL="${MOCK_PRODUCTION_FAIL:-false}" \
    MOCK_PRODUCTION_RUN_FAIL="${MOCK_PRODUCTION_RUN_FAIL:-false}" \
    DOCKER_BIN="$MOCK_DOCKER" \
    STATE_DIR="$STATE_DIR" \
    RUN_ONCE=true \
    HEALTH_ATTEMPTS=1 \
    HEALTH_POLL_SECONDS=0 \
    "$DEPLOYER"
}

assert_content() {
  file=$1
  expected=$2
  [ -f "$file" ] || fail "missing file $file"
  actual=$(cat "$file")
  [ "$actual" = "$expected" ] ||
    fail "$file contained '$actual', expected '$expected'"
}

new_case unchanged
MOCK_NEW_IMAGE_ID=sha256:old
export MOCK_NEW_IMAGE_ID
run_deployer
assert_content "$STATE_DIR/current-image-id" "sha256:old"
if grep -q '^stop ' "$MOCK_DIR/commands.log"; then
  fail "unchanged image stopped production"
fi
unset MOCK_NEW_IMAGE_ID

new_case smoke_failure
MOCK_SMOKE_FAIL=true
export MOCK_SMOKE_FAIL
run_deployer
assert_content "$STATE_DIR/failed-image-id" "sha256:new"
assert_content "$MOCK_DIR/containers/thermostat-monitor" "sha256:old"
if grep -q '^stop ' "$MOCK_DIR/commands.log"; then
  fail "smoke failure stopped production"
fi
unset MOCK_SMOKE_FAIL

new_case success
run_deployer
assert_content "$STATE_DIR/current-image-id" "sha256:new"
assert_content "$MOCK_DIR/containers/thermostat-monitor" "sha256:new"
assert_content "$MOCK_DIR/containers/thermostat-monitor-rollback" "sha256:old"
[ ! -f "$STATE_DIR/failed-image-id" ] ||
  fail "successful deployment retained failed-image-id"

new_case production_failure
MOCK_PRODUCTION_FAIL=true
export MOCK_PRODUCTION_FAIL
run_deployer
assert_content "$STATE_DIR/failed-image-id" "sha256:new"
assert_content "$MOCK_DIR/containers/thermostat-monitor" "sha256:old"
[ ! -f "$MOCK_DIR/containers/thermostat-monitor-rollback" ] ||
  fail "rollback container was not restored to production"
unset MOCK_PRODUCTION_FAIL

printf '%s\n' "All deployer state-machine tests passed."
