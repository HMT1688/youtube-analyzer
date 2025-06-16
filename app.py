# app.py

import os
import io
import tempfile
import traceback 
from datetime import datetime, timezone
from flask import Flask, request, send_file, render_template, abort
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pytubefix import YouTube
from faster_whisper import WhisperModel

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

# ✨ 페이지네이션을 위해 수정된 함수 ✨
def fetch_channel_videos(channel_id, page_token=None, max_results=12):
    """지정된 페이지의 동영상 목록과 다음/이전 페이지 토큰을 가져옵니다."""
    try:
        youtube = build(YT_SERVICE, YT_VERSION, developerKey=API_KEY)
        channel_response = youtube.channels().list(part="contentDetails", id=channel_id).execute()
        uploads_playlist_id = channel_response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

        playlist_request = youtube.playlistItems().list(
            part="snippet",
            playlistId=uploads_playlist_id,
            maxResults=max_results,
            pageToken=page_token  # 페이지 토큰 사용
        )
        playlist_response = playlist_request.execute()

        video_ids = [item["snippet"]["resourceId"]["videoId"] for item in playlist_response.get("items", [])]
        if not video_ids:
            return {'videos': [], 'next_page': None, 'prev_page': None}

        video_response = youtube.videos().list(
            part="snippet,statistics,contentDetails", id=",".join(video_ids)
        ).execute()
        
        videos = []
        for v in video_response.get("items", []):
            sn, st, cd = v.get("snippet", {}), v.get("statistics", {}), v.get("contentDetails", {})
            videos.append({
                "id": v["id"], "title": sn.get("title", "제목 없음"),
                "thumb": sn.get("thumbnails", {}).get("medium", {}).get("url"),
                "url": f"https://youtu.be/{v['id']}", "published": parse_iso_date(sn["publishedAt"]),
                "views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                "comments": int(st.get("commentCount", 0)), "duration": cd.get("duration", "PT0S")
            })
        
        # 다음 페이지와 이전 페이지 토큰을 함께 반환
        return {
            'videos': videos,
            'next_page': playlist_response.get('nextPageToken'),
            'prev_page': playlist_response.get('prevPageToken')
        }
    except HttpError as e:
        print(f"동영상 정보 수집 오류: {e}")
        return None

# --- Flask 라우트 ---

@app.route('/')
def home():
    return render_template('index.html')

# ✨ 페이지네이션을 위해 수정된 라우트 ✨
@app.route('/analyze')
def analyze():
    url = request.args.get('url', '').strip()
    sort_by = request.args.get('sortBy', 'date')
    page_token = request.args.get('pageToken') # URL에서 페이지 토큰 가져오기

    if not url: return render_template('index.html', error="채널 URL을 입력해주세요.")
    
    channel_id = extract_channel_id_from_url(url)
    if not channel_id: return render_template('index.html', error="유효하지 않은 채널 URL이거나 채널을 찾을 수 없습니다.")

    try:
        youtube = build(YT_SERVICE, YT_VERSION, developerKey=API_KEY)
        info_res = youtube.channels().list(part="snippet,statistics", id=channel_id).execute()
        if not info_res.get("items"): return render_template('index.html', error="채널 정보를 가져올 수 없습니다.")
        
        info = info_res["items"][0]
        # ... (이전 stats 계산 부분은 동일) ...
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

        # 페이지 토큰을 사용해 해당 페이지의 영상 데이터 가져오기
        video_data = fetch_channel_videos(channel_id, page_token=page_token)
        if video_data is None: return render_template('index.html', error="채널의 동영상 목록을 가져오는 데 실패했습니다.")

        videos = video_data['videos']
        next_page_token = video_data['next_page']
        prev_page_token = video_data['prev_page']
        
        sort_key = sort_by if sort_by in ['views', 'likes'] else 'published'
        videos.sort(key=lambda x: x.get(sort_key, 0), reverse=True)

        return render_template(
            'analyze.html', 
            stats=stats, 
            videos=videos, 
            original_url=url, 
            sort_by=sort_by, 
            CPM_USD=CPM_USD,
            next_page=next_page_token, # 다음 페이지 토큰 전달
            prev_page=prev_page_token  # 이전 페이지 토큰 전달
        )
    except Exception as e:
        print("--- 채널 분석 오류 ---")
        traceback.print_exc()
        return render_template('index.html', error=f"채널 분석 중 오류가 발생했습니다: {e}")


# --- 자막/영상 다운로드 라우트 (이전과 동일) ---
# ... (이하 모든 다운로드 관련 코드는 수정할 필요 없습니다) ...

@app.route('/caption/<video_id>')
def download_caption(video_id):
    try:
        yt = YouTube(f"https://youtu.be/{video_id}")
        caption = yt.captions.get_by_language_code('ko') or yt.captions.get_by_language_code('en') or yt.captions.get_by_language_code('a.en')
        if not caption:
            return "이 영상에는 다운로드할 수 있는 자막이 없습니다.", 404
        buffer = io.BytesIO(caption.generate_srt_captions().encode('utf-8'))
        return send_file(buffer, as_attachment=True, download_name=f"{video_id}.srt", mimetype="text/plain")
    except Exception as e:
        print(f"--- 자막 다운로드 오류 (ID: {video_id}) ---")
        traceback.print_exc()
        return f"자막 다운로드 중 오류가 발생했습니다: {e}", 500

@app.route('/caption-ai/<video_id>')
def caption_ai(video_id):
    if not WHISPER_MODEL:
        return "AI 자막 기능이 현재 비활성화 상태입니다.", 503
    try:
        yt = YouTube(f"https://youtu.be/{video_id}")
        stream = yt.streams.filter(only_audio=True, file_extension="mp4").first()
        if not stream:
            return "오디오 스트림을 찾을 수 없습니다.", 404

        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = stream.download(output_path=temp_dir)
            segments, _ = WHISPER_MODEL.transcribe(audio_path, beam_size=5, language="ko")
            srt_content = []
            for i, seg in enumerate(segments):
                start = f"{int(seg.start//3600):02}:{int(seg.start%3600//60):02}:{int(seg.start%60):02},{int(seg.start*1000%1000):03}"
                end = f"{int(seg.end//3600):02}:{int(seg.end%3600//60):02}:{int(seg.end%60):02},{int(seg.end*1000%1000):03}"
                srt_content.append(f"{i+1}\n{start} --> {end}\n{seg.text.strip()}\n")
            buffer = io.BytesIO("\n".join(srt_content).encode('utf-8'))
            return send_file(buffer, as_attachment=True, download_name=f"{video_id}_ai.srt", mimetype="text/plain")
    except Exception as e:
        print(f"--- AI 자막 생성 오류 (ID: {video_id}) ---")
        traceback.print_exc()
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

        return send_file(
            buffer,
            as_attachment=True,
            download_name=filename,
            mimetype="video/mp4"
        )
    except Exception as e:
        print(f"--- 영상 다운로드 오류 (ID: {video_id}) ---")
        traceback.print_exc()
        return f"영상 다운로드 중 오류가 발생했습니다: {e}", 500

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=5001)
