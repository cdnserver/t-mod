#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CACHE="$ROOT/.render-cache"
SIZES="24 32 64 128 256"
SERVICES="home reactor consensus atlas sgl ovr games tasks admin"

mkdir -p "$CACHE/color" "$CACHE/mono"

render_svg() {
  source_file=$1
  cache_dir=$2
  output_group=$3
  name=$4

  qlmanage -t -s 512 -o "$cache_dir" "$source_file" >/dev/null 2>&1
  rendered="$cache_dir/$(basename "$source_file").png"
  for size in $SIZES; do
    destination="$ROOT/exports/$output_group/$size"
    mkdir -p "$destination"
    sips -z "$size" "$size" "$rendered" --out "$destination/$name.png" >/dev/null
  done
}

for service in $SERVICES; do
  render_svg "$ROOT/services/$service.svg" "$CACHE/color" color "$service"
  render_svg "$ROOT/services/mono/$service.svg" "$CACHE/mono" mono "$service"
done

render_svg "$ROOT/tmod/mark.svg" "$CACHE/color" tmod-color tmod
render_svg "$ROOT/tmod/mark-mono-light.svg" "$CACHE/mono" tmod-mono-light tmod
render_svg "$ROOT/tmod/mark-mono-dark.svg" "$CACHE/mono" tmod-mono-dark tmod

printf '%s\n' "T-Mod media exports generated in $ROOT/exports"
