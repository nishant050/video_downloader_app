VidExtract - Local Video & Audio Extractor

This is a local Flask app that uses yt-dlp and ffmpeg to extract video/audio from a webpage and offer downloads in available qualities.

Quick start (Windows PowerShell):

1. Create a virtual env and activate:
```
python -m venv .venv; .\.venv\Scripts\Activate.ps1
```
2. Install dependencies:
```
python -m pip install -r requirements.txt
```
If you plan to use the Playwright fallback, also run:
```
python -m playwright install
```
3. Ensure ffmpeg is installed and on PATH. On Windows, you can install ffmpeg from https://ffmpeg.org and add to PATH.

4. Run the app:
```
python app.py
```

Open http://127.0.0.1:5000 in your browser.

Notes:
- This app is a local tool. Respect site terms of service and copyright when downloading.
- yt-dlp supports many sites; some sites may require additional headers/auth which are not handled in this minimal app.
 - Advanced: in the UI's "Advanced options" you can choose the download method: "Auto" (yt-dlp then Playwright fallback), "yt-dlp" only, or "Playwright" (force browser capture). Playwright requires the extra install step above.
