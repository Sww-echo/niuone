#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

prune_niuone_images() {
  docker image prune --force \
    --filter "label=org.opencontainers.image.title=NiuOne"
}

echo "== Reclaim dangling NiuOne images before build =="
prune_niuone_images

echo "== Build NiuOne Compose images =="
docker compose build

echo "== Reclaim the superseded NiuOne image =="
prune_niuone_images
