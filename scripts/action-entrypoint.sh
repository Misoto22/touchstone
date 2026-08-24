#!/usr/bin/env bash
set -euo pipefail

stage="${1:-}"
version="${2:-}"

case "$stage" in
  prepare|analysis|publish|snapshot) ;;
  *)
    echo "touchstone: invalid hosted stage" >&2
    exit 64
    ;;
esac

if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([a-zA-Z0-9.-]*)?$ ]]; then
  echo "touchstone: version must be an exact package version" >&2
  exit 64
fi

runner_temp="${RUNNER_TEMP:?RUNNER_TEMP is required}"
venv="$runner_temp/touchstone-action-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}"
python -m venv --clear "$venv"
"$venv/bin/python" -m pip install --disable-pip-version-check "touchstone-agent==${version}"
PATH="$venv/bin:$PATH"
export PATH
touchstone hosted "$stage"
