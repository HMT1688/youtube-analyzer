# app.py

import os
import io
import tempfile
import traceback 
import re
from datetime import datetime, timezone, timedelta
from flask import Flask, request, send_file, render_template, abort
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pytubefix import YouTube
from faster_whisper import WhisperModel
import math

# --- 설정 (이전과 동일) ---
API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    raise ValueError("YOUTUBE_API_KEY 환경 변수가 설정되지 않았습니다!")

YT_SERVICE = "youtube"
YT_VERSION = "v3"
CPM_USD = 1.5

print("Whisper 모델을 로드하는 중입니다. 잠시 기다려주세요...")
try:
    WHISPER_MODEL = WhisperModel('base', device='cpu', compute_type="int8")
    print("Whisper 모델 로드 완료.")
except Exception as e:
    print(f"Whisper 모델 로드 실패: {e}")
    WHISPER_MODEL = None

app = Flask(__name__)

# --- 헬퍼 함수들 ---

def parse_iso_date(iso_str):
    s = iso_str.rstrip("Z")
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.fromisoformat(s.split(".")[0]).replace(tzinfo=timezone.utc)

# ✨ 버그를 수정한 함수 ✨
def parse_duration(duration_str):
    """ISO 8601 기간 형식(PTxMxS)을 초 단위로 변환합니다. 비어있는 값에 대한 예외 처리를 추가합니다."""
    if not duration_str: # 영상 길이 정보가 비어있으면 0을 반환
        return 0
        
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
    if not match: # 형식이 맞지 않는 경우에도 0을 반환
        return 0
        
    groups = match.groups()
    hours = int(groups[0]) if groups[0] else 0
    minutes = int(groups[1]) if groups[1] else 0
    seconds = int(groups[2]) if groups[2] else 0
    return hours * 3600 + minutes * 60 + seconds

def format_seconds(seconds):
    if seconds is None or seconds == 0: return "N/A"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0: return f"{h}시간 {m}분 {s}초"
    if m > 0: return f"{m}분 {s}초"
    return f"{s}초"


def extract_channel_id_from_url(url):
    try:
        youtube = build(YT_SERVICE, YT_VERSION, developerKey=API_KEY)
        if "channel/" in url:
            return url.split("channel/")[1].split("/")[0]
        if "user/" in url:
            username = url.split("user/")[1].split("/")[0]
            request = youtube.channels().list(part="id", forUsername=username)
            return request.execute()["items"][0]["id"]
        if "/@" in url:
            handle = url.split("/@")[1].split("/")[0]
            request = youtube.channels().list(part="id", forHandle=handle)
            return request.execute()["items"][0]["id"]
    except (HttpError, IndexError, KeyError) as e:
        print(f"채널 ID 추출 오류: {e}")
        return None
    return None

def fetch_all_videos(channel_id, max_videos=200):
    try:
        youtube = build(YT_SERVICE, YT_VERSION, developerKey=API_KEY)
        channel_response = youtube.channels().list(part="contentDetails", id=channel_id).execute()
        uploads_playlist_id = channel_response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

        all_video_ids = []
        next_page_token = None

        while len(all_video_ids) < max_videos:
            playlist_request = youtube.playlistItems().list(
                part="snippet",
                playlistId=uploads_playlist_id,
                maxResults=50,
                pageToken=next_page_token
            )
            playlist_response = playlist_request.execute()
            
            all_video_ids.extend([item["snippet"]["resourceId"]["videoId"] for item in playlist_response.get("items", [])])
            
            next_page_token = playlist_response.get('nextPageToken')
            if not next_page_token:
                break
        
        all_videos = []
        for i in range(0, len(all_video_ids), 50):
            batch_ids = all_video_ids[i:i+50]
            video_response = youtube.videos().list(
                part="snippet,statistics,contentDetails", id=",".join(batch_ids)
            ).execute()
            
            for v in video_response.get("items", []):
                sn, st, cd = v.get("snippet", {}), v.get("statistics", {}), v.get("contentDetails", {})
                all_videos.append({
                    "id": v["id"], "title": sn.get("title", "제목 없음"),
                    "thumb": sn.get("thumbnails", {}).get("medium", {}).get("url"),
                    "url": f"https://youtu.be/{v['id']}", "published": parse_iso_date(sn["publishedAt"]),
                    "views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                    "comments": int(st.get("commentCount", 0)), 
                    "duration_sec": parse_duration(cd.get("duration", "PT0S"))
                })
        return sorted(all_videos, key=lambda x: x['published'], reverse=True)
    except HttpError as e:
        print(f"전체 동영상 정보 수집 오류: {e}")
        return None

# --- Flask 라우트 ---
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/analyze')
def analyze():
    url = request.args.get('url', '').strip()
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1

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
        video_count = int(st.get("videoCount", 0))
        
        stats = {
            "title": sn.get("title", "이름 없음"), "description": sn.get("description", ""),
            "subscribers": int(st.get("subscriberCount", 0)), "total_views": total_views,
            "created_date": created_at.date(), "video_count": video_count,
            "profile_image": sn.get("thumbnails", {}).get("high", {}).get("url")
        }

        videos = fetch_all_videos(channel_id)
        if videos is None: return render_template('index.html', error="채널의 동영상 목록을 가져오는 데 실패했습니다.")

        analysis = {}
        if videos:
            total_duration = sum(v.get('duration_sec', 0) for v in videos)
            total_vid_views = sum(v.get('views', 0) for v in videos)
            total_likes = sum(v.get('likes', 0) for v in videos)
            total_comments = sum(v.get('comments', 0) for v in videos)

            analysis['avg_duration'] = format_seconds(total_duration / len(videos)) if len(videos) > 0 else "N/A"
            
            time_diff = videos[0]['published'] - videos[-1]['published']
            weeks = max(time_diff.days / 7, 1)
            analysis['uploads_per_week'] = round(len(videos) / weeks, 1)

            analysis['likes_per_1000_views'] = round((total_likes / total_vid_views) * 1000, 1) if total_vid_views > 0 else 0
            analysis['comments_per_1000_views'] = round((total_comments / total_vid_views) * 1000, 1) if total_vid_views > 0 else 0
            
            analysis['top_5_videos'] = sorted(videos, key=lambda x: x.get('views', 0), reverse=True)[:5]

        videos_per_page = 16
        total_pages = math.ceil(len(videos) / videos_per_page)
        start_index = (page - 1) * videos_per_page
        end_index = start_index + videos_per_page
        paginated_videos = videos[start_index:end_index]

        return render_template('analyze.html', stats=stats, videos=paginated_videos, analysis=analysis,
                               original_url=url, total_pages=total_pages, current_page=page, CPM_USD=CPM_USD)
    except Exception as e:
        print("--- 채널 분석 오류 ---")
        traceback.print_exc()
        return render_template('index.html', error=f"채널 분석 중 오류가 발생했습니다: {e}")

# --- 자막/영상 다운로드 라우트 (이전과 동일) ---
@app.route('/caption/<video_id>')
def download_caption(video_id):
    pass

@app.route('/caption-ai/<video_id>')
def caption_ai(video_id):
    pass

@app.route('/download-video/<video_id>')
def download_video(video_id):
    pass

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=5001)
