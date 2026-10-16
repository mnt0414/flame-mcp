#!/bin/bash
#
# Makes synthetic media for the real-Flame regression tests (it does not touch Flame).
# Three mov files, 1920x1080, 23.976 fps, ProRes 422 (Standard), timecode starting at 00:00:00:00:
#   p2_len48.mov (48 frames) / p2_len72.mov (72) / p2_len12.mov (12, for a replacement shorter
#   than its target)
#
# The picture carries the frame number (counting from 0). This ffmpeg has no drawtext
# (freetype), so the number that testsrc2 draws at the top left (second row) is scaled up 6 times
# and overlaid. The low 8 bits of the number n are also burned in at the bottom left as black and
# white boxes (bit 0 on the left), for machines to read.
#
# ffmpeg and ffprobe are looked up on PATH (the script stops if either is missing). Nothing is
# written under /opt/Autodesk. The output directory can be given as an argument.

set -euo pipefail

FFMPEG="$(command -v ffmpeg || true)"
FFPROBE="$(command -v ffprobe || true)"
OUT_DIR="${1:-$HOME/Library/Application Support/flame-mcp/probe_project/media/p2}"
WIDTH=1920
HEIGHT=1080
RATE="24000/1001"
TIMECODE="00:00:00:00"
NUM_SCALE=6
NUM_W=102
NUM_H=38
BIT_COUNT=8
BIT_SIZE=40
BIT_PITCH=50
BIT_Y=960

if [ -z "$FFMPEG" ] || [ -z "$FFPROBE" ]; then
  echo "ffmpeg or ffprobe is not on PATH. Install ffmpeg, then run this again." >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

bit_boxes="drawbox=x=50:y=$((BIT_Y - 10)):w=$((BIT_COUNT * BIT_PITCH + 10)):h=$((BIT_SIZE + 20)):color=black:t=fill"
for ((b = 0; b < BIT_COUNT; b++)); do
  bit_boxes+=",drawbox=x=$((60 + b * BIT_PITCH)):y=${BIT_Y}:w=${BIT_SIZE}:h=${BIT_SIZE}:color=white:t=fill:enable='bitand(n,$((1 << b)))'"
done

graph="[1:v]crop=${NUM_W}:${NUM_H}:0:0,scale=$((NUM_W * NUM_SCALE)):$((NUM_H * NUM_SCALE)):flags=neighbor[num];"
graph+="[0:v][num]overlay=60:60,${bit_boxes},format=yuv422p10le[v]"

make() {
  local name="$1" frames="$2"
  local out="$OUT_DIR/${name}.mov"
  "$FFMPEG" -hide_banner -loglevel error -y \
    -f lavfi -i "testsrc2=s=${WIDTH}x${HEIGHT}:r=${RATE}" \
    -f lavfi -i "testsrc2=s=320x180:r=${RATE}" \
    -filter_complex "$graph" -map "[v]" -frames:v "$frames" \
    -c:v prores_ks -profile:v 2 -vendor apl0 -timecode "$TIMECODE" -r "$RATE" \
    "$out"
  echo "Created: $out"
}

make p2_len48 48
make p2_len72 72
make p2_len12 12

for name in p2_len48 p2_len72 p2_len12; do
  echo "=== ffprobe: $OUT_DIR/${name}.mov ==="
  "$FFPROBE" -v error -count_frames -select_streams v:0 \
    -show_entries stream=codec_name,profile,pix_fmt,width,height,r_frame_rate,avg_frame_rate,nb_read_frames,duration:stream_tags=timecode:format_tags=timecode \
    -of default=noprint_wrappers=1 "$OUT_DIR/${name}.mov"
done
