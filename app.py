# app.py

import os
import io
import tempfile
import time
import re
import math
from datetime import datetime, timezone
from functools import lru_cache
from urllib.error import HTTPError

from flask import (
    Flask, request, send_file, render_template,
    jsonify, redirect, abort
)
from flask.logging import create_logger
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError as GoogleHttpError
from pytubefix import YouTube
from faster_whisper import WhisperModel

# --- Flask 앱 설정 ---
app = Flask(__name__)
logger = create_logger(app)

# --- 환경 변수 및 설정 ---
API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    raise RuntimeError("YOUTUBE_API_KEY 환경 변수 필요")

CPM_USD = float(os.getenv("CPM_USD", 1.5))
DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"

# --- Whisper 모델 Lazy-Load 및 영구 저장소 사용 ---
WHISPER_MODEL = None
def get_whisper_model():
    global WHISPER_MODEL
    if WHISPER_MODEL is None:
        logger.info("Whisper 모델을 로드하는 중입니다...")
        try:
            # Render의 영구 디스크 경로를 사용
            cache_directory = "/var/data/whisper_cache"
            if not os.path.exists(cache_directory):
                os.makedirs(cache_directory)
            WHISPER_MODEL = WhisperModel('base', device='cpu', compute_type="int8", download_root=cache_directory)
            logger.info("Whisper 모델 로드 완료.")
        except Exception as e:
            logger.error(f"Whisper 모델 로드 실패: {e}")
            WHISPER_MODEL = False # 실패했음을 명확히 표시
    return WHISPER_MODEL

# --- 유틸리티 함수들 ---
def parse_iso_date(iso_str):
    try:
        return datetime.fromisoformat(iso_str.rstrip("Z")).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)

def parse_duration(duration_str):
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str or "")
    if not m: return 0
    h, mm, ss = (int(x) if x else 0 for x in m.groups())
    return h*3600 + mm*60 + ss

def format_seconds(sec):
    sec = int(sec or 0)
    if not sec: return "N/A"
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    parts = []
    if h: parts.append(f"{h}시간")
    if m: parts.append(f"{m}분")
    if s or not parts: parts.append(f"{s}초")
    return " ".join(parts)

@lru_cache()
def get_youtube_client():
    return build("youtube", "v3", developerKey=API_KEY)

def extract_channel_id(url):
    yt = get_youtube_client()
    try:
        if "channel/" in url:
            return url.split("channel/")[1].split("/")[0]
        if "user/" in url:
            user = url.split("user/")[1].split("/")[0]
            return yt.channels().list(part="id", forUsername=user).execute()["items"][0]["id"]
        if "/@" in url:
            handle = url.split("/@")[1].split("/")[0]
            return yt.channels().list(part="id", forHandle=handle).execute()["items"][0]["id"]
    except Exception:
        logger.exception(f"채널 ID 추출 실패: {url}")
    return None

def fetch_videos(channel_id, max_v=200):
    yt = get_youtube_client()
    try:
        ch = yt.channels().list(part="contentDetails", id=channel_id).execute()
        uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        vids, token = [], None
        while len(vids) < max_v:
            r = yt.playlistItems().list(part="snippet", playlistId=uploads, maxResults=50, pageToken=token).execute()
            vids += [i["snippet"]["resourceId"]["videoId"] for i in r.get("items", [])]
            token = r.get("nextPageToken")
            if not token: break
        
        out = []
        for i in range(0, len(vids), 50):
            batch = vids[i:i+50]
            r2 = yt.videos().list(part="snippet,statistics,contentDetails", id=",".join(batch)).execute()
            for v in r2.get("items", []):
                sn, st, cd = v["snippet"], v["statistics"], v.get("contentDetails",{})
                out.append({
                    "id": v["id"], "title": sn.get("title",""),
                    "thumb": sn.get("thumbnails",{}).get("medium",{}).get("url",""),
                    "url": f"https://youtu.be/{v['id']}",
                    "published": parse_iso_date(sn.get("publishedAt","")),
                    "views": int(st.get("viewCount",0)), "likes": int(st.get("likeCount",0)),
                    "comments": int(st.get("commentCount",0)),
                    "duration_sec": parse_duration(cd.get("duration",""))
                })
        return out
    except Exception:
        logger.exception("동영상 목록 로딩 중 오류")
        return []

# --- 라우트 정의 ---
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/analyze')
def analyze():
    url = request.args.get('url','').strip()
    sort_by = request.args.get('sortBy', 'published')
    try: page = max(int(request.args.get('page',1)),1)
    except: page = 1
    
    if not url: return render_template('index.html', error="URL을 입력해주세요.")
    cid = extract_channel_id(url)
    if not cid: return render_template('index.html', error="유효하지 않은 채널 URL입니다.")

    try:
        info = get_youtube_client().channels().list(part='snippet,statistics',id=cid).execute()['items'][0]
    except GoogleHttpError as e:
        if e.resp.status == 429:
            return render_template('index.html', error="API 사용량을 초과했습니다. 잠시 후 다시 시도해주세요.")
        return render_template('index.html', error=f"채널 정보 로딩 실패: {e}")
    except Exception:
        logger.exception('채널 정보 실패')
        return render_template('index.html', error='채널 정보 로딩 중 오류')

    stats = {
        'title':info['snippet']['title'], 'description':info['snippet']['description'],
        'subscribers':int(info['statistics'].get('subscriberCount',0)), 
        'total_views':int(info['statistics'].get('viewCount',0)),
        'created_date':parse_iso_date(info['snippet']['publishedAt']).date(), 
        'video_count':int(info['statistics'].get('videoCount',0)),
        'profile_image':info['snippet']['thumbnails']['high']['url']
    }
    
    videos = fetch_videos(cid)
    if videos:
        videos.sort(key=lambda x: x.get(sort_by, 0), reverse=True)

    per = 16
    total_pages = math.ceil(len(videos) / per) or 1
    if page > total_pages: page = total_pages
    page_videos = videos[(page-1)*per : page*per]

    analysis={}
    if videos:
        td, tv, tl, tc = (sum(v.get(k, 0) for v in videos) for k in ["duration_sec", "views", "likes", "comments"])
        
        sorted_by_date = sorted(videos, key=lambda x: x['published'])
        weeks = max((sorted_by_date[-1]["published"] - sorted_by_date[0]["published"]).days / 7, 1) if len(videos) > 1 else 1

        analysis = {
            "uploads_per_week": round(len(videos)/weeks, 1),
            "avg_duration": format_seconds(td/len(videos)) if videos else 'N/A',
            "likes_per_1000_views": round(tl/tv*1000, 1) if tv else 0,
            "comments_per_1000_views": round(tc/tv*1000, 1) if tv else 0,
            "top_5_videos": sorted(videos, key=lambda x: x["views"], reverse=True)[:5]
        }

    return render_template('analyze.html', stats=stats, videos=page_videos,
                           analysis=analysis, original_url=url, sort_by=sort_by,
                           total_pages=total_pages, current_page=page, CPM_USD=CPM_USD)

# --- 자막 & 다운로드 엔드포인트 ---
def get_yt_with_retry(video_id, retries=3, delay=1):
    for i in range(retries):
        try:
            # 봇 감지 회피 옵션을 항상 True로 설정
            return YouTube(f"https://youtu.be/{video_id}", use_po_token=True)
        except HTTPError as e:
            if e.code == 429 and i < retries - 1:
                logger.warning(f"429 에러, {delay}초 후 재시도... ({i+1}/{retries})")
                time.sleep(delay)
            else:
                raise
    return None

@app.route('/get-caption/<video_id>')
def get_caption(video_id):
    try:
        yt = get_yt_with_retry(video_id)
        if not yt: return jsonify({'error':'영상 정보를 가져올 수 없습니다.'}), 500
        
        cap = yt.captions.get_by_language_code('ko') or yt.captions.get_by_language_code('en') or yt.captions.get_by_language_code('a.en')
        if not cap: return jsonify({'error':'자막이 없습니다.'}), 404
        return jsonify({'title': yt.title, 'srt_content': cap.generate_srt_captions()})
    except Exception:
        logger.exception(f"자막 로딩 오류(video_id={video_id})")
        return jsonify({'error':'자막 로딩 실패'}), 500

@app.route('/get-caption-ai/<video_id>')
def get_caption_ai(video_id):
    model = get_whisper_model()
    if model is False: return jsonify({'error':'AI 자막 기능이 현재 비활성화 상태입니다.'}), 503

    try:
        yt = get_yt_with_retry(video_id)
        if not yt: return jsonify({'error':'영상 정보를 가져올 수 없습니다.'}), 500

        stream = yt.streams.filter(only_audio=True, file_extension="mp4").first()
        if not stream: return jsonify({'error':'오디오 스트림 없음'}),404
        
        with tempfile.TemporaryDirectory() as td:
            path = stream.download(output_path=td)
            segs, _ = model.transcribe(path, beam_size=5, language="ko")
            lines = [f"{i+1}\n{int(s.start//3600):02}:{int(s.start%3600//60):02}:{int(s.start%60):02},{int(s.start*1000%1000):03} --> {int(s.end//3600):02}:{int(s.end%3600//60):02}:{int(s.end%60):02},{int(s.end*1000%1000):03}\n{s.text.strip()}" for i, s in enumerate(segs)]
        return jsonify({'title':f"[AI] {yt.title}", 'srt_content':"\n\n".join(lines)})
    except Exception:
        logger.exception(f"AI 자막 오류({video_id})")
        return jsonify({'error':'AI 자막 실패'}), 500

@app.route('/download-video/<video_id>')
def download_video(video_id):
    try:
        yt = get_yt_with_retry(video_id)
        if not yt: abort(500, description="다운로드 실패: 영상 정보 로딩 불가")

        stream = yt.streams.get_highest_resolution()
        title_safe = ''.join(c for c in yt.title if c.isalnum() or c in (' ','-')).strip()
        buf = io.BytesIO()
        stream.stream_to_buffer(buf)
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name=f"{title_safe}.mp4", mimetype="video/mp4")
    except Exception:
        logger.exception(f"비디오 다운로드 오류({video_id})")
        return redirect(f"https://youtu.be/{video_id}")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=DEBUG)
