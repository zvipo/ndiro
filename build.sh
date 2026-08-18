#!/bin/sh
# Build the Ndiro image with the running commit stamped into it, so /status and
# /health can report what is actually deployed.
#
# This wrapper exists because .git is dockerignored: inside the image there is
# no way to work the hash out, so it has to come in as a build arg. A plain
# `docker build -t ndiro .` still works — /status then just says "unknown".
# (On Render nothing is needed: it injects RENDER_GIT_COMMIT at run time.)
#
#   ./build.sh                                  # tags ndiro
#   ./build.sh ndiro:2026-08-18                 # tags that instead
#   ./build.sh ndiro --build-arg INSTALL_HEIC=0 # extra flags pass through
set -eu

TAG="${1:-ndiro}"
[ $# -gt 0 ] && shift || true

COMMIT=$(git rev-parse HEAD 2>/dev/null || true)
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)
TITLE=$(git log -1 --format=%s 2>/dev/null || true)
BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)

if [ -z "$COMMIT" ]; then
    echo "build.sh: not a git checkout — building without a commit stamp." >&2
elif [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    # The image would contain the working tree, not that commit exactly. Stamp
    # it anyway (it is the closest true answer) but say so out loud.
    echo "build.sh: WARNING — uncommitted changes; the image will not match $(printf %.7s "$COMMIT") exactly." >&2
fi

set -x
exec docker build -t "$TAG" \
    --build-arg GIT_COMMIT="$COMMIT" \
    --build-arg GIT_BRANCH="$BRANCH" \
    --build-arg GIT_COMMIT_TITLE="$TITLE" \
    --build-arg BUILD_TIME="$BUILD_TIME" \
    "$@" .
