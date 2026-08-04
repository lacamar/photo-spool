#!/usr/bin/env bash
# Builds the photo-import RPM with mx-rpm (mock-based, matching this
# machine's other RPM-packaged projects) from the current git checkout --
# uses `git archive` so the tarball only contains tracked files.
#
# Conventions this script sets up (see ~/.local/bin/mx-rpm):
#   - Spec lives in-repo (packaging/photo-import.spec) and is symlinked into
#     ~/.local/rpm/specs/photo-import.spec, same as this machine's other
#     packages (box64.spec, anki.spec, etc. all symlink back to their
#     project checkouts rather than living only under ~/.local/rpm/specs).
#   - Sources for this package are colocated under
#     ~/.local/rpm/specs/photo-import/ rather than mx-rpm's own default
#     ~/.local/rpm/sources/photo-import/ -- passed via --sourceroot so the
#     per-package resolution lands there instead.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v mx-rpm >/dev/null; then
    echo "mx-rpm not found on PATH (expected ~/.local/bin/mx-rpm)." >&2
    exit 1
fi

NAME=photo-import
SPEC=packaging/photo-import.spec
VERSION=$(awk '/^Version:/ {print $2; exit}' "$SPEC")

SPECS_ROOT="${HOME}/.local/rpm/specs"
SOURCE_DIR="${SPECS_ROOT}/${NAME}"
mkdir -p "$SOURCE_DIR"

ln -sf "$(pwd)/${SPEC}" "${SPECS_ROOT}/${NAME}.spec"
git archive --prefix="${NAME}-${VERSION}/" -o "${SOURCE_DIR}/${NAME}-${VERSION}.tar.gz" HEAD

mx-rpm --sourceroot "$SPECS_ROOT" --no-git-commit "$@" "$NAME"
