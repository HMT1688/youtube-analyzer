# app.py

import os
import io
import tempfile
import traceback
import re
import math
from datetime import datetime, timezone
from functools import lru_cache

from flask import Flask, request, send_file, render_template, abort, jsonify
from flask.logging import create_logger
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pytubefix import YouTube
from faster_whisper import WhisperModel

# --- Flask 앱 설정 ---
app = Flask(__name__)
logger = create_logger(app)

# --- 환경 변수 및 설정 ---
API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    logger.error("YOUTUBE_API_KEY 환경 변수가 설정되지 않았습니다!")
    raise RuntimeError("YOUTUBE_API_KEY 환경 변수 필요")

CPM_USD = float(os.getenv("CPM_USD", 1.5))
DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"

# --- Whisper 모델 로드 (앱 시작 시 한 번) ---
logger.info("Whisper 모델을 로드하는 중입니다...")
try:
    WHISPER_MODEL = WhisperModel('base', device='cpu', compute_type="int8")
    logger.info("Whisper 모델 로드 완료.")
except Exception as e:
    logger.error(f"Whisper 모델 로드 실패: {e}")
    WHISPER_MODEL = None

# --- 유틸리티 함수들 ---
def parse_iso_date(iso_str):
    s = iso_str.rstrip("Z")
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)

def parse_duration(duration_str):
    if not duration_str: return 0
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
    if not match: return 0
    h, m, s = match.groups()
    return int(h or 0) * 3600 + int(m or 0) * 60 + int(s or 0)

def format_seconds(seconds):
    if not seconds: return "N/A"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    parts = []
    if h: parts.append(f"{h}시간")
    if m: parts.append(f"{m}분")
    if s or not parts: parts.append(f"{s}초")
    return ' '.join(parts)

@lru_cache(maxsize=1)
def get_youtube_client():
    return build("youtube", "v3", developerKey=API_KEY)

def extract_channel_id_from_url(url):
    youtube = get_youtube_client()
    try:
        if "channel/" in url:
            return url.split("channel/")[1].split("/")[0]
        if "user/" in url:
            user = url.split("user/")[1].split("/")[0]
            return youtube.channels().list(part="id", forUsername=user).execute()["items"][0]["id"]
        if "/@" in url:
            handle = url.split("/@")[1].split("/")[0]
            return youtube.channels().list(part="id", forHandle=handle).execute()["items"][0]["id"]
    except Exception:
        logger.exception(f"채널 ID 추출 실패: {url}")
    return None

def fetch_all_videos(channel_id, max_videos=200):
    youtube = get_youtube_client()
    try:
        ch = youtube.channels().list(part="contentDetails", id=channel_id).execute()
        uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        video_ids, token = [], None
        while len(video_ids) < max_videos:
            pl = youtube.playlistItems().list(part="snippet", playlistId=uploads, maxResults=50, pageToken=token).execute()
            video_ids.extend([i["snippet"]["resourceId"]["videoId"] for i in pl.get("items", [])])
            token = pl.get("nextPageToken")
            if not token: break
        
        vids = []
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i+50]
            resp = youtube.videos().list(part="snippet,statistics,contentDetails", id=",".join(batch)).execute()
            for v in resp.get("items", []):
                sn, st, cd = v["snippet"], v["statistics"], v.get("contentDetails", {})
                vids.append({
                    "id": v["id"], "title": sn.get("title", "제목 없음"),
                    "thumb": sn.get("thumbnails", {}).get("medium", {}).get("url", ""),
                    "url": f"https://youtu.be/{v['id']}",
                    "published": parse_iso_date(sn.get("publishedAt", "")),
                    "views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                    "comments": int(st.get("commentCount", 0)),
                    "duration_sec": parse_duration(cd.get("duration", ""))
                })
        return sorted(vids, key=lambda x: x["published"], reverse=True)
    except Exception:
        logger.exception("동영상 가져오기 중 오류")
        return None

# --- 라우트 정의 ---
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/analyze')
def analyze():
    url = request.args.get('url', '').strip()
    sort_by = request.args.get('sortBy', 'published') 
    try: page = max(int(request.args.get('page', 1)), 1)
    except ValueError: page = 1

    if not url: return render_template('index.html', error="채널 URL을 입력해주세요.")

    cid = extract_channel_id_from_url(url)
    if not cid: return render_template('index.html', error="유효하지 않은 채널 URL입니다.")

    try:
        info = get_youtube_client().channels().list(part="snippet,statistics", id=cid).execute()["items"][0]
    except HttpError as e:
        if e.resp.status == 429:
             return render_template('index.html', error="API 사용량을 초과했습니다. 내일 다시 시도해주세요.")
        logger.exception("채널 정보 로딩 실패")
        return render_template('index.html', error=f"채널 정보 로딩 중 오류: {e}")
    except Exception as e:
        logger.exception("채널 정보 로딩 실패")
        return render_template('index.html', error=f"채널 정보 로딩 중 오류: {e}")


    stats = {
        "title": info["snippet"].get("title", ""), "description": info["snippet"].get("description", ""),
        "subscribers": int(info["statistics"].get("subscriberCount", 0)),
        "total_views": int(info["statistics"].get("viewCount", 0)),
        "created_date": parse_iso_date(info["snippet"].get("publishedAt","")).date(),
        "video_count": int(info["statistics"].get("videoCount", 0)),
        "profile_image": info["snippet"].get("thumbnails", {}).get("high", {}).get("url", "")
    }

    videos = fetch_all_videos(cid)
    if videos is None: return render_template('index.html', error="동영상 목록을 가져올 수 없습니다.")

    # 정렬
    videos.sort(key=lambda x: x.get(sort_by, x['published']), reverse=True)

    per_page = 16
    total_pages = math.ceil(len(videos) / per_page)
    if page > total_pages and total_pages > 0: page = total_pages
    page_videos = videos[(page-1)*per_page : page*per_page]

    analysis = {}
    if videos:
        total_dur, total_view, total_like, total_com = (sum(v.get(k, 0) for v in videos) for k in ["duration_sec", "views", "likes", "comments"])
        if len(videos) > 1:
             first_video_date = videos[0]['published']
             last_video_date = videos[-1]['published']
             weeks = max((first_video_date - last_video_date).days / 7, 1)
             analysis['uploads_per_week'] = round(len(videos)/weeks, 1)
        else:
             analysis['uploads_per_week'] = len(videos)

        analysis.update({
            "avg_duration": format_seconds(total_dur / len(videos)) if videos else "N/A",
            "likes_per_1000_views": round(total_like/total_view*1000, 1) if total_view else 0,
            "comments_per_1000_views": round(total_com/total_view*1000, 1) if total_view else 0,
            "top_5_videos": sorted(videos, key=lambda x: x.get("views", 0), reverse=True)[:5]
        })

    return render_template('analyze.html', stats=stats, videos=page_videos,
                           analysis=analysis, original_url=url, sort_by=sort_by,
                           total_pages=total_pages, current_page=page, CPM_USD=CPM_USD)

@app.route('/get-caption/<video_id>')
def get_caption(video_id):
    try:
        yt = YouTube(f"https://youtu.be/{video_id}", use_po_token=True)
        cap = yt.captions.get_by_language_code('ko') or yt.captions.get_by_language_code('en') or yt.captions.get_by_language_code('a.en')
        if not cap: return jsonify({'error':'자막이 없습니다.'}), 404
        return jsonify({'title': yt.title, 'srt_content': cap.generate_srt_captions()})
    except Exception:
        logger.exception(f"자막 로딩 오류(video_id={video_id})")
        return jsonify({'error':'자막 로딩 실패'}), 500

@app.route('/get-caption-ai/<video_id>')
def get_caption_ai(video_id):
    if not WHISPER_MODEL: return jsonify({'error':'AI 자막 비활성화'}), 503
    try:
        yt = YouTube(f"https://youtu.be/{video_id}", use_po_token=True)
        stream = yt.streams.filter(only_audio=True, file_extension="mp4").first()
        if not stream: return jsonify({'error':'오디오 스트림 없음'}), 404
        with tempfile.TemporaryDirectory() as td:
            path = stream.download(output_path=td)
            segments, _ = WHISPER_MODEL.transcribe(path, beam_size=5, language="ko")
            srt_list = [f"{i+1}\n{int(s.start//3600):02}:{int(s.start%3600//60):02}:{int(s.start%60):02},{int(s.start*1000%1000):03} --> {int(s.end//3600):02}:{int(s.end%3600//60):02}:{int(s.end%60):02},{int(s.end*1000%1000):03}\n{s.text.strip()}" for i, s in enumerate(segments)]
        return jsonify({'title':f"[AI] {yt.title}", 'srt_content':"\n\n".join(srt_list)})
    except Exception:
        logger.exception(f"AI 자막 오류({video_id})")
        return jsonify({'error':'AI 자막 실패'}), 500

@app.route('/download-video/<video_id>')
def download_video(video_id):
    try:
        yt = YouTube(f"https://youtu.be/{video_id}", use_po_token=True)
        stream = yt.streams.get_highest_resolution()
        title_safe = ''.join(c for c in yt.title if c.isalnum() or c in (' ','-')).strip()
        buf = io.BytesIO()
        stream.stream_to_buffer(buf)
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name=f"{title_safe}.mp4", mimetype="video/mp4")
    except Exception:
        logger.exception(f"비디오 다운로드 오류({video_id})")
        abort(500, description="다운로드 실패")

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5001, debug=DEBUG)
