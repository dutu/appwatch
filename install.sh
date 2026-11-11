#!/usr/bin/env bash
set -Eeuo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

SCRIPT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)'; then
    printf '%s\n' 'Python 3.13 or newer is required; upgrade the operating system first.' >&2
    exit 1
fi
exec python3 "$SCRIPT_ROOT/scripts/install.py" "$@"
