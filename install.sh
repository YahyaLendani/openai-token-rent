#!/bin/sh
# Installs (or upgrades) the openai-token-rent skill for every agent found on this machine:
#   Codex CLI   -> ~/.agents/skills/openai-token-rent
#   Claude Code -> ~/.claude/skills/openai-token-rent
# Usage:  curl -fsSL https://raw.githubusercontent.com/YahyaLendani/openai-token-rent/main/install.sh | sh
# It downloads the latest release from GitHub. It uploads nothing and touches only those folders.
set -eu

REPO="YahyaLendani/openai-token-rent"
NAME="openai-token-rent"
HOME_DIR="${TOKEN_RENT_HOME:-$HOME}"     # override for testing
SRC="${TOKEN_RENT_SRC:-}"                 # a local .tar.gz instead of downloading (testing/offline)

tmp=$(mktemp -d 2>/dev/null || mktemp -d -t token-rent)
trap 'rm -rf "$tmp"' EXIT INT TERM

if [ -z "$SRC" ]; then
  command -v curl >/dev/null 2>&1 || { echo "token-rent: curl is required" >&2; exit 1; }
  # /releases/latest redirects to /releases/tag/<tag>; fall back to main if there is no release yet
  latest=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest" || true)
  case "$latest" in
    */tag/*) ref="refs/tags/${latest##*/tag/}" ;;
    *)       ref="refs/heads/main" ;;
  esac
  echo "token-rent: downloading $REPO ($ref)"
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$ref" -o "$tmp/src.tar.gz"
  SRC="$tmp/src.tar.gz"
fi

mkdir "$tmp/x"
tar -xzf - -C "$tmp/x" < "$SRC"   # via stdin: GNU tar reads "C:/..." as a remote host
src_dir=$(find "$tmp/x" -mindepth 1 -maxdepth 1 -type d | head -n 1)
if [ -z "$src_dir" ] || [ ! -f "$src_dir/SKILL.md" ]; then
  echo "token-rent: the download does not contain SKILL.md; nothing installed" >&2
  exit 1
fi
rm -rf "$src_dir/evals" "$src_dir/.gitattributes" "$src_dir/.gitignore"   # development-only files

targets=""
if [ -d "$HOME_DIR/.codex" ] || command -v codex >/dev/null 2>&1; then
  targets="$targets $HOME_DIR/.agents/skills"
fi
if [ -d "$HOME_DIR/.claude" ] || command -v claude >/dev/null 2>&1; then
  targets="$targets $HOME_DIR/.claude/skills"
fi
if [ -z "$targets" ]; then
  targets="$HOME_DIR/.agents/skills"       # the open Agent Skills location; Codex reads it
  echo "token-rent: no Codex or Claude Code found; installing to the standard ~/.agents/skills"
fi

for t in $targets; do
  dest="$t/$NAME"
  if [ -d "$dest/.git" ]; then
    echo "token-rent: $dest is a git clone; leaving it alone (run git pull there to upgrade)"
    continue
  fi
  mkdir -p "$t"
  rm -rf "$dest.new"
  cp -R "$src_dir" "$dest.new"
  rm -rf "$dest"
  mv "$dest.new" "$dest"
  echo "token-rent: installed $dest"
done

echo "token-rent: done. Ask your agent about OpenAI costs; it loads the skill by itself."
echo "            (If it does not show up, restart Codex or Claude Code once.)"
