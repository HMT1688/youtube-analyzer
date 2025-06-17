#!/usr/bin/env bash
set -e

echo "FFmpeg 설치 시작..."
apt-get update && apt-get install -y ffmpeg

echo "의존성 설치 시작..."
pip install -r requirements.txt

echo "빌드 완료."