import os
import io
import tempfile
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

app = Flask(__name__)
logger = create_logger(app)

API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    logger.error("YOUTUBE_API_KEY 환경 변수가 설정되지 않았습니다!")
    raise RuntimeError("YOUTUBE_API_KEY 필요")
CPM_USD = float(os.getenv("CPM_USD", 1.5))
DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"

logger.info("Whisper 모델 로드 중...")
try:
    WHISPER_MODEL = WhisperModel('base', device='cpu', compute_type="int8")
    logger.info("Whisper 모델 로드 완료.")
except Exception as e:
    logger.error(f"Whisper 모델 로드 실패: {e}")
    WHISPER_MODEL = None


def parse_iso_date(iso_str):
    s = iso_str or ''
    try:
        return datetime.fromisoformat(s.rstrip('Z')).replace(tzinfo=timezone.utc)
    except:
        return datetime.now(timezone.utc)


def parse_duration(d):
    if not d: return 0
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', d)
    if not m: return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h*3600 + mi*60 + s


def format_seconds(sec):
    if not sec: return "N/A"
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h: parts.append(f"{h}시간")
    if m: parts.append(f"{m}분")
    if s or not parts: parts.append(f"{s}초")
    return ' '.join(parts)

@lru_cache(maxsize=1)
def get_youtube_client():
    return build("youtube", "v3", developerKey=API_KEY)


def extract_channel_id_from_url(url):
    yt = get_youtube_client()
    try:
        if 'channel/' in url:
            return url.split('channel/')[1].split('/')[0]
        if 'user/' in url:
            user = url.split('user/')[1].split('/')[0]
            return yt.channels().list(part='id', forUsername=user).execute()['items'][0]['id']
        if '/@' in url:
            handle = url.split('/@')[1].split('/')[0]
            return yt.channels().list(part='id', forHandle=handle).execute()['items'][0]['id']
    except:
        logger.exception("채널 ID 추출 실패")
    return None


def fetch_all_videos(channel_id, max_videos=200):
    yt = get_youtube_client()
    try:
        ch = yt.channels().list(part='contentDetails', id=channel_id).execute()
        uploads = ch['items'][0]['contentDetails']['relatedPlaylists']['uploads']
        ids, token = [], None
        while len(ids) < max_videos:
            pl = yt.playlistItems().list(part='snippet', playlistId=uploads, maxResults=50, pageToken=token).execute()
            ids += [i['snippet']['resourceId']['videoId'] for i in pl.get('items', [])]
            token = pl.get('nextPageToken')
            if not token: break
        vids = []
        for i in range(0, len(ids), 50):
            batch = ids[i:i+50]
            res = yt.videos().list(part='snippet,statistics,contentDetails', id=','.join(batch)).execute()
            for v in res.get('items', []):
                sn, st, cd = v['snippet'], v['statistics'], v.get('contentDetails', {})
                vids.append({
                    'id': v['id'], 'title': sn.get('title',''),
                    'thumb': sn.get('thumbnails',{}).get('medium',{}).get('url',''),
                    'url': f"https://youtu.be/{v['id']}",
                    'published': parse_iso_date(sn.get('publishedAt','')),
                    'views': int(st.get('viewCount',0)), 'likes': int(st.get('likeCount',0)),
                    'comments': int(st.get('commentCount',0)),
                    'duration_sec': parse_duration(cd.get('duration',''))
                })
        return vids
    except:
        logger.exception("동영상 가져오기 중 오류")
        return None

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/analyze')
def analyze():
    url = request.args.get('url','').strip()
    sort_by = request.args.get('sortBy','published')
    try:
        page = max(int(request.args.get('page',1)),1)
    except:
        page = 1
    if not url:
        return render_template('index.html', error='채널 URL을 입력해주세요.')
    cid = extract_channel_id_from_url(url)
    if not cid:
        return render_template('index.html', error='유효하지 않은 채널 URL입니다.')
    try:
        info = get_youtube_client().channels().list(part='snippet,statistics', id=cid).execute()['items'][0]
    except HttpError as e:
        if e.resp.status == 429:
            return render_template('index.html', error='API 호출 제한 초과, 잠시 후 다시 시도해주세요.')
        logger.exception('채널 정보 로딩 오류')
        return render_template('index.html', error='채널 정보 로딩 중 오류')
    stats = {
        'title': info['snippet'].get('title',''), 'description': info['snippet'].get('description',''),
        'subscribers': int(info['statistics'].get('subscriberCount',0)),
        'total_views': int(info['statistics'].get('viewCount',0)),
        'created_date': parse_iso_date(info['snippet'].get('publishedAt','')).date(),
        'video_count': int(info['statistics'].get('videoCount',0)),
        'profile_image': info['snippet'].get('thumbnails',{}).get('high',{}).get('url','')
    }
    videos = fetch_all_videos(cid)
    if videos is None:
        return render_template('index.html', error='동영상 목록 로딩 실패')
    videos.sort(key=lambda x: x.get(sort_by, x.get('published')), reverse=True)
    per = 16
    total_pages = math.ceil(len(videos)/per)
    if page > total_pages and total_pages>0:
        page = total_pages
    page_videos = videos[(page-1)*per : page*per]
    analysis = {}
    if videos:
        td = sum(v['duration_sec'] for v in videos)
        tv = sum(v['views'] for v in videos)
        tl = sum(v['likes'] for v in videos)
        tc = sum(v['comments'] for v in videos)
        weeks = max((videos[0]['published'] - videos[-1]['published']).days/7,1)
        analysis = {
            'uploads_per_week': round(len(videos)/weeks,1),
            'avg_duration': format_seconds(td/len(videos)),
            'likes_per_1000_views': round(tl/tv*1000,1) if tv else 0,
            'comments_per_1000_views': round(tc/tv*1000,1) if tv else 0,
            'top_5_videos': sorted(videos, key=lambda x: x['views'], reverse=True)[:5]
        }
    return render_template('analyze.html', stats=stats, videos=page_videos, analysis=analysis,
                           original_url=url, sort_by=sort_by, total_pages=total_pages,
                           current_page=page, CPM_USD=CPM_USD)

@app.route('/get-caption/<video_id>')
def get_caption(video_id):
    try:
        yt = YouTube(f"https://youtu.be/{video_id}", use_po_token=True)
        cap = yt.captions.get_by_language_code('ko') or yt.captions.get_by_language_code('en') or yt.captions.get_by_language_code('a.en')
        if not cap:
            return jsonify({'error':'자막이 없습니다.'}),404
        return jsonify({'title': yt.title, 'srt_content': cap.generate_srt_captions()})
    except:
        logger.exception(f"자막 로딩 오류({video_id})")
        return jsonify({'error':'자막 로딩 실패'}),500

@app.route('/get-caption-ai/<video_id>')
def get_caption_ai(video_id):
    if not WHISPER_MODEL:
        return jsonify({'error':'AI 자막 비활성화'}),503
    try:
        yt = YouTube(f"https://youtu.be/{video_id}", use_po_token=True)
        stream = yt.streams.filter(only_audio=True, file_extension="mp4").first()
        if not stream:
            return jsonify({'error':'오디오 스트림 없음'}),404
        with tempfile.TemporaryDirectory() as td:
            path = stream.download(output_path=td)
            segments, _ = WHISPER_MODEL.transcribe(path, beam_size=5, language='ko')
            srt = []
            for i,seg in enumerate(segments):
                start = f"{int(seg.start//3600):02}:{int(seg.start%3600//60):02}:{int(seg.start%60):02},{int(seg.start*1000%1000):03}"
                end   = f"{int(seg.end//3600):02}:{int(seg.end%3600//60):02}:{int(seg.end%60):02},{int(seg.end*1000%1000):03}"
                srt.append(f"{i+1}\n{start} --> {end}\n{seg.text.strip()}")
        return jsonify({'title':f"[AI] {ych}" , 'srt_content':"\n\n".join(srt)})
    except:
        logger.exception(f"AI 자막 오류({video_id})")
        return jsonify({'error':'AI 자막 실패'}),500

@app.route('/download-video/<video_id>')