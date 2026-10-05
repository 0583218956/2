import hashlib
import json
import os
import re
import shutil
import time
from time import strftime
from urllib.parse import urlparse

import feedparser
import requests

STATE_FILE = "gitmanual_seen.json"
DOWNLOAD_DIR = "downloads"    # תיקייה זמנית, מתנקה בתחילת כל הרצה ולא נשמרת ב-Git
ENV_OUTPUT_FILE = "env_output.env"

ALLOWED_COUNTS = (1, 5, 10, 15, 20, 30, 40, 100)
DEFAULT_COUNT = 5
MAX_NAME_LENGTH = 120
MAX_RANGE = 100

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

# מספר פרק בתחילת כותרת: "Episode 266: ...", "Ep. 54: ...", "#12 ...", "109. ..."
_TITLE_PREFIXED = re.compile(r"^\W*(?:episode|ep\.?|#)\s*(\d+)\b", re.IGNORECASE)
_TITLE_BARE = re.compile(r"^\W*(\d+)\s*[.:)\-–—]")


# ---------------------------------------------------------------- קלט

def get_episodes_per_run():
    value = os.environ.get("EPISODES_PER_RUN", "").strip()
    if value.isdigit() and int(value) in ALLOWED_COUNTS:
        return int(value)
    if value:
        print(f"ערך לא תקין ב-EPISODES_PER_RUN: {value}. משתמש בברירת המחדל ({DEFAULT_COUNT}).")
    return DEFAULT_COUNT


def get_requested_episodes():
    """
    קורא את EPISODE_NUMBER ומחזיר אחד מאלה:
      None                       - אין בקשה ספציפית
      ("numbers", start, end)    - מספר ("25") או טווח ("25-35")
      ("text", query)            - טקסט לחיפוש בכותרת
    """
    value = os.environ.get("EPISODE_NUMBER", "").strip()
    if not value:
        return None

    m = re.fullmatch(r"(\d+)\s*(?:[-–—:]\s*(\d+))?", value)
    if not m:
        return ("text", value)

    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    if start > end:
        start, end = end, start
    if start < 1:
        print(f"ערך לא תקין ב-EPISODE_NUMBER: {value}. מתעלם ממנו.")
        return None
    if end - start + 1 > MAX_RANGE:
        end = start + MAX_RANGE - 1
        print(f"הטווח ארוך מדי. מוגבל ל-{MAX_RANGE} פרקים ({start}-{end}).")
    return ("numbers", start, end)


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


def oldest_first(entries):
    """רשימת הפרקים מהישן לחדש."""
    if entries and all(e.get("published_parsed") for e in entries):
        return sorted(entries, key=lambda e: mktime(e["published_parsed"]))
    return list(reversed(entries))


def entry_numbers(entry):
    """כל המספרים שכתובים בפיד עבור הפרק: תג itunes:episode ומספר בתחילת הכותרת."""
    numbers = set()
    tag = str(entry.get("itunes_episode", "")).strip()
    if tag.isdigit():
        numbers.add(int(tag))

    title = entry.get("title", "") or ""
    for pattern in (_TITLE_PREFIXED, _TITLE_BARE):
        m = pattern.match(title)
        if m:
            numbers.add(int(m.group(1)))
            break
    return numbers


def entry_id(entry, audio_url=""):
    eid = entry.get("id") or entry.get("link")
    if not eid and entry.get("enclosures"):
        eid = entry.enclosures[0].get("href")
    return eid or audio_url


def build_filename(entry, sequence_number, enclosure):
    title = entry.get("title", "New episode")
    return f"{sequence_number:04d} - {sanitize_filename(title)}{detect_extension(enclosure)}"


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


# ---------------------------------------------------------------- רשימת פרקים (List Only)

def build_episode_list(entries):
    """יוצר טקסט של כל הפרקים מהישן לחדש: מספר, תאריך וכותרת."""
    ordered = oldest_first(entries)
    feed_has_numbers = any(entry_numbers(e) for e in ordered)

    lines = []
    if feed_has_numbers:
        lines.append("המספרים הם המספרים הכתובים בפיד. '-' = אין מספר.")
    else:
        lines.append("בפיד אין מספרים כתובים. הפרקים ממוספרים לפי סדר (הישן ביותר = 1).")
    lines.append(f"סה\"כ {len(ordered)} פרקים. פורמט: מספר | תאריך | כותרת")
    lines.append("")

    for pos, e in enumerate(ordered, start=1):
        if feed_has_numbers:
            nums = sorted(entry_numbers(e))
            label = "/".join(str(n) for n in nums) if nums else "-"
        else:
            label = str(pos)
        date = strftime("%Y-%m-%d", e["published_parsed"]) if e.get("published_parsed") else "????-??-??"
        lines.append(f"{label} | {date} | {e.get('title', '')}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- בחירת פרקים לפי מספרים / טקסט

def select_by_numbers(entries, start, end):
    ordered = oldest_first(entries)
    feed_has_numbers = any(entry_numbers(e) for e in ordered)

    if feed_has_numbers:
        print("בפיד יש מספרי פרקים כתובים - מחפש לפי המספר הכתוב.")
    else:
        print("בפיד אין מספרי פרקים כתובים - סופר לפי מיקום (הפרק הכי ישן = 1).")

    chosen = []
    for number in range(start, end + 1):
        if feed_has_numbers:
            idxs = [i for i, e in enumerate(ordered) if number in entry_numbers(e)]
        else:
            idxs = [number - 1] if 1 <= number <= len(ordered) else []

        if not idxs:
            print(f"פרק מספר {number} לא נמצא.")
            continue
        for idx in idxs:
            if idx in chosen:
                continue
            if not ordered[idx].get("enclosures"):
                print(f"בפרק מספר {number} אין קובץ שמע.")
                continue
            chosen.append(idx)

    chosen.sort()
    return [(i, ordered[i]) for i in chosen]


def select_by_text(entries, query):
    q = query.strip().lower()
    ordered = oldest_first(entries)
    matches = [
        (i, e) for i, e in enumerate(ordered)
        if q in (e.get("title", "") or "").lower() and e.get("enclosures")
    ]
    if len(matches) > MAX_RANGE:
        matches = matches[:MAX_RANGE]
    return matches


# ---------------------------------------------------------------- main

def main():
    rss_url = os.environ.get("RSS_URL", "").strip()
    if not rss_url:
        print("לא הוגדרה כתובת RSS.")
        return

    per_run = get_episodes_per_run()
    requested = get_requested_episodes()
    list_only = os.environ.get("LIST_ONLY", "").strip().lower() == "true"

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

    # מצב יצירת רשימה בלבד (TXT)
    if list_only:
        text = build_episode_list(entries)
        print(text)
        list_path = os.path.join(DOWNLOAD_DIR, "episodes_list.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            f.write(text)

        with open(ENV_OUTPUT_FILE, "w", encoding="utf-8") as env_file:
            env_file.write(f"PODCAST_TITLE={feed_title}\n")
            env_file.write(f"PODCAST_TAG={make_tag(rss_url)}\n")
            env_file.write(f"DOWNLOADED_COUNT=1\n")
        print("[V] רשימת הפרקים נוצרה בהצלחה.")
        return

    # ---- בחירת הפרקים להורדה ----
    targets = []

    if requested is not None:
        if requested[0] == "numbers":
            _, start, end = requested
            targets = select_by_numbers(entries, start, end)
        else:
            _, query = requested
            targets = select_by_text(entries, query)
    else:
        # רגיל: לוקח מהחדש לישן את מה שלא הורד עדיין
        ordered = oldest_first(entries)
        unseen = []
        for e in ordered:
            enclosures = e.get("enclosures", [])
            if not enclosures:
                continue
            eid = entry_id(e, enclosures[0].get("href"))
            if eid not in seen:
                unseen.append((e, enclosures[0].get("href")))

        # לוקח לפי per_run מהחדשים ביותר (כלומר מהסוף של הרשימה מהישן-לחדש)
        # או אפשר לפי הסדר שמוגדר. ניקח את ה-per_run האחרונים ברשימת oldest_first שטרם הורדו:
        selected_unseen = unseen[-per_run:] if per_run < len(unseen) else unseen
        # נמצא את האינדקס המקורי שלהם ב-entries
        for e_target, url_target in selected_unseen:
            idx = next((i for i, item in enumerate(entries) if entry_id(item) == entry_id(e_target)), None)
            if idx is not None:
                targets.append((idx, e_target))

    if not targets:
        print("לא נמצאו פרקים להורדה.")
        return

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

    with open(ENV_OUTPUT_FILE, "w", encoding="utf-8") as env_file:
        env_file.write(f"PODCAST_TITLE={feed_title}\n")
        env_file.write(f"PODCAST_TAG={make_tag(rss_url)}\n")
        env_file.write(f"DOWNLOADED_COUNT={downloaded}\n")

    print(f"הסתיים. הורדו {downloaded} פרקים.")


if __name__ == "__main__":
    main()
