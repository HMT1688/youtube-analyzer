#!/usr/bin/env bash
# 오류가 발생하면 즉시 중단
set -o errexit

echo "FFmpeg 설치를 시작합니다..."
# 시스템 업데이트 및 ffmpeg 설치
apt-get update && apt-get install -y ffmpeg

echo "파이썬 라이브러리 설치를 시작합니다..."
# requirements.txt에 있는 모든 라이브러리 설치
pip install -r requirements.txt

echo "빌드 스크립트 완료."