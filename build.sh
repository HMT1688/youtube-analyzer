#!/usr/bin/env bash
set -o errexit

echo "파이썬 라이브러리 설치를 시작합니다..."
pip install -r requirements.txt

echo "빌드 스크립트 완료."