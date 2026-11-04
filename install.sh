#!/usr/bin/env bash
# Install kali-utils for the current user by linking the launchers into ~/.local/bin.
# The launchers run straight from this folder, so edits take effect immediately.
#   ./install.sh            install / update the links
#   ./install.sh --remove   remove the links
set -eu
ROOT="$(dirname "$(realpath "$0")")"
BIN="${KALI_UTILS_BIN:-$HOME/.local/bin}"
TOOLS=(trctl plexsort syswatch kvctl bookread)

if [[ "${1:-}" == "--remove" ]]; then
  for t in "${TOOLS[@]}"; do
    if [[ -L "$BIN/$t" && "$(readlink "$BIN/$t")" == "$ROOT/bin/$t" ]]; then rm "$BIN/$t"; echo "removed $BIN/$t"; fi
  done
  exit 0
fi

python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "kali-utils needs Python 3.11+"; exit 1; }
mkdir -p "$BIN"
for t in "${TOOLS[@]}"; do
  ln -sfn "$ROOT/bin/$t" "$BIN/$t"
  echo "linked $BIN/$t -> $ROOT/bin/$t"
done
case ":$PATH:" in *":$BIN:"*) ;; *) echo "note: $BIN is not on your PATH" ;; esac
echo "done. Try: trctl --version; plexsort --version; syswatch --version; kvctl --version; bookread --version"
