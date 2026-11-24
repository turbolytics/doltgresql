#!/bin/sh
# Build the doltgres.main image (doltgres-main-source) from this checkout,
# then drop the build cache it leaves behind. Run from the repository root
# before any scenario that runs doltgres.main.
set -e
docker build -f temper/recipe/doltgres_main/Dockerfile -t doltgres-main-source .
docker builder prune -af >/dev/null
docker run --rm --entrypoint doltgres doltgres-main-source --version
