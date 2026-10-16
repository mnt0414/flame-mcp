#!/bin/bash
#
# Makes the three plain-colour clips that most of the real-Flame regression tests import (it does
# not touch Flame):
#   clip1.mov (red) / clip2.mov (green) / clip3.mov (blue)
# Each is 1920x1080, 23.976 fps, 24 frames, ProRes 422 (Standard), 10-bit 4:2:2, no audio and no
# timecode tag.
#
# Usage: make_clip_media.sh [--force] [OUT_DIR]
#
# OUT_DIR defaults to the media folder the tests read. The clips go directly in OUT_DIR. The
# script stops, and writes nothing, if any of the three files is already there; --force replaces
# them. A clip is made in a hidden work folder inside OUT_DIR, checked with ffprobe against the
# values above, and only then moved into place, so a failed check never leaves a bad clip behind
# and never damages a clip that was there before.
#
# ffmpeg and ffprobe are looked up on PATH (the script stops if either is missing). Nothing is
# written under /opt/Autodesk.

set -euo pipefail

FFMPEG="$(command -v ffmpeg || true)"
FFPROBE="$(command -v ffprobe || true)"
DEFAULT_OUT_DIR="$HOME/Library/Application Support/flame-mcp/probe_project/media"
WIDTH=1920
HEIGHT=1080
RATE="24000/1001"
FRAMES=24
CLIPS=("clip1:red" "clip2:green" "clip3:blue")

EXPECTED_STREAM="codec_name=prores
codec_tag_string=apcn
profile=Standard
width=1920
height=1080
pix_fmt=yuv422p10le
color_range=tv
field_order=progressive
r_frame_rate=24000/1001
avg_frame_rate=24000/1001
duration=1.001000
nb_frames=24
nb_read_frames=24"

usage() {
  echo "Usage: $0 [--force] [OUT_DIR]" >&2
}

force=0
out_dir="$DEFAULT_OUT_DIR"
dir_given=0
for arg in "$@"; do
  case "$arg" in
    --force) force=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    -*)
      echo "Unknown option: $arg" >&2
      usage
      exit 2
      ;;
    *)
      if [ "$dir_given" -eq 1 ]; then
        echo "Only one output folder can be given." >&2
        usage
        exit 2
      fi
      out_dir="$arg"
      dir_given=1
      ;;
  esac
done

if [ -z "$FFMPEG" ] || [ -z "$FFPROBE" ]; then
  echo "ffmpeg or ffprobe is not on PATH. Install ffmpeg, then run this again." >&2
  exit 1
fi

if [ "$force" -eq 0 ]; then
  existing=0
  for entry in "${CLIPS[@]}"; do
    if [ -e "$out_dir/${entry%%:*}.mov" ] || [ -L "$out_dir/${entry%%:*}.mov" ]; then
      echo "Already exists: $out_dir/${entry%%:*}.mov" >&2
      existing=1
    fi
  done
  if [ "$existing" -eq 1 ]; then
    echo "Nothing was written. Use --force to replace the existing clips." >&2
    exit 1
  fi
fi

mkdir -p "$out_dir"
work_dir="$(mktemp -d "$out_dir/.make_clip_media.XXXXXX")"
trap 'rm -rf "$work_dir"' EXIT

verify() {
  local file="$1" name="$2" actual extra
  actual="$("$FFPROBE" -v error -count_frames -select_streams v:0 \
    -show_entries stream=codec_name,codec_tag_string,profile,pix_fmt,color_range,field_order,width,height,r_frame_rate,avg_frame_rate,duration,nb_frames,nb_read_frames \
    -of default=noprint_wrappers=1 "$file" | sort)"
  if [ "$actual" != "$(echo "$EXPECTED_STREAM" | sort)" ]; then
    echo "$name: the video stream is not as expected." >&2
    diff <(echo "$EXPECTED_STREAM" | sort) <(echo "$actual") >&2 || true
    return 1
  fi
  extra="$("$FFPROBE" -v error -show_entries stream=codec_type -of csv=p=0 "$file")"
  if [ "$extra" != "video" ]; then
    echo "$name: expected exactly one video stream and no audio, found: $(echo "$extra" | tr '\n' ' ')" >&2
    return 1
  fi
  extra="$("$FFPROBE" -v error -show_entries stream_tags=timecode:format_tags=timecode \
    -of default=noprint_wrappers=1 "$file")"
  if [ -n "$extra" ]; then
    echo "$name: a timecode tag is present: $extra" >&2
    return 1
  fi
}

for entry in "${CLIPS[@]}"; do
  name="${entry%%:*}"
  colour="${entry##*:}"
  "$FFMPEG" -hide_banner -loglevel error -y \
    -f lavfi -i "color=c=${colour}:s=${WIDTH}x${HEIGHT}:r=${RATE}" \
    -frames:v "$FRAMES" \
    -c:v prores_ks -profile:v standard -pix_fmt yuv422p10le \
    "$work_dir/${name}.mov"
  verify "$work_dir/${name}.mov" "$name"
done

for entry in "${CLIPS[@]}"; do
  name="${entry%%:*}"
  mv -f "$work_dir/${name}.mov" "$out_dir/${name}.mov"
  echo "Created: $out_dir/${name}.mov"
done
