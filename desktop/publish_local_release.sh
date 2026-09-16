#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESKTOP="$ROOT/desktop"
REPOSITORY="${TMOD_RELEASE_REPOSITORY:-cdnserver/t-mod-releases}"
VERSION="$(node -p "require('$DESKTOP/package.json').version")"
TAG="v$VERSION"

command -v gh >/dev/null || { echo "GitHub CLI (gh) is required" >&2; exit 1; }
command -v pnpm >/dev/null || { echo "pnpm is required" >&2; exit 1; }
gh auth status >/dev/null

echo "[T-Mod Desktop] Testing $VERSION"
cd "$DESKTOP"
pnpm install --frozen-lockfile
pnpm test
pnpm typecheck
pnpm build
pnpm check:bundle

rm -rf release
echo "[T-Mod Desktop] Building macOS universal installers locally"
CSC_IDENTITY_AUTO_DISCOVERY=false pnpm exec electron-builder \
  --mac dmg zip --universal --publish never

notes="$(mktemp)"
trap 'rm -f "$notes"' EXIT
cat >"$notes" <<EOF
T-Mod Desktop $VERSION · Beta

- обязательные системные обновления устанавливаются до открытия устаревших контуров;
- появился отдельный защищённый экран загрузки и установки обновления;
- обновлена доменная архитектура Atlas, Сената и персонального Reactor;
- правовой центр Atlas получил полную оферту и политику обработки данных.
EOF

if gh release view "$TAG" --repo "$REPOSITORY" >/dev/null 2>&1; then
  gh release edit "$TAG" --repo "$REPOSITORY" \
    --title "T-Mod Desktop $VERSION · Beta" --notes-file "$notes"
else
  gh release create "$TAG" --repo "$REPOSITORY" \
    --title "T-Mod Desktop $VERSION · Beta" --notes-file "$notes"
fi

shopt -s nullglob
files=(release/*.dmg release/*.zip release/*.blockmap release/latest-mac.yml)
((${#files[@]})) || { echo "No macOS installers were produced" >&2; exit 1; }
gh release upload "$TAG" "${files[@]}" --repo "$REPOSITORY" --clobber
echo "[T-Mod Desktop] macOS Beta $VERSION published. Mark it Latest after the native Windows build."
