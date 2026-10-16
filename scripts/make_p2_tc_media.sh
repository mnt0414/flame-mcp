#!/bin/bash
#
# Makes synthetic media for the real-Flame regression tests (it does not touch Flame).
# Four mov files, 1920x1080, 23.976 fps, ProRes 422 (Standard), 48 frames, differing only in
# the start timecode:
#   p2_src_tc1h.mov         TC 01:00:00:00 (the source that gets replaced)
#   p2_rep_tc1h_plus10.mov  TC 01:00:00:10 (matched by TC, it overlaps only part of the used range)
#   p2_rep_tc2h.mov         TC 02:00:00:00 (matched by TC, it does not overlap; matched by
#                           offset, all of it fits)
#   p2_rep_tc1h.mov         TC 01:00:00:00 (positive control: fits under either reading)
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
FRAMES=48
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
  local name="$1" timecode="$2"
  local out="$OUT_DIR/${name}.mov"
  "$FFMPEG" -hide_banner -loglevel error -y \
    -f lavfi -i "testsrc2=s=${WIDTH}x${HEIGHT}:r=${RATE}" \
    -f lavfi -i "testsrc2=s=320x180:r=${RATE}" \
    -filter_complex "$graph" -map "[v]" -frames:v "$FRAMES" \
    -c:v prores_ks -profile:v 2 -vendor apl0 -timecode "$timecode" -r "$RATE" \
    "$out"
  echo "Created: $out"
}

make p2_src_tc1h 01:00:00:00
make p2_rep_tc1h_plus10 01:00:00:10
make p2_rep_tc2h 02:00:00:00
make p2_rep_tc1h 01:00:00:00

for name in p2_src_tc1h p2_rep_tc1h_plus10 p2_rep_tc2h p2_rep_tc1h; do
  echo "=== ffprobe: $OUT_DIR/${name}.mov ==="
  "$FFPROBE" -v error -count_frames -select_streams v:0 \
    -show_entries stream=codec_name,profile,pix_fmt,width,height,r_frame_rate,avg_frame_rate,nb_read_frames,duration:stream_tags=timecode:format_tags=timecode \
    -of default=noprint_wrappers=1 "$OUT_DIR/${name}.mov"
done
