#!/usr/bin/env sh
set -eu

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <source-directory> <release-id>" >&2
  exit 2
fi

SOURCE_DIR=$1
RELEASE_ID=$2
ROOT_DIR="${CDN_ROOT_DIR:-/srv/tmod-cdn}"

[ -d "$SOURCE_DIR" ] || {
  echo "Source directory does not exist: $SOURCE_DIR" >&2
  exit 1
}
printf '%s' "$RELEASE_ID" | grep -Eq '^[A-Za-z0-9._-]{1,80}$' || {
  echo "Release id may contain only letters, digits, dot, underscore and dash." >&2
  exit 1
}

RELEASES_DIR="$ROOT_DIR/content/releases"
TARGET_DIR="$RELEASES_DIR/$RELEASE_ID"
TEMP_DIR="$RELEASES_DIR/.${RELEASE_ID}.tmp.$$"
[ ! -e "$TARGET_DIR" ] || {
  echo "Release already exists: $RELEASE_ID" >&2
  exit 1
}

mkdir -p "$TEMP_DIR"
trap 'rm -rf "$TEMP_DIR"' EXIT INT TERM
cp -a "$SOURCE_DIR"/. "$TEMP_DIR"/
(
  cd "$TEMP_DIR"
  find . -type f ! -name checksums.sha256 -print0 \
    | sort -z \
    | xargs -0 sha256sum > checksums.sha256
)
mv "$TEMP_DIR" "$TARGET_DIR"
ln -sfn "$TARGET_DIR" "$ROOT_DIR/content/.current-next"
mv -Tf "$ROOT_DIR/content/.current-next" "$ROOT_DIR/content/current"
trap - EXIT INT TERM

echo "Published CDN release $RELEASE_ID atomically."

