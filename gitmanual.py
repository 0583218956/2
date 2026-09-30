import hashlib
import json
import os
import re
import shutil
import time
from urllib.parse import urlparse

import feedparser
import requests

STATE_FILE = "gitmanual_seen.json"
DOWNLOAD_DIR = "downloads"   # תיקייה זמנית, מתנקה בתחילת כל הרצה ולא נשמרת ב-Git
ENV_OUTPUT_FILE = "env_output.env"

ALLOWED_COUNTS = (1, 5, 10, 15, 20, 30, 40, 100)
DEFAULT_COUNT = 5
MAX_NAME_LENGTH = 120

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

AUDIO_TYPES = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".opus")


# ---------------------------------------------------------------- קלט

def get_episodes_per_run():
    value = os.environ.get("EPISODES_PER_RUN", "").strip()
    if value.isdigit() and int(value) in ALLOWED_COUNTS:
        return int(value)
    if value:
        print(f"ערך לא תקין ב-EPISODES_PER_RUN: {value}. משתמש בברירת המחדל ({DEFAULT_COUNT}).")
    return DEFAULT_COUNT


def get_requested_episode_number():
    value = os.environ.get("EPISODE_NUMBER", "").strip()
    if not value:
        return None
    if value.isdigit() and int(value) > 0:
        return int(value)
    print(f"ערך לא תקין ב-EPISODE_NUMBER: {value}. מתעלם ממנו.")
    return None


# ---------------------------------------------------------------- state

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- עזרים

def sanitize_filename(name):
    """מסיר תווים אסורים במערכות קבצים, מנרמל רווחים ומקצר."""
    cleaned = re.sub(r'[\\/*?:"<>|\'„“‘’`]', "", name)
    cleaned = re.sub(r"[\x00-\x1f]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    cleaned = cleaned[:MAX_NAME_LENGTH].strip(" .")
    return cleaned or "podcast_episode"


def single_line(text):
    """שורה אחת בלבד - חשוב לכתיבה ל-GITHUB_ENV."""
    return re.sub(r"\s+", " ", text).strip()


def make_tag(rss_url):
    """תג טכני יציב: אותו פיד תמיד מקבל אותו Release."""
    digest = hashlib.sha1(rss_url.encode("utf-8")).hexdigest()[:10]
    return f"podcast-{digest}"


def detect_extension(enclosure):
    mime = (enclosure.get("type") or "").lower().split(";")[0].strip()
    if mime in AUDIO_TYPES:
        return AUDIO_TYPES[mime]
    ext = os.path.splitext(urlparse(enclosure.get("href", "")).path)[1].lower()
    if ext in AUDIO_EXTENSIONS:
        return ext
    return ".mp3"


def download_podcast(url, filename, folder):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, filename)
    tmp_path = path + ".part"

    for attempt in range(1, 4):
        try:
            with requests.get(url, headers=HEADERS, stream=True, timeout=600) as r:
                r.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
            os.replace(tmp_path, path)
            return path
        except Exception as e:
            print(f"ניסיון {attempt} נכשל בהורדת {filename}: {e}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            if attempt == 3:
                raise
            time.sleep(5)


def find_entry_index(entries, number):
    """מחזיר אינדקס של פרק לפי מספר: קודם itunes:episode, אחר כך מיקום מהפרק הישן ביותר."""
    for i, entry in enumerate(entries):
        if str(entry.get("itunes_episode", "")).strip() == str(number):
            return i
    if 1 <= number <= len(entries):
        return len(entries) - number  # הפיד ממוין מהחדש לישן
    return None


def entry_id(entry, audio_url):
    return entry.get("id") or entry.get("link") or audio_url


def build_filename(entry, sequence_number, enclosure):
    title = entry.get("title", "New episode")
    return f"{sequence_number:04d} - {sanitize_filename(title)}{detect_extension(enclosure)}"


# ---------------------------------------------------------------- main

def main():
    rss_url = os.environ.get("RSS_URL", "").strip()
    if not rss_url:
        print("לא הוגדרה כתובת RSS.")
        return

    per_run = get_episodes_per_run()
    requested_number = get_requested_episode_number()

    # ניקוי תיקיית ההורדות כדי שה-ZIP יכיל רק את מה שהורד בהרצה הזו
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    print(f"בודק את הפיד: {rss_url}")
    parsed = feedparser.parse(rss_url)
    if not parsed.entries:
        print("לא נמצאו פרקים בפיד.")
        return

    feed_title = single_line(parsed.feed.get("title", "Podcast")) or "Podcast"
    entries = parsed.entries
    total = len(entries)

    state = load_state()
    seen = set(state.get(rss_url, []))

    # ---- בחירת הפרקים להורדה ----
    targets = []  # רשימת (אינדקס בפיד, פרק)
    force = False

    if requested_number is not None:
        force = True
        idx = find_entry_index(entries, requested_number)
        if idx is None or not entries[idx].get("enclosures"):
            print(f"פרק מספר {requested_number} לא נמצא. מוריד את הפרק החדש ביותר.")
            idx = next((i for i, e in enumerate(entries) if e.get("enclosures")), None)
        else:
            print(f"מוריד פרק מספר {requested_number}.")
        if idx is not None:
            targets.append((idx, entries[idx]))

    elif per_run == 1:
        force = True
        idx = next((i for i, e in enumerate(entries) if e.get("enclosures")), None)
        print("מוריד את הפרק החדש ביותר.")
        if idx is not None:
            targets.append((idx, entries[idx]))

    else:
        print(f"כמות פרקים להורדה בהרצה זו: {per_run}")
        for idx in range(total):  # מהחדש לישן (הפיד ממוין מהחדש לישן)
            if len(targets) >= per_run:
                break
            entry = entries[idx]
            enclosures = entry.get("enclosures", [])
            if not enclosures:
                continue
            if entry_id(entry, enclosures[0].get("href")) in seen:
                continue
            targets.append((idx, entry))

    # ---- הורדה ----
    downloaded = 0
    for idx, entry in targets:
        enclosure = entry["enclosures"][0]
        audio_url = enclosure.get("href")
        if not audio_url:
            continue

        sequence_number = total - idx
        filename = build_filename(entry, sequence_number, enclosure)
        print(f"מוריד: {filename}")
        try:
            download_podcast(audio_url, filename, DOWNLOAD_DIR)
            seen.add(entry_id(entry, audio_url))
            downloaded += 1
        except Exception as e:
            print(f"שגיאה בהורדת {filename}: {e}")

    state[rss_url] = sorted(seen)
    save_state(state)

    # ---- פלט ל-workflow ----
    with open(ENV_OUTPUT_FILE, "w", encoding="utf-8") as env_file:
        env_file.write(f"PODCAST_TITLE={feed_title}\n")
        env_file.write(f"PODCAST_TAG={make_tag(rss_url)}\n")
        env_file.write(f"DOWNLOADED_COUNT={downloaded}\n")

    print(f"הסתיים. הורדו {downloaded} פרקים.")


if __name__ == "__main__":
    main()
