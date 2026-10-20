#!/bin/bash
#
# Makes synthetic media with audio for the real-Flame regression tests (it does not touch Flame).
# One mov file, 1920x1080, 23.976 fps, ProRes 422 (Standard), 72 frames, timecode starting at
# 00:00:00:00, with PCM audio (24-bit, 48 kHz, 2 channels, a 440 Hz sine wave) as long as the
# picture:
#   p2_av72.mov
#
# The picture is the one of make_p2_media.sh: it carries the frame number (counting from 0), and
# the low 8 bits of the number are burned in at the bottom left as black and white boxes.
# The audio has 144144 samples (72 frames * 2002 samples per frame), so the video and the audio
# end on the same frame. The script checks that with ffprobe before it puts the file in place.
#
# ffmpeg and ffprobe are looked up on PATH (the script stops if either is missing). Nothing is
# written under /opt/Autodesk. The output directory can be given as an argument.

set -euo pipefail

FFMPEG="$(command -v ffmpeg || true)"
FFPROBE="$(command -v ffprobe || true)"
OUT_DIR="${1:-$HOME/Library/Application Support/flame-mcp/probe_project/media/p2}"
NAME="p2_av72"
WIDTH=1920
HEIGHT=1080
RATE="24000/1001"
FRAMES=72
TIMECODE="00:00:00:00"
SAMPLE_RATE=48000
SAMPLES_PER_FRAME=2002
SAMPLES=$((FRAMES * SAMPLES_PER_FRAME))
AUDIO_SECONDS="$((FRAMES * 1001 / 24000)).$(printf '%03d' $((FRAMES * 1001 % 24000 * 1000 / 24000)))"
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
work_dir="$(mktemp -d "$OUT_DIR/.make_p2_av_media.XXXXXX")"
trap 'rm -rf "$work_dir"' EXIT

bit_boxes="drawbox=x=50:y=$((BIT_Y - 10)):w=$((BIT_COUNT * BIT_PITCH + 10)):h=$((BIT_SIZE + 20)):color=black:t=fill"
for ((b = 0; b < BIT_COUNT; b++)); do
  bit_boxes+=",drawbox=x=$((60 + b * BIT_PITCH)):y=${BIT_Y}:w=${BIT_SIZE}:h=${BIT_SIZE}:color=white:t=fill:enable='bitand(n,$((1 << b)))'"
done

graph="[1:v]crop=${NUM_W}:${NUM_H}:0:0,scale=$((NUM_W * NUM_SCALE)):$((NUM_H * NUM_SCALE)):flags=neighbor[num];"
graph+="[0:v][num]overlay=60:60,${bit_boxes},format=yuv422p10le[v];"
graph+="[2:a]aformat=sample_fmts=s32:channel_layouts=stereo,atrim=end_sample=${SAMPLES}[a]"

"$FFMPEG" -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc2=s=${WIDTH}x${HEIGHT}:r=${RATE}" \
  -f lavfi -i "testsrc2=s=320x180:r=${RATE}" \
  -f lavfi -i "sine=frequency=440:sample_rate=${SAMPLE_RATE}:duration=${AUDIO_SECONDS}" \
  -filter_complex "$graph" -map "[v]" -map "[a]" -frames:v "$FRAMES" \
  -c:v prores_ks -profile:v 2 -vendor apl0 -timecode "$TIMECODE" -r "$RATE" \
  -c:a pcm_s24le -ar "$SAMPLE_RATE" \
  "$work_dir/${NAME}.mov"

video="$("$FFPROBE" -v error -count_frames -select_streams v:0 \
  -show_entries stream=codec_name,width,height,r_frame_rate,nb_read_frames \
  -of default=noprint_wrappers=1 "$work_dir/${NAME}.mov" | sort)"
expected_video="codec_name=prores
height=${HEIGHT}
nb_read_frames=${FRAMES}
r_frame_rate=${RATE}
width=${WIDTH}"
if [ "$video" != "$expected_video" ]; then
  echo "${NAME}: the video stream is not as expected." >&2
  diff <(echo "$expected_video") <(echo "$video") >&2 || true
  exit 1
fi

audio="$("$FFPROBE" -v error -select_streams a:0 \
  -show_entries stream=codec_name,sample_rate,channels \
  -of default=noprint_wrappers=1 "$work_dir/${NAME}.mov" | sort)"
expected_audio="channels=2
codec_name=pcm_s24le
sample_rate=${SAMPLE_RATE}"
if [ "$audio" != "$expected_audio" ]; then
  echo "${NAME}: the audio stream is not as expected." >&2
  diff <(echo "$expected_audio") <(echo "$audio") >&2 || true
  exit 1
fi

audio_samples="$("$FFMPEG" -hide_banner -loglevel error -i "$work_dir/${NAME}.mov" -map 0:a:0 \
  -f s32le -c:a pcm_s32le - | wc -c | tr -d ' ')"
expected_bytes=$((SAMPLES * 2 * 4))
if [ "$audio_samples" != "$expected_bytes" ]; then
  echo "${NAME}: the audio holds $((audio_samples / 8)) samples, expected ${SAMPLES}." >&2
  exit 1
fi

mv -f "$work_dir/${NAME}.mov" "$OUT_DIR/${NAME}.mov"
echo "Created: $OUT_DIR/${NAME}.mov"
echo "=== ffprobe: $OUT_DIR/${NAME}.mov ==="
"$FFPROBE" -v error -show_entries stream=codec_type,codec_name,sample_rate,channels,width,height,r_frame_rate,duration,nb_frames \
  -of default=noprint_wrappers=1 "$OUT_DIR/${NAME}.mov"
