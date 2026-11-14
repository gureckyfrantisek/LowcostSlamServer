#!/bin/sh
# Copies the working tree to ~/slam-dev on the Radxa (the real install is not touched) and runs
# the tests there. The Radxa has the compiled camera SDK, smbus2 and gpiod; a dev PC may not.
#   scripts/sync_test.sh                 # all tests
#   scripts/sync_test.sh tests/test_x.py -k name
set -eu
cd "$(dirname "$0")/.."
tar --exclude=__pycache__ --exclude=.git --exclude=sdk --exclude=.pytest_cache -cf - . \
  | tailscale ssh radxa@radxa-slam 'rm -rf ~/slam-dev && mkdir ~/slam-dev && tar -xf - -C ~/slam-dev && ln -s ~/LowcostSlamServer/sdk ~/slam-dev/sdk'
tailscale ssh radxa@radxa-slam "cd ~/slam-dev && timeout 300 ~/slam-dev-venv/bin/python -m pytest -q --durations=8 -p no:cacheprovider ${*:-tests}"
