#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
output="${1:-$repo_root/artifacts/t-mod-gource.mp4}"
width="${WIDTH:-1920}"
height="${HEIGHT:-1080}"
fps="${FPS:-60}"
seconds_per_day="${SECONDS_PER_DAY:-2.2}"
audio_file="${AUDIO_FILE:-}"

for command_name in gource ffmpeg git; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'Missing dependency: %s\n' "$command_name" >&2
    exit 1
  fi
done

mkdir -p "$(dirname "$output")"

gource_args=(
  "$repo_root"
  "--viewport" "${width}x${height}"
  "--output-ppm-stream" "-"
  "--output-framerate" "$fps"
  "--stop-at-end"
  "--file-idle-time" "0"
  "--file-idle-time-at-end" "8"
  "--seconds-per-day" "$seconds_per_day"
  "--auto-skip-seconds" "1.2"
  "--max-file-lag" "0.35"
  "--camera-mode" "overview"
  "--padding" "1.08"
  "--elasticity" "0.08"
  "--user-friction" "0.75"
  "--max-user-speed" "420"
  "--background-colour" "070A12"
  "--font-colour" "E8EEFF"
  "--filename-colour" "B8C7F2"
  "--dir-colour" "6EE7F9"
  "--highlight-colour" "F9C74F"
  "--selection-colour" "FF6B9A"
  "--bloom-multiplier" "1.35"
  "--bloom-intensity" "0.8"
  "--font-scale" "1.15"
  "--font-size" "28"
  "--file-font-size" "16"
  "--dir-font-size" "20"
  "--user-font-size" "22"
  "--filename-time" "3.5"
  "--dir-name-depth" "2"
  "--highlight-dirs"
  "--highlight-users"
  "--title" "t-mod  ·  repository evolution"
  "--date-format" "%d %B %Y"
  "--hash-seed" "20260714"
  "--disable-input"
  "--hide" "mouse,progress"
  "--file-filter" '(^|/)(\.git|\.venv|node_modules|__pycache__|\.ruff_cache)(/|$)|\.(pyc|pyo)$'
)

ffmpeg_video_args=(
  -y
  -f image2pipe
  -vcodec ppm
  -r "$fps"
  -i -
)

if [[ -n "$audio_file" ]]; then
  if [[ ! -f "$audio_file" ]]; then
    printf 'Audio file not found: %s\n' "$audio_file" >&2
    exit 1
  fi
  ffmpeg_video_args+=(
    -stream_loop -1
    -i "$audio_file"
    -map 0:v:0
    -map 1:a:0
    -shortest
    -c:a aac
    -b:a 256k
    -af "afade=t=in:st=0:d=2"
  )
fi

printf 'Rendering %sx%s at %s fps to %s\n' "$width" "$height" "$fps" "$output"
gource "${gource_args[@]}" |
  ffmpeg "${ffmpeg_video_args[@]}" \
    -c:v libx264 \
    -preset medium \
    -crf 18 \
    -pix_fmt yuv420p \
    -movflags +faststart \
    "$output"

printf 'Done: %s\n' "$output"
