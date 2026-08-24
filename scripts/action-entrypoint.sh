#!/usr/bin/env bash
set -euo pipefail

stage="${1:-}"

case "$stage" in
  install|prepare|analysis|verify|publish|snapshot) ;;
  *)
    echo "touchstone: invalid hosted stage" >&2
    exit 64
    ;;
esac

runner_temp="${RUNNER_TEMP:?RUNNER_TEMP is required}"
venv="$runner_temp/touchstone-action-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}"
marker="$venv/.touchstone-action-ref"
action_ref="${GITHUB_ACTION_REF:-local-source}"

if [[ "$stage" == "install" ]]; then
  for name in OPENAI_API_KEY ANTHROPIC_API_KEY TOUCHSTONE_STATE_KEY GH_TOKEN GITHUB_TOKEN; do
    if [[ -n "${!name:-}" ]]; then
      echo "touchstone: install step received a prohibited credential" >&2
      exit 78
    fi
  done
  if [[ -x "$venv/bin/touchstone" && -f "$marker" ]] &&
    [[ "$(<"$marker")" == "$action_ref" ]]; then
    exit 0
  fi
  python -m venv --clear "$venv"
  "$venv/bin/python" -m pip install --disable-pip-version-check "$GITHUB_ACTION_PATH"
  printf '%s\n' "$action_ref" >"$marker"
  exit 0
fi

if [[ ! -x "$venv/bin/touchstone" || ! -f "$marker" ]] ||
  [[ "$(<"$marker")" != "$action_ref" ]]; then
  echo "touchstone: pinned Action source is not installed" >&2
  exit 78
fi

exec "$venv/bin/touchstone" hosted "$stage"
