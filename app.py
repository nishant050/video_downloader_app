from flask import Flask, render_template, request, jsonify, send_file
import yt_dlp
import yt_dlp.utils as ytd_utils
import tempfile
import os
import uuid
import shutil
import logging
import time
import threading
from werkzeug.utils import secure_filename
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('vidextract')

app = Flask(__name__, static_folder='static', template_folder='templates')

# in-memory job tracker: job_id -> {status, progress, filepath, filename, error}
DOWNLOADS = {}
DL_LOCK = threading.Lock()
PLAY_JOBS = {}
PLAY_LOCK = threading.Lock()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/extract', methods=['POST'])
def extract():
    data = request.get_json() or {}
    url = data.get('url')
    # optional tuning from client
    timeout = int(data.get('timeout', 30))
    retries = int(data.get('retries', 2))
    proxy = data.get('proxy')  # e.g. "http://127.0.0.1:8888"

    if not url:
        return jsonify({'error': 'missing url'}), 400

    method = data.get('method', 'auto')

    base_opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'http_chunk_size': 1048576,
        'format': 'best',
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/116.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9'
        }
    }
    info = None
    last_err = None
    # if user explicitly requested Playwright, skip yt-dlp and run capture
    if method == 'playwright':
        # start background playwright capture job and return job id
        job_id = uuid.uuid4().hex
        with PLAY_LOCK:
            PLAY_JOBS[job_id] = {'status': 'queued', 'candidates': None, 'error': None}

        def play_job():
            try:
                with PLAY_LOCK:
                    PLAY_JOBS[job_id]['status'] = 'running'
                candidates = run_playwright_capture(url, timeout=timeout, proxy=proxy)
                with PLAY_LOCK:
                    PLAY_JOBS[job_id].update({'status': 'finished', 'candidates': candidates})
            except Exception as e:
                logger.exception('playwright job failed')
                with PLAY_LOCK:
                    PLAY_JOBS[job_id].update({'status': 'error', 'error': str(e)})

        t = threading.Thread(target=play_job, daemon=True)
        t.start()
        return jsonify({'job_id': job_id}), 202
    if method == 'selenium':
        # start background selenium capture job and return job id
        job_id = uuid.uuid4().hex
        with PLAY_LOCK:
            PLAY_JOBS[job_id] = {'status': 'queued', 'candidates': None, 'error': None}

        def sel_job():
            try:
                with PLAY_LOCK:
                    PLAY_JOBS[job_id]['status'] = 'running'
                candidates = run_selenium_capture(url, timeout=timeout, proxy=proxy)
                with PLAY_LOCK:
                    PLAY_JOBS[job_id].update({'status': 'finished', 'candidates': candidates})
            except Exception as e:
                logger.exception('selenium job failed')
                with PLAY_LOCK:
                    PLAY_JOBS[job_id].update({'status': 'error', 'error': str(e)})

        t = threading.Thread(target=sel_job, daemon=True)
        t.start()
        return jsonify({'job_id': job_id}), 202

    for attempt in range(retries + 1):
        ydl_opts = dict(base_opts)
        attempt_timeout = timeout * (1 + attempt)
        ydl_opts['socket_timeout'] = int(attempt_timeout)
        ydl_opts['retries'] = 0
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except Exception as e:
            last_err = e
            msg = str(e)
            logger.warning('extract attempt %d for %s failed: %s', attempt + 1, url, msg)
            if attempt < retries:
                backoff = min(5 * (attempt + 1), 20)
                logger.info('retrying after %ss', backoff)
                time.sleep(backoff)
                continue
            # final failure -> try Playwright fallback
            logger.info('yt-dlp failed after retries; attempting Playwright capture')
            try:
                candidates = run_playwright_capture(url, timeout=timeout, proxy=proxy)
                return jsonify({'error': 'yt-dlp failed', 'detail': msg, 'candidates': candidates}), 207
            except Exception as pe:
                logger.exception('playwright fallback failed')
                if 'timed out' in msg or 'Connection to' in msg or isinstance(e, ytd_utils.DownloadError):
                    return jsonify({'error': 'network timeout or transport error', 'detail': msg}), 504
                return jsonify({'error': msg}), 500

    # If a playlist or similar, pick the first entry for simplicity
    if isinstance(info, dict) and info.get('entries'):
        info = info['entries'][0]

    formats = []
    for f in info.get('formats', []):
        formats.append({
            'format_id': f.get('format_id'),
            'ext': f.get('ext'),
            'format_note': f.get('format_note'),
            'filesize': f.get('filesize') or f.get('filesize_approx'),
            'height': f.get('height'),
            'width': f.get('width'),
            'tbr': f.get('tbr'),
            'acodec': f.get('acodec'),
            'vcodec': f.get('vcodec'),
            'protocol': f.get('protocol'),
        })

    return jsonify({
        'id': info.get('id'),
        'title': info.get('title'),
        'uploader': info.get('uploader'),
        'duration': info.get('duration'),
        'thumbnail': info.get('thumbnail'),
        'formats': formats,
    })


def run_playwright_capture(url, timeout=30, proxy=None):
    """Launch Playwright headless Chromium, navigate to url, capture network responses and return candidate media URLs."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError('playwright not installed or failed to import; install with "pip install playwright" and run "playwright install"')

    candidates = []
    seen = set()

    with sync_playwright() as pw:
        browser_args = {}
        if proxy:
            browser_args['proxy'] = { 'server': proxy }
        browser = pw.chromium.launch(headless=True, **browser_args)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        def handle_response(response):
            try:
                rurl = response.url
                if not rurl or rurl in seen:
                    return
                headers = response.headers
                ctype = (headers.get('content-type') or '').lower()
                # heuristics: file extensions and content types
                parsed = urlparse(rurl)
                path = parsed.path or ''
                if any(path.endswith(ext) for ext in ('.m3u8', '.mp4', '.ts', '.webm', '.m4a', '.mp3', '.mpd', '.mkv')) or \
                   ('mpegurl' in ctype) or ('dash' in ctype) or ctype.startswith('video') or ctype.startswith('audio'):
                    seen.add(rurl)
                    candidates.append({'url': rurl, 'content_type': ctype})
            except Exception:
                pass

        page.on('response', handle_response)

        try:
            page.goto(url, wait_until='domcontentloaded', timeout=timeout*1000)
        except Exception:
            # still continue to capture network requests
            pass

        # wait for network to settle and for potential players to load
        try:
            page.wait_for_load_state('networkidle', timeout=timeout*1000)
        except Exception:
            pass

        # look for video elements and try to play them to trigger fetches
        try:
            videos = page.query_selector_all('video')
            for v in videos:
                try:
                    page.evaluate('(v) => v.play().catch(()=>{})', v)
                except Exception:
                    pass
        except Exception:
            pass

        # give a small buffer for late requests
        time.sleep(2)
        # gather candidates from captured responses
        browser.close()

    # de-duplicate and return
    return candidates


def run_selenium_capture(url, timeout=30, proxy=None):
    """Attempt to capture direct media URLs using Selenium (Chrome/Chromium).
    This is experimental and kept as a fallback option when Playwright is not desired.
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
    except Exception:
        raise RuntimeError('selenium not installed; install with "pip install selenium" and ensure chromedriver is available')

    opts = Options()
    # Use new headless if supported, else fallback
    try:
        opts.add_argument('--headless=new')
    except Exception:
        opts.add_argument('--headless')
    # common stability flags
    opts.add_argument('--disable-gpu')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-extensions')
    opts.add_argument('--disable-background-timer-throttling')
    opts.add_argument('--disable-renderer-backgrounding')
    if proxy:
        opts.add_argument(f'--proxy-server={proxy}')

    driver = None
    # try to instantiate webdriver.Chrome; if not available, try webdriver-manager to obtain a driver

        try:
            try:
                driver = webdriver.Chrome(options=opts)
            except Exception:
                # attempt to use webdriver-manager to download a matching chromedriver
                try:
                    from webdriver_manager.chrome import ChromeDriverManager
                    driver = webdriver.Chrome(ChromeDriverManager().install(), options=opts)
                except Exception:
                    raise

        # navigate with a moderate timeout and then poll for video element presence
        try:
            # allow a longer load timeout to avoid renderer timeouts on heavy pages
            driver.set_page_load_timeout(max(30, timeout))
            driver.get(url)
        except Exception:
            # navigation may time out on heavy pages; continue and try to wait for elements
            pass

        # wait for any video element to appear (short wait)
        try:
            WebDriverWait(driver, min(10, timeout)).until(EC.presence_of_element_located((By.TAG_NAME, 'video')))
        except Exception:
            # still continue even if no video element appears
            pass

        candidates = []
        seen = set()

        # try to extract video tags and their srcs
        try:
            videos = driver.find_elements(By.TAG_NAME, 'video')
            for v in videos:
                try:
                    src = v.get_attribute('src')
                    if src and src not in seen:
                        seen.add(src)
                        candidates.append({'url': src, 'content_type': None})
                except Exception:
                    continue
        except Exception:
            pass

        # fallback: inspect network via performance logs if available (best-effort)
        try:
            # many chromedriver setups don't expose network logs; ignore failures
            logs = driver.get_log('performance')
            for entry in logs:
                import json
                try:
                    msg = json.loads(entry.get('message', '{}'))
                    params = msg.get('message', {}).get('params', {})
                    message = params.get('message') or {}
                    if message.get('method') == 'Network.responseReceived':
                        r = message.get('params', {}).get('response', {})
                        rurl = r.get('url')
                        ctype = r.get('mimeType')
                        if rurl and (rurl.endswith(('.mp4', '.m3u8', '.webm', '.m4a', '.mp3', '.ts')) or (ctype and (ctype.startswith('video') or ctype.startswith('audio')))):
                            if rurl not in seen:
                                seen.add(rurl)
                                candidates.append({'url': rurl, 'content_type': ctype})
                except Exception:
                    continue
        except Exception:
            pass

    # very small delay to allow lazy-loaded players to fetch
    time.sleep(1)

    return candidates
    finally:
        try:
            if driver:
                driver.quit()
        except Exception:
            pass


@app.route('/api/playjob/<job_id>', methods=['GET'])
def playjob_status(job_id):
    with PLAY_LOCK:
        job = PLAY_JOBS.get(job_id)
        if not job:
            return jsonify({'error': 'not found'}), 404
        return jsonify(job)



@app.route('/api/download', methods=['POST'])
def api_download():
    data = request.get_json() or {}
    url = data.get('url')
    format_id = data.get('format_id')
    acodec = data.get('acodec')
    kind = data.get('kind', 'video')  # 'video' or 'audio'
    audio_bitrate = data.get('abr', '192')
    timeout = int(data.get('timeout', 60))
    retries = int(data.get('retries', 2))
    proxy = data.get('proxy')

    if not url:
        return jsonify({'error': 'missing url'}), 400

    method = data.get('method', 'auto')

    # if user requested playwright for download, try capture first and set url to candidate
    if method == 'playwright' or method == 'selenium':
        try:
            if method == 'playwright':
                candidates = run_playwright_capture(url, timeout=timeout, proxy=proxy)
            else:
                candidates = run_selenium_capture(url, timeout=timeout, proxy=proxy)
            if candidates:
                # pick first candidate URL
                url = candidates[0]['url']
            else:
                # return candidates (empty) so frontend can show friendly message
                return jsonify({'candidates': [], 'error': 'no media candidates found by Playwright'}), 207
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    job_id = uuid.uuid4().hex
    with DL_LOCK:
        DOWNLOADS[job_id] = {'status': 'queued', 'progress': 0, 'filepath': None, 'filename': None, 'error': None}

    def download_job(job_id, url, format_id, acodec, kind, audio_bitrate, timeout, retries, proxy):
        tmpdir = tempfile.mkdtemp(prefix='vd-')
        basename = uuid.uuid4().hex
        outtmpl = os.path.join(tmpdir, basename + '.%(ext)s')

        ydl_opts = {
            'outtmpl': outtmpl,
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'http_chunk_size': 1048576,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                              '(KHTML, like Gecko) Chrome/116.0 Safari/537.36',
            }
        }
        if proxy:
            ydl_opts['proxy'] = proxy

        # Keep track if the user requested audio-only or video
        if kind == 'audio':
            ydl_opts.update({
                'format': format_id or 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': str(audio_bitrate),
                }]
            })
        else:
            ydl_opts['format'] = format_id or 'best'

        def progress_hook(d):
            try:
                with DL_LOCK:
                    job = DOWNLOADS.get(job_id)
                    if not job:
                        return
                    if d.get('status') == 'downloading':
                        total = d.get('total_bytes') or d.get('total_bytes_estimate')
                        downloaded = d.get('downloaded_bytes', 0)
                        job['status'] = 'downloading'
                        job['progress'] = int((downloaded / total) * 100) if total else 0
                        job['speed'] = d.get('speed')
                        job['eta'] = d.get('eta')
                    elif d.get('status') == 'finished':
                        job['status'] = 'processing'
            except Exception:
                logger.exception('progress hook error')

        ydl_opts['progress_hooks'] = [progress_hook]

        info = None
        try:
            # try with retries/backoff
            for attempt in range(retries + 1):
                attempt_timeout = timeout * (1 + attempt)
                ydl_opts['socket_timeout'] = int(attempt_timeout)
                ydl_opts['retries'] = 0
                # If the selected format has no audio and user requested video, try a combined format first
                attempt_opts = dict(ydl_opts)
                if kind == 'video' and (acodec in (None, '', 'none', 'unknown')):
                    # prefer the specific format combined with bestaudio when possible
                    if format_id:
                        attempt_opts['format'] = f"{format_id}+bestaudio/best"
                    else:
                        attempt_opts['format'] = 'bestvideo+bestaudio/best'
                try:
                    with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                    break
                except Exception as e:
                    logger.warning('download attempt %d failed: %s', attempt + 1, e)
                    if attempt < retries:
                        time.sleep(min(5 * (attempt + 1), 30))
                        continue
                    else:
                        raise

            # if the requested format is video-only (no audio), yt-dlp may still have downloaded only video
            # detect that and (after main download) fetch bestaudio and merge if needed (handled below)

            # find produced file
            produced = []
            for root, _, files in os.walk(tmpdir):
                for f in files:
                    produced.append(os.path.join(root, f))

            if not produced:
                raise RuntimeError('no file produced')

            produced.sort(key=lambda p: os.path.getsize(p), reverse=True)
            file_path = produced[0]
            title = (info.get('title') or basename) if isinstance(info, dict) else basename
            filename = secure_filename(title) + os.path.splitext(file_path)[1]

            # Use ffprobe (if available) to check if the produced file has an audio stream
            has_audio = None
            try:
                import subprocess, json
                probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'json', file_path], capture_output=True, text=True)
                if probe.returncode == 0 and probe.stdout:
                    info_json = json.loads(probe.stdout)
                    streams = info_json.get('streams') or []
                    has_audio = any(s.get('codec_type') == 'audio' for s in streams)
            except Exception:
                has_audio = None

            need_merge = False
            if has_audio is False:
                need_merge = True
            else:
                # fallback to yt-dlp info dict if ffprobe unavailable
                if isinstance(info, dict) and info.get('acodec') in (None, 'none', 'unknown') and info.get('vcodec') and info.get('vcodec') != 'none':
                    need_merge = True

            if need_merge:
                # download best audio into tmpdir
                audio_out = os.path.join(tmpdir, basename + '.bestaudio')
                a_opts = dict(ydl_opts)
                a_opts.update({'outtmpl': audio_out + '.%(ext)s', 'format': 'bestaudio/best', 'postprocessors': []})
                try:
                    with yt_dlp.YoutubeDL(a_opts) as ydl2:
                        a_info = ydl2.extract_info(url, download=True)
                except Exception:
                    a_info = None

                # find audio file
                audio_file = None
                for root, _, files in os.walk(tmpdir):
                    for f in files:
                        if f.startswith(basename + '.bestaudio') or f.endswith(('.m4a', '.mp3', '.webm', '.opus')):
                            audio_file = os.path.join(root, f)
                            break
                    if audio_file:
                        break

                if audio_file:
                    # merge using ffmpeg (yt-dlp's postprocessor can also be used, but call ffmpeg directly)
                    merged_path = os.path.join(tmpdir, basename + '.merged.mp4')
                    import subprocess
                    cmd = ['ffmpeg', '-y', '-i', file_path, '-i', audio_file, '-c:v', 'copy', '-c:a', 'aac', merged_path]
                    try:
                        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        file_path = merged_path
                        filename = secure_filename(title) + os.path.splitext(file_path)[1]
                    except Exception:
                        # if ffmpeg merge fails, leave original file and log
                        logger.exception('ffmpeg merge failed')

            with DL_LOCK:
                DOWNLOADS[job_id].update({'status': 'finished', 'progress': 100, 'filepath': file_path, 'filename': filename})
        except Exception as e:
            logger.exception('job failed')
            with DL_LOCK:
                DOWNLOADS[job_id].update({'status': 'error', 'error': str(e)})

    t = threading.Thread(target=download_job, args=(job_id, url, format_id, acodec, kind, audio_bitrate, timeout, retries, proxy), daemon=True)
    t.start()

    return jsonify({'job_id': job_id}), 202


@app.route('/api/status/<job_id>', methods=['GET'])
def api_status(job_id):
    with DL_LOCK:
        job = DOWNLOADS.get(job_id)
        if not job:
            return jsonify({'error': 'not found'}), 404
        return jsonify(job)


@app.route('/api/file/<job_id>', methods=['GET'])
def api_file(job_id):
    with DL_LOCK:
        job = DOWNLOADS.get(job_id)
        if not job:
            return jsonify({'error': 'not found'}), 404
        if job['status'] != 'finished' or not job.get('filepath'):
            return jsonify({'error': 'not ready'}), 409
        path = job['filepath']
        fname = job.get('filename') or os.path.basename(path)

    # send file and cleanup
    from flask import after_this_request

    @after_this_request
    def cleanup(response):
        try:
            # remove produced file and its directory
            d = os.path.dirname(path)
            try:
                shutil.rmtree(d)
            except PermissionError:
                # on Windows the file might be briefly locked; schedule a retry in background
                def _retry_remove(p):
                    import time
                    for _ in range(6):
                        try:
                            time.sleep(1)
                            shutil.rmtree(p)
                            return
                        except Exception:
                            continue
                threading.Thread(target=_retry_remove, args=(d,), daemon=True).start()
            with DL_LOCK:
                DOWNLOADS.pop(job_id, None)
        except Exception:
            logger.exception('cleanup failed')
        return response

    return send_file(path, as_attachment=True, download_name=fname)


if __name__ == '__main__':
    # disable reloader to avoid spurious restarts triggered by changes in site-packages
    # in some Windows environments the watchdog may detect unrelated files and repeatedly
    # restart the server; use_reloader=False keeps the debug server stable while developing.
    app.run(debug=True, use_reloader=False, port=5000)
