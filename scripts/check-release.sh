#!/bin/sh
# Run the release checks in the same Linux runtime that will be published.
set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
python3 scripts/check-plain-ascii.py

: "${CI_REVISION:?CI_REVISION is required}"
scratch=$(mktemp -d)
image="deskmate/ci:${CI_REVISION}-${scratch##*/}"
container=''
status=0
trap '
  status=$?
  trap - EXIT
  if [ -n "$container" ]; then
    docker rm --force "$container" || status=1
  fi
  docker image rm "$image" || status=1
  rm -f "$scratch/files" "$scratch/source.tar"
  rmdir "$scratch"
  exit "$status"
' EXIT
docker build --platform linux/amd64 --file dashboard/Dockerfile --tag "$image" .

# Copy source rather than bind a checkout path that belongs to a runner container.
git ls-files --cached --others --exclude-standard -z > "$scratch/files"
tar --null --verbatim-files-from --no-recursion \
  --files-from "$scratch/files" -cf "$scratch/source.tar"
container=$(docker create --user 0 \
  --workdir /app/dashboard --env UV_CACHE_DIR=/tmp/uv-cache \
  --env RUFF_CACHE_DIR=/tmp/ruff-cache "$image" sh -eu -c '
    uv sync --frozen --group dev --no-install-project
    uv run pytest -q -o cache_dir=/tmp/pytest-cache
    uvx --from ruff==0.12.0 ruff check . ../scripts/publish-image.py
  ')
docker cp - "$container:/app" < "$scratch/source.tar"
docker start --attach "$container"
exit "$(docker inspect --format '{{.State.ExitCode}}' "$container")"
