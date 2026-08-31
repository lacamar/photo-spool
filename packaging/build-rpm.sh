#!/usr/bin/env bash
# Builds the photo-spool RPM with mx-rpm (mock-based, matching this
# machine's other RPM-packaged projects) from the current git checkout --
# uses `git archive` so the tarball only contains tracked files.
#
# Conventions this script sets up (see packaging/photo-spool.spec, and
# mx-rpm's own build-rpm.sh for the canonical version of this pattern):
#   - Spec lives in-repo (packaging/photo-spool.spec) and is symlinked into
#     ~/.local/rpm/specs/photo-spool.spec, same as this machine's other
#     packages (box64.spec, anki.spec, etc. all symlink back to their
#     project checkouts rather than living only under ~/.local/rpm/specs).
#   - Sources for this package land in mx-rpm's own default source-root
#     resolution, ~/.local/rpm/sources/photo-spool/, same as every other
#     package mx-rpm builds -- no --sourceroot override. (An earlier version
#     of this script pointed --sourceroot at ~/.local/rpm/specs, colocating
#     the tarball with the spec instead -- that scattered sources across
#     two different roots depending on which package you were building and
#     had to be fixed back to this, mx-rpm's own default, deliberately.)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v mx-rpm >/dev/null; then
    echo "mx-rpm not found on PATH (expected ~/.local/bin/mx-rpm)." >&2
    exit 1
fi

NAME=photo-spool
SPEC=packaging/photo-spool.spec
VERSION=$(awk '/^Version:/ {print $2; exit}' "$SPEC")

SPECS_ROOT="${HOME}/.local/rpm/specs"
SOURCE_DIR="${HOME}/.local/rpm/sources/${NAME}"
mkdir -p "$SOURCE_DIR"

ln -sf "$(pwd)/${SPEC}" "${SPECS_ROOT}/${NAME}.spec"
git archive --prefix="${NAME}-${VERSION}/" -o "${SOURCE_DIR}/${NAME}-${VERSION}.tar.gz" HEAD

mx-rpm --no-git-commit "$@" "$NAME"
