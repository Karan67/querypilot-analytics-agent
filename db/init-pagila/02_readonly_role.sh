#!/bin/bash
#
# Postgres init hook 2 of 2 - read-only role, delegated.
#
# Not a second copy of db/init/03_readonly_role.sh: that script is already
# fully parameterized by env vars with no Chinook-specific content, so this
# execs it from the sibling mount docker-compose.yml sets up
# (./db/init:/opt/chinook-init:ro) rather than duplicating security-relevant
# logic that could drift out of sync with Chinook's copy.
set -euo pipefail
exec /opt/chinook-init/03_readonly_role.sh
