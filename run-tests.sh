#!/usr/bin/env bash
# Run the kali-utils test suites:  ./run-tests.sh            (everything)
#                                  ./run-tests.sh tests/trctl (one tool)
# Everything runs offline in temp folders / against a fake Transmission daemon; the UI tests need tmux.
set -eu
cd "$(dirname "$(realpath "$0")")"
if [[ $# -eq 0 ]]; then set -- tests; fi
for dir in "$@"; do
  echo "== ${dir}"
  python3 -m unittest discover -s "$dir" -t . 2>&1 | tail -3
done
