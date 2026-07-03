#!/usr/bin/env bash
# Allowlist-shaped hygiene scan. Anything that looks like an internal
# name fails unless it's one of the public ones — a denylist here
# would have to name the very things it exists to keep out.
#
#   scripts/hygiene.sh            scan the repo tree
#   scripts/hygiene.sh --stdin    scan text on stdin (PR titles/bodies)
set -euo pipefail

fail=0

if [[ "${1:-}" == "--stdin" ]]
then
  tmp="$(mktemp)"
  trap 'rm -f "${tmp}"' EXIT
  cat >"${tmp}"
  grep -InoE 'analog[-_][a-z]+' "${tmp}" | grep -vE ':analog[-_](sdk|mcp|fetcher)$' && fail=1
  grep -InoE 'getanalog/[a-z-]+' "${tmp}" | grep -vE ':getanalog/(homebrew-tap|tap)$' && fail=1
  grep -InE 'amazonaws[.]com|arn[:]aws|mono[r]epo' "${tmp}" && fail=1
else
  grep -rInoE 'analog[-_][a-z]+' --exclude-dir=.git . | grep -vE ':analog[-_](sdk|mcp|fetcher)$' && fail=1
  grep -rInoE 'getanalog/[a-z-]+' --exclude-dir=.git . | grep -vE ':getanalog/(homebrew-tap|tap)$' && fail=1
  grep -rInE 'amazonaws[.]com|arn[:]aws|mono[r]epo' --exclude-dir=.git . && fail=1
fi

if [[ "${fail}" -ne 0 ]]
then
  echo "::error::internal-looking reference found, see matches above" >&2
  exit 1
fi
echo "clean"
