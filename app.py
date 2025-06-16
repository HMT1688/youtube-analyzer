# app.py

import os
import io
import tempfile
import traceback 
import webbrowser # << 자동으로 브라우저를 열기 위해 추가
import threading  # << 서버 시작 후 브라우저를 열기 위해 추가
from datetime import datetime, timezone
from flask import Flask, request, send_file, render_template, abort
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pytubefix import YouTube
from faster_whisper import WhisperModel

# ─ 설정 ───────────────────────────────────────────────────

# 1. API 키 설정
API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    raise ValueError("YOUTUBE_API_KEY 환경 변수가 설정되지 않았습니다!")


YT_SERVICE = "youtube"
YT_VERSION = "v3"
CPM_USD = 1.5

# 2. Whisper 모델 로드
print("Whisper 모델을 로드하는 중입니다. 잠시 기다려주세요...")
try:
    WHISPER_MODEL = WhisperModel('base', device='cpu', compute_type="int8")
    print("Whisper 모델 로드 완료.")
except Exception as e:
    print(f"Whisper 모델 로드 실패: {e}")
    WHISPER_MODEL = None

app = Flask(__name__)

# ─ 헬퍼 함수들 (이전과 동일) ───────────────────────────────

def parse_iso_date(iso_str):
    s = iso_str.rstrip("Z")
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.fromisoformat(s.split(".")[0]).replace(tzinfo=timezone.utc)

def extract_channel_id_from_url(url):
    try:
        youtube = build(YT_SERVICE, YT_VERSION, developerKey=API_KEY)
        if "channel/" in url:
            return url.split("channel/")[1].split("/")[0]
        if "user/" in url:
            username = url.split("user/")[1].split("/")[0]
            req = youtube.channels().list(part="id", forUsername=username)
            return req.execute()["items"][0]["id"]
        if "/@" in url:
            handle = url.split("/@")[1].split("/")[0]
            req = youtube.channels().list(part="id", forHandle=handle)
            return req.execute()["items"][0]["id"]
    except (HttpError, IndexError, KeyError) as e:
        print(f"채널 ID 추출 오류: {e}")
        return None
    return None

def fetch_channel_videos(channel_id, max_results=50):
    try:
        youtube = build(YT_SERVICE, YT_VERSION, developerKey=API_KEY)
        req = youtube.channels().list(part="contentDetails", id=channel_id)
        uploads_id = req.execute()["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        
        req = youtube.playlistItems().list(part="snippet", playlistId=uploads_id, maxResults=max_results)
        items = req.execute().get("items", [])
        
        video_ids = [i["snippet"]["resourceId"]["videoId"] for i in items]
        if not video_ids: return []
        
        req = youtube.videos().list(part="snippet,statistics,contentDetails", id=",".join(video_ids))
        vids = req.execute().get("items", [])
        
        videos = []
        for v in vids:
            sn, st, cd = v.get("snippet", {}), v.get("statistics", {}), v.get("contentDetails", {})
            videos.append({
                "id": v["id"], "title": sn.get("title", "제목 없음"),
                "thumb": sn.get("thumbnails", {}).get("medium", {}).get("url"),
                "url": f"https://youtu.be/{v['id']}", "published": parse_iso_date(sn["publishedAt"]),
                "views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                "comments": int(st.get("commentCount", 0)), "duration": cd.get("duration", "PT0S")
            })
        return videos
    except HttpError as e:
        print(f"동영상 정보 수집 오류: {e}")
        return None

# ─ Flask 라우트 (이전과 동일) ───────────────────────────

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/analyze')
def analyze():
    url = request.args.get('url', '').strip()
    sort_by = request.args.get('sortBy', 'published')
    if not url: return render_template('index.html', error="채널 URL을 입력해주세요.")
    
    channel_id = extract_channel_id_from_url(url)
    if not channel_id: return render_template('index.html', error="유효하지 않은 채널 URL이거나 채널을 찾을 수 없습니다.")

    try:
        youtube = build(YT_SERVICE, YT_VERSION, developerKey=API_KEY)
        info_res = youtube.channels().list(part="snippet,statistics", id=channel_id).execute()
        if not info_res.get("items"): return render_template('index.html', error="채널 정보를 가져올 수 없습니다.")
        
        info = info_res["items"][0]
        sn, st = info["snippet"], info["statistics"]
        created_at = parse_iso_date(sn["publishedAt"])
        days_since_creation = max((datetime.now(timezone.utc) - created_at).days, 1)
        total_views = int(st.get("viewCount", 0))
        
        stats = {
            "title": sn.get("title", "이름 없음"), "description": sn.get("description", ""),
            "subscribers": int(st.get("subscriberCount", 0)), "total_views": total_views,
            "created_date": created_at.date(), "avg_daily_views": total_views // days_since_creation,
            "monthly_views": (total_views // days_since_creation) * 30,
            "estimated_revenue": ((total_views // days_since_creation) * 30) / 1000 * CPM_USD,
            "profile_image": sn.get("thumbnails", {}).get("medium", {}).get("url")
        }

        videos = fetch_channel_videos(channel_id)
        if videos is None: return render_template('index.html', error="채널의 동영상 목록을 가져오는 데 실패했습니다.")
        
        if sort_by not in ['published', 'views', 'likes']: sort_by = 'published'
        videos.sort(key=lambda x: x.get(sort_by, 0), reverse=True)

        return render_template('analyze.html', stats=stats, videos=videos, original_url=url, sort_by=sort_by, CPM_USD=CPM_USD)
    except Exception as e:
        print(f"--- 채널 분석 오류 ---"); traceback.print_exc()
        return render_template('index.html', error=f"채널 분석 중 오류가 발생했습니다: {e}")

@app.route('/caption/<video_id>')
def download_caption(video_id):
    try:
        yt = YouTube(f"https://youtu.be/{video_id}")
        caption = yt.captions.get_by_language_code('ko') or yt.captions.get_by_language_code('en') or yt.captions.get_by_language_code('a.en')
        if not caption: return "이 영상에는 다운로드할 수 있는 자막이 없습니다.", 404
        buffer = io.BytesIO(caption.generate_srt_captions().encode('utf-8'))
        return send_file(buffer, as_attachment=True, download_name=f"{video_id}.srt", mimetype="text/plain")
    except Exception as e:
        print(f"--- 자막 다운로드 오류 ---"); traceback.print_exc()
        return f"자막 다운로드 중 오류가 발생했습니다: {e}", 500

@app.route('/caption-ai/<video_id>')
def caption_ai(video_id):
    if not WHISPER_MODEL: return "AI 자막 기능이 현재 비활성화 상태입니다.", 503
    try:
        yt = YouTube(f"https://youtu.be/{video_id}")
        stream = yt.streams.filter(only_audio=True, file_extension="mp4").first()
        if not stream: return "오디오 스트림을 찾을 수 없습니다.", 404
        with tempfile.TemporaryDirectory() as td:
            path = stream.download(output_path=td)
            segs, _ = WHISPER_MODEL.transcribe(path, beam_size=5, language="ko")
            srt = "".join([f"{i+1}\n{int(s.start//3600):02}:{int(s.start%3600//60):02}:{int(s.start%60):02},{int(s.start*1000%1000):03} --> {int(s.end//3600):02}:{int(s.end%3600//60):02}:{int(s.end%60):02},{int(s.end*1000%1000):03}\n{s.text.strip()}\n\n" for i, s in enumerate(segs)])
            buffer = io.BytesIO(srt.encode('utf-8'))
            return send_file(buffer, as_attachment=True, download_name=f"{video_id}_ai.srt", mimetype="text/plain")
    except Exception as e:
        print(f"--- AI 자막 생성 오류 ---"); traceback.print_exc()
        return f"AI 자막 생성 중 오류가 발생했습니다: {e}", 500

@app.route('/download-video/<video_id>')
def download_video(video_id):
    try:
        yt = YouTube(f"https://youtu.be/{video_id}")
        stream = yt.streams.get_highest_resolution()
        safe_title = "".join([c for c in yt.title if c.isalnum() or c in (' ', '-')]).rstrip()
        filename = f"{safe_title}.mp4"
        buffer = io.BytesIO()
        stream.stream_to_buffer(buffer)
        buffer.seek(0)
        return send_file(buffer, as_attachment=True, download_name=filename, mimetype="video/mp4")
    except Exception as e:
        print(f"--- 영상 다운로드 오류 ---"); traceback.print_exc()
        return f"영상 다운로드 중 오류가 발생했습니다: {e}", 500

# --- ✨ 자동으로 브라우저 열기 기능 추가 ✨ ---
def open_browser():
      webbrowser.open_new("http://127.0.0.1:5001")

if __name__ == "__main__":
    # 서버가 시작된 후 (1초 뒤)에 브라우저를 열도록 설정
    threading.Timer(1, open_browser).start()
    app.run(debug=True, host='0.0.0.0', port=5001)
