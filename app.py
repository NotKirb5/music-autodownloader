#!/usr/bin/env python3
"""
AutoDownload — Download music from YouTube with MusicBrainz metadata.
Songs are organized into playlist-categorized folders under the music library.
"""

import os
import re
import json
import uuid
import time
import shutil
import logging
import threading
import queue as queue_module
from pathlib import Path
from typing import Optional
import dotenv
from flask import Flask, render_template, request, jsonify, Response
import yt_dlp
import acoustid
import musicbrainzngs
import requests
import pykakasi
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC

_kakasi = pykakasi.kakasi()
dotenv.load_dotenv()


def _romaji(s: str) -> str:
    try:
        return "".join(item["hepburn"] for item in _kakasi.convert(s))
    except Exception:
        return s


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

MUSIC_DIR = Path(os.path.expanduser("/mnt/styash/Music"))
TEMP_DIR = Path("/tmp/autodownload")
CONFIG_DIR = Path(os.path.expanduser("~/.config/autodownload"))
CONFIG_FILE = CONFIG_DIR / "config.json"
MUSIC_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

musicbrainzngs.set_useragent("AutoDownload", "1.0", "https://github.com/autodownload")
musicbrainzngs.set_rate_limit(1.0)

ACOUSTID_API_KEY = os.getenv("ACOUSTID_API_KEY")


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def save_config(config: dict):
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


def normalize_url(url: str) -> str:
    return re.sub(r"https?://music\.youtube\.com/", "https://www.youtube.com/", url)


def get_base_ydl_opts(
    cookies_from_browser: str = None, cookies_file: str = None
) -> dict:
    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "extractor_args": {"youtube": {"player_client": ["tv"]}},
    }
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    elif cookies_file:
        opts["cookiefile"] = cookies_file
    return opts


class TaskManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._tasks = {}

    def create(self) -> str:
        task_id = uuid.uuid4().hex[:8]
        with self._lock:
            self._tasks[task_id] = {
                "id": task_id,
                "status": "pending",
                "queue": queue_module.Queue(),
                "results": [],
            }
        return task_id

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            return self._tasks.get(task_id)

    def push(self, task_id: str, event: str, data: dict):
        task = self.get(task_id)
        if task:
            task["queue"].put({"event": event, "data": data})

    def update_status(self, task_id: str, status: str):
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["status"] = status

    def add_result(self, task_id: str, result: dict):
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["results"].append(result)

    def events(self, task_id: str):
        task = self.get(task_id)
        if not task:
            yield f"event: error\ndata: {json.dumps({'message': 'Task not found'})}\n\n"
            return
        q = task["queue"]
        while True:
            try:
                msg = q.get(timeout=30)
                yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'])}\n\n"
                if msg["event"] == "done":
                    break
            except queue_module.Empty:
                yield f"event: heartbeat\ndata: {json.dumps({})}\n\n"


tasks = TaskManager()


def sanitize(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = name.strip(". ")
    if len(name) > 200:
        name = name[:200]
    return name or "Unknown"


def norm(s: str) -> str:
    return re.sub(r"[^\w]", "", _romaji(s).lower())


def build_existing_index(base_dir: Path) -> dict:
    index = {}
    if not base_dir.exists():
        return index
    music_exts = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav"}
    for f in base_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in music_exts:
            key = norm(f.stem)
            if key not in index:
                index[key] = str(f)
    return index


def _read_tags(filepath: str):
    try:
        audio = EasyID3(filepath)
        artist_raw = str(audio.get("artist", [""])[0])
        title_raw = str(audio.get("title", [""])[0])
        return norm(artist_raw), norm(title_raw)
    except Exception:
        return None


def _build_tag_index(base_dir: Path) -> dict:
    tag_index = {}
    music_exts = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav"}
    for f in base_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in music_exts:
            tags = _read_tags(str(f))
            if tags and tags[0] and tags[1]:
                key = f"{tags[0]}\x00{tags[1]}"
                tag_index[key] = str(f)
    return tag_index


def check_duplicate(
    artist: str, title: str, existing_index: dict, tag_index: dict = None
) -> Optional[str]:
    art_n = norm(artist)
    ttl_n = norm(title)

    candidates = [
        (f"{art_n}{ttl_n}", 0),
        (f"{ttl_n}{art_n}", 0),
        (ttl_n, 1),
    ]

    for key, _min_len in candidates:
        if len(key) < 3:
            continue
        if key in existing_index:
            return existing_index[key]

    for key, _min_len in candidates:
        if len(key) < 8:
            continue
        for ek in existing_index:
            if len(ek) >= len(key) * 0.6 and (key in ek or ek in key):
                return existing_index[ek]

    if tag_index:
        for tag_key in (f"{art_n}\x00{ttl_n}", f"{ttl_n}\x00{art_n}"):
            if tag_key in tag_index:
                return tag_index[tag_key]

    return None


def parse_youtube_title(raw_title: str) -> tuple[str, str]:
    title = re.sub(
        r"\s*[\(\[].*?(official\s*(music\s*)?video|lyrics?(\s*video)?|audio|hd|hq|4k|1080p|720p|explicit|clean).*?[\)\]]\s*",
        "",
        raw_title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"\s*[\(\[][^\)\]]*?[\)\]]\s*$", "", title)
    title = title.strip()

    separators = [" — ", " - ", " ~ ", " – ", " : "]
    for sep in separators:
        if sep in title:
            parts = title.split(sep, 1)
            artist = parts[0].strip()
            track = parts[1].strip()

            track = re.sub(
                r"\s*[\(\[].*?(feat\.?|ft\.?|prod\.?|with|remix|cover).*?[\)\]]\s*",
                "",
                track,
                flags=re.IGNORECASE,
            )

            for s in separators:
                idx = track.find(s)
                if idx > 0:
                    tail = track[idx + len(s) :].strip()
                    if re.match(r"^[A-Z0-9\s\-_]{4,}$", tail):
                        track = track[:idx].strip()
                        break

            return artist, track or title

    return "Unknown Artist", title


def identify_track(filepath: str):
    try:
        results = acoustid.match(ACOUSTID_API_KEY, filepath, timeout=15)
        for score, recording_id, title, artist in results:
            if score > 0.5:
                return recording_id, title, artist
    except acoustid.WebServiceError as e:
        logger.warning("AcoustID web service error: %s", e)
    except acoustid.FingerprintGenerationError as e:
        logger.warning("AcoustID fingerprint error: %s", e)
    except Exception as e:
        logger.warning("AcoustID error: %s", e)
    return None, None, None


def search_musicbrainz_fallback(artist: str, title: str) -> dict:
    try:
        result = musicbrainzngs.search_recordings(
            artist=artist, recording=title, limit=3
        )
        recordings = result.get("recording-list", [])
        if not recordings:
            return {}
        best = recordings[0]
        meta = {
            "title": best.get("title", title),
            "artist": best.get("artist-credit", [{}])[0]
            .get("artist", {})
            .get("name", artist),
            "album": None,
            "date": None,
            "release_id": None,
        }
        release_list = best.get("release-list", [])
        if release_list:
            release = release_list[0]
            meta["album"] = release.get("title")
            meta["release_id"] = release.get("id")
            date = release.get("date")
            if date:
                meta["date"] = date[:4]
        return meta
    except Exception as e:
        logger.warning("MusicBrainz fallback search failed: %s", e)
        return {}


def get_mb_metadata(recording_id: str) -> dict:
    try:
        result = musicbrainzngs.get_recording_by_id(
            recording_id, includes=["artist-credits", "releases"]
        )
        rec = result["recording"]
        release = rec.get("release-list", [{}])[0]
        return {
            "title": rec["title"],
            "artist": rec["artist-credit"][0]["artist"]["name"],
            "album": release.get("title", ""),
            "date": release.get("date", ""),
            "release_id": release.get("id"),
        }
    except Exception as e:
        logger.warning("MusicBrainz metadata lookup failed: %s", e)
        return {}


def add_cover_art(filepath: str, release_id: str):
    if not release_id:
        return False
    try:
        r = requests.get(
            f"https://coverartarchive.org/release/{release_id}/front", timeout=10
        )
        if r.status_code == 200:
            audio = ID3(filepath)
            audio["APIC"] = APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,
                desc="Cover",
                data=r.content,
            )
            audio.save()
            return True
    except Exception as e:
        logger.warning("Cover art download failed: %s", e)
    return False


def embed_thumbnail(filepath: str, thumbnail_path: str):
    try:
        if Path(thumbnail_path).exists():
            audio = ID3(filepath)
            with open(thumbnail_path, "rb") as f:
                audio["APIC"] = APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,
                    desc="Cover",
                    data=f.read(),
                )
            audio.save()
            return True
    except Exception as e:
        logger.warning("Thumbnail embedding failed: %s", e)
    return False


def download_and_process(
    info: dict,
    playlist_name: str,
    task_id: str,
    existing_index: dict,
    base_ydl_opts: dict,
    tag_index: dict = None,
):
    raw_title = info.get("title", "Unknown")
    artist, title = parse_youtube_title(raw_title)
    logger.info("Parsed: artist=%r title=%r (raw=%r)", artist, title, raw_title)

    dupe = check_duplicate(artist, title, existing_index, tag_index)
    if dupe:
        tasks.push(
            task_id,
            "track_skip",
            {
                "artist": artist,
                "title": title,
                "reason": "already exists",
                "path": dupe,
            },
        )
        return

    tasks.push(
        task_id,
        "track_progress",
        {"artist": artist, "title": title, "stage": "downloading"},
    )

    video_url = (
        info.get("webpage_url") or f"https://youtube.com/watch?v={info.get('id')}"
    )
    temp_path = TEMP_DIR / f"{uuid.uuid4().hex}.%(ext)s"

    ydl_opts = dict(base_ydl_opts)
    ydl_opts.update(
        {
            "outtmpl": str(temp_path),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                },
            ],
            "writethumbnail": True,
        }
    )

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])
    except Exception as e:
        logger.error("Download failed: %s", e)
        tasks.push(
            task_id, "track_error", {"artist": artist, "title": title, "error": str(e)}
        )
        return

    mp3_files = sorted(
        TEMP_DIR.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not mp3_files:
        tasks.push(
            task_id,
            "track_error",
            {"artist": artist, "title": title, "error": "No output file found"},
        )
        return
    downloaded = mp3_files[0]

    thumbnail_files = sorted(
        TEMP_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    thumbnail = str(thumbnail_files[0]) if thumbnail_files else None

    tasks.push(
        task_id,
        "track_progress",
        {"artist": artist, "title": title, "stage": "identifying"},
    )

    recording_id, mb_title, mb_artist = identify_track(str(downloaded))

    if recording_id:
        meta = get_mb_metadata(recording_id)
    else:
        meta = search_musicbrainz_fallback(artist, title)
        if meta:
            logger.info(
                "AcoustID missed, but MB text search found: %s - %s",
                meta.get("artist"),
                meta.get("title"),
            )
        else:
            logger.info(
                "No MB match for: %s - %s, using YouTube metadata", artist, title
            )

    if meta:
        final_artist = meta.get("artist") or artist
        final_title = meta.get("title") or title
        final_album = meta.get("album") or playlist_name

        tasks.push(
            task_id,
            "track_progress",
            {"artist": final_artist, "title": final_title, "stage": "tagging"},
        )

        try:
            audio = EasyID3(str(downloaded))
            audio["title"] = final_title
            audio["artist"] = final_artist
            audio["album"] = final_album
            if recording_id:
                audio["musicbrainz_trackid"] = recording_id
            if meta.get("date"):
                audio["date"] = str(meta["date"])[:4]
            audio["albumartist"] = final_artist
            audio.save()
        except Exception as e:
            logger.warning("Failed to write ID3 tags: %s", e)

        cover_added = add_cover_art(str(downloaded), meta.get("release_id"))
        if not cover_added and thumbnail:
            embed_thumbnail(str(downloaded), thumbnail)

        mb_enriched = True
        target_folder = final_album if final_album else playlist_name
    else:
        final_artist = artist
        final_title = title
        final_album = ""

        tasks.push(
            task_id,
            "track_progress",
            {
                "artist": final_artist,
                "title": final_title,
                "stage": "tagging (fallback)",
            },
        )

        try:
            audio = EasyID3(str(downloaded))
            audio["title"] = final_title
            audio["artist"] = final_artist
            audio["album"] = playlist_name
            audio.save()
        except Exception as e:
            logger.warning("Failed to write fallback ID3 tags: %s", e)

        if thumbnail:
            embed_thumbnail(str(downloaded), thumbnail)

        mb_enriched = False
        target_folder = playlist_name

    tasks.push(
        task_id,
        "track_progress",
        {"artist": final_artist, "title": final_title, "stage": "organizing"},
    )

    dest_dir = MUSIC_DIR / sanitize(target_folder)
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = sanitize(f"{final_artist} - {final_title}.mp3")
    dest_path = dest_dir / filename

    counter = 1
    while dest_path.exists():
        dest_path = dest_dir / sanitize(
            f"{final_artist} - {final_title} ({counter}).mp3"
        )
        counter += 1

    shutil.move(str(downloaded), str(dest_path))
    existing_index[norm(dest_path.stem)] = str(dest_path)

    result = {
        "artist": final_artist,
        "title": final_title,
        "playlist": target_folder,
        "path": str(dest_path),
        "mb_enriched": mb_enriched,
    }
    tasks.add_result(task_id, result)
    tasks.push(task_id, "track_done", result)


def process_download(
    task_id: str,
    url: str,
    playlist_name: str,
    cookies_from_browser: str = None,
    cookies_file: str = None,
):
    try:
        url = normalize_url(url)
        tasks.update_status(task_id, "running")
        tasks.push(
            task_id,
            "status",
            {"status": "extracting", "message": "Extracting URL info..."},
        )

        existing_index = build_existing_index(MUSIC_DIR)
        tag_index = _build_tag_index(MUSIC_DIR)
        base_ydl_opts = get_base_ydl_opts(cookies_from_browser, cookies_file)

        with yt_dlp.YoutubeDL(base_ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info is None:
            tasks.push(task_id, "error", {"message": "Could not extract URL info"})
            return

        entries = []
        if info.get("_type") == "playlist" or "entries" in info:
            if not playlist_name or playlist_name.strip() == "":
                playlist_name = info.get("title") or "Playlist"
            entries = list(info.get("entries", []))
        else:
            if not playlist_name or playlist_name.strip() == "":
                playlist_name = "Singles"
            entries = [info]

        valid_entries = [e for e in entries if e is not None]
        if not valid_entries:
            tasks.push(task_id, "error", {"message": "No valid tracks found in URL"})
            return

        tasks.push(
            task_id,
            "status",
            {
                "status": "downloading",
                "message": f"Processing {len(valid_entries)} track(s) into '{playlist_name}'",
                "total": len(valid_entries),
                "playlist": playlist_name,
            },
        )

        for i, entry in enumerate(valid_entries):
            tasks.push(
                task_id,
                "status",
                {
                    "status": "downloading",
                    "message": f"Track {i + 1}/{len(valid_entries)}",
                    "current": i + 1,
                    "total": len(valid_entries),
                    "playlist": playlist_name,
                },
            )
            download_and_process(
                entry, playlist_name, task_id, existing_index, base_ydl_opts, tag_index
            )

        tasks.update_status(task_id, "done")
        tasks.push(
            task_id,
            "done",
            {
                "message": f"Finished processing {len(valid_entries)} track(s)",
                "playlist": playlist_name,
            },
        )

    except Exception as e:
        logger.exception("Download task failed")
        tasks.update_status(task_id, "error")
        tasks.push(task_id, "error", {"message": str(e)})

    finally:
        leftover = list(TEMP_DIR.glob("*"))
        for f in leftover:
            try:
                if f.is_file():
                    f.unlink()
            except Exception:
                pass


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    playlist_name = (data.get("playlist") or "").strip()
    cookies_from_browser = (data.get("cookies_from_browser") or "").strip() or None
    cookies_file = (data.get("cookies_file") or "").strip() or None

    task_id = tasks.create()
    thread = threading.Thread(
        target=process_download,
        args=(task_id, url, playlist_name, cookies_from_browser, cookies_file),
        daemon=True,
    )
    thread.start()

    return jsonify({"task_id": task_id})


@app.route("/api/stream/<task_id>")
def api_stream(task_id: str):
    def generate():
        for event_str in tasks.events(task_id):
            yield event_str

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/tasks/<task_id>")
def api_task_status(task_id: str):
    task = tasks.get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(
        {
            "id": task["id"],
            "status": task["status"],
            "results": task["results"],
        }
    )


@app.route("/api/library")
def api_library():
    index = build_existing_index(MUSIC_DIR)
    files = []
    for path_str in set(index.values()):
        p = Path(path_str)
        rel = p.relative_to(MUSIC_DIR)
        playlist = str(rel.parent) if str(rel.parent) != "." else "Singles"
        files.append({"name": p.stem, "playlist": playlist, "path": str(p)})
    files.sort(key=lambda x: (x["playlist"], x["name"]))
    return jsonify({"files": files})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        cfg = load_config()
        return jsonify(
            {
                "cookies_from_browser": cfg.get("cookies_from_browser") or "",
                "cookies_file": cfg.get("cookies_file") or "",
            }
        )
    else:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No JSON body"}), 400
        cfg = {}
        cb = (data.get("cookies_from_browser") or "").strip()
        cf = (data.get("cookies_file") or "").strip()
        if cb:
            cfg["cookies_from_browser"] = cb
        if cf:
            cfg["cookies_file"] = cf
        save_config(cfg)
        return jsonify(cfg)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)
