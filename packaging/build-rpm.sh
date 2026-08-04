#!/usr/bin/env bash
# Builds the photo-import RPM from the current git checkout (must be a git
# repo -- uses `git archive` so the tarball only contains tracked files).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v rpmbuild >/dev/null; then
    echo "rpmbuild not found. Install with: sudo dnf install rpm-build rpmdevtools" >&2
    exit 1
fi

NAME=photo-import
SPEC=packaging/photo-import.spec
VERSION=$(awk '/^Version:/ {print $2; exit}' "$SPEC")

RPMBUILD_ROOT="${HOME}/rpmbuild"
mkdir -p "${RPMBUILD_ROOT}"/{SOURCES,SPECS,BUILD,BUILDROOT,RPMS,SRPMS}

git archive --prefix="${NAME}-${VERSION}/" -o "${RPMBUILD_ROOT}/SOURCES/${NAME}-${VERSION}.tar.gz" HEAD
cp "$SPEC" "${RPMBUILD_ROOT}/SPECS/${NAME}.spec"

rpmbuild -bb "${RPMBUILD_ROOT}/SPECS/${NAME}.spec"

echo
echo "Built:"
find "${RPMBUILD_ROOT}/RPMS" -name "${NAME}-${VERSION}*.rpm"
echo
echo "Install with: sudo dnf install <path above>"
