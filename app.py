import os
import io
import tempfile
import time
import re
import math
from datetime import datetime, timezone
from functools import lru_cache
from urllib.error import HTTPError

from flask import Flask, request, send_file, render_template, abort, jsonify, redirect
from flask.logging import create_logger
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pytubefix import YouTube
from faster_whisper import WhisperModel

app = Flask(__name__)
logger = create_logger(app)

API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    raise RuntimeError("YOUTUBE_API_KEY 필요")
CPM_USD = float(os.getenv("CPM_USD", 1.5))
DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"

# Whisper
try:
    WHISPER_MODEL = WhisperModel('base', device='cpu', compute_type="int8")
except:
    WHISPER_MODEL = None

def parse_iso_date(s):
    try:
        return datetime.fromisoformat(s.rstrip("Z")).replace(tzinfo=timezone.utc)
    except:
        return datetime.now(timezone.utc)

def parse_duration(d):
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', d or "")
    if not m:
        return 0
    h, mm, ss = (int(x) if x else 0 for x in m.groups())
    return h*3600 + mm*60 + ss

def format_seconds(sec):
    sec = int(sec or 0)
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
            h = url.split("/@")[1].split("/")[0]
            return yt.channels().list(part="id", forHandle=h).execute()["items"][0]["id"]
    except:
        pass
    return None

def fetch_videos(cid, max_v=200):
    yt = get_youtube_client()
    try:
        pl = yt.channels().list(part="contentDetails", id=cid).execute()
        uploads = pl["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        vids, token = [], None
        while len(vids)<max_v:
            r = yt.playlistItems().list(part="snippet", playlistId=uploads, maxResults=50, pageToken=token).execute()
            vids += [i["snippet"]["resourceId"]["videoId"] for i in r["items"]]
            token = r.get("nextPageToken")
            if not token: break

        out = []
        for i in range(0,len(vids),50):
            batch = vids[i:i+50]
            r2 = yt.videos().list(part="snippet,statistics,contentDetails", id=",".join(batch)).execute()
            for v in r2["items"]:
                sn, st, cd = v["snippet"], v["statistics"], v.get("contentDetails",{})
                out.append({
                    "id": v["id"],
                    "title": sn.get("title",""),
                    "thumb": sn.get("thumbnails",{}).get("medium",{}).get("url",""),
                    "url": f"https://youtu.be/{v['id']}",
                    "published": parse_iso_date(sn.get("publishedAt","")),
                    "views": int(st.get("viewCount",0)),
                    "likes": int(st.get("likeCount",0)),
                    "comments": int(st.get("commentCount",0)),
                    "duration_sec": parse_duration(cd.get("duration",""))
                })
        return out
    except:
        return []

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/analyze')
def analyze():
    url = request.args.get('url','').strip()
    if not url:
        return render_template('index.html', error="URL을 입력해주세요.")
    cid = extract_channel_id(url)
    if not cid:
        return render_template('index.html', error="올바른 채널 URL이 아닙니다.")
    # stats
    try:
        info = get_youtube_client().channels().list(part="snippet,statistics",id=cid).execute()["items"][0]
    except HttpError as e:
        if e.resp.status==429:
            return render_template('index.html', error="API 제한 초과, 나중에 시도하세요.")
        return render_template('index.html', error="채널 정보를 가져올 수 없습니다.")
    except:
        return render_template('index.html', error="채널 정보를 가져오는 중 오류")

    stats = {
        "title": info["snippet"]["title"],
        "description": info["snippet"]["description"],
        "subscribers": int(info["statistics"].get("subscriberCount",0)),
        "total_views": int(info["statistics"].get("viewCount",0)),
        "created_date": parse_iso_date(info["snippet"]["publishedAt"]).date(),
        "video_count": int(info["statistics"].get("videoCount",0)),
        "profile_image": info["snippet"]["thumbnails"]["high"]["url"]
    }

    videos = fetch_videos(cid)
    # 정렬, 페이징
    sort_by = request.args.get('sortBy','published')
    videos.sort(key=lambda x: x.get(sort_by, x["published"]), reverse=True)
    p = max(int(request.args.get('page',1)),1)
    per = 16
    tp = math.ceil(len(videos)/per) or 1
    if p>tp: p=tp
    page_videos = videos[(p-1)*per : p*per]

    # 분석
    analysis={}
    if videos:
        td = sum(v["duration_sec"] for v in videos)
        tv = sum(v["views"] for v in videos)
        tl = sum(v["likes"] for v in videos)
        tc = sum(v["comments"] for v in videos)
        weeks = max((videos[0]["published"]-videos[-1]["published"]).days/7,1)
        analysis = {
            "uploads_per_week": round(len(videos)/weeks,1),
            "avg_duration": format_seconds(td/len(videos)),
            "likes_per_1000_views": round(tl/tv*1000,1) if tv else 0,
            "comments_per_1000_views": round(tc/tv*1000,1) if tv else 0,
            "top_5_videos": sorted(videos, key=lambda x: x["views"], reverse=True)[:5]
        }

    return render_template('analyze.html',
        stats=stats,
        videos=page_videos,
        analysis=analysis,
        original_url=url,
        total_pages=tp,
        current_page=p,
        CPM_USD=CPM_USD
    )

@app.route('/get-caption/<video_id>')
def get_caption(video_id):
    for _ in range(3):
        try:
            yt = YouTube(f"https://youtu.be/{video_id}", use_po_token=True)
            cap = yt.captions.get_by_language_code('ko') or yt.captions.get_by_language_code('en') or yt.captions.get_by_language_code('a.en')
            if not cap:
                return jsonify({'title':'','srt_content':''})
            return jsonify({'title':yt.title,'srt_content':cap.generate_srt_captions()})
        except HTTPError as e:
            if e.code==429:
                time.sleep(1)
                continue
        except:
            break
    # 반복 실패 시 빈 결과
    return jsonify({'title':'','srt_content':''})

@app.route('/get-caption-ai/<video_id>')
def get_caption_ai(video_id):
    if not WHISPER_MODEL:
        return jsonify({'title':'','srt_content':''})
    for _ in range(2):
        try:
            yt = YouTube(f"https://youtu.be/{video_id}", use_po_token=True)
            stream = yt.streams.filter(only_audio=True, file_extension="mp4").first()
            if not stream:
                break
            with tempfile.TemporaryDirectory() as td:
                path = stream.download(output_path=td)
                segs,_ = WHISPER_MODEL.transcribe(path, beam_size=5, language="ko")
                lines=[]
                for i,s in enumerate(segs):
                    st = f"{int(s.start//3600):02}:{int(s.start%3600//60):02}:{int(s.start%60):02},{int(s.start*1000%1000):03}"
                    en = f"{int(s.end//3600):02}:{int(s.end%3600//60):02}:{int(s.end%60):02},{int(s.end*1000%1000):03}"
                    lines.append(f"{i+1}\n{st} --> {en}\n{s.text.strip()}")
                return jsonify({'title':f"[AI] {yt.title}",'srt_content':"\n\n".join(lines)})
        except HTTPError as e:
            if e.code==429:
                time.sleep(1)
                continue
        except:
            break
    return jsonify({'title':'','srt_content':''})

@app.route('/download-video/<video_id>')
def download_video(video_id):
    # 다운로드 실패해도 유튜브로 리다이렉트
    try:
        yt = YouTube(f"https://youtu.be/{video_id}", use_po_token=True)
        stream = yt.streams.get_highest_resolution()
        buf=io.BytesIO()
        for i in range(3):
            try:
                stream.stream_to_buffer(buf)
                break
            except HTTPError as e:
                if e.code==429:
                    time.sleep(1)
                    continue
                raise
        buf.seek(0)
        name=''.join(c for c in yt.title if c.isalnum() or c in(' ','-')).strip()
        return send_file(buf, as_attachment=True, download_name=f"{name}.mp4", mimetype="video/mp4")
    except:
        return redirect(f"https://youtu.be/{video_id}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=DEBUG)