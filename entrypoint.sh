#!/bin/bash
set -e

# Symlink credentials from the host-mounted directory so the container always
# resolves the current inode. A file bind mount tracks the inode at mount time
# and misses atomic host refreshes; a directory mount resolves paths at access
# time, so the symlink target is always current.
ln -sf /home/appuser/.claude-host/.credentials.json /home/appuser/.claude/.credentials.json

exec "$@"
