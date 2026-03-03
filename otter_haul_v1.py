#!/usr/bin/env python3
"""
otter-export  —  Download all your Otter.ai transcripts as plain-text files
============================================================================

USAGE
-----
    python otter_export_v10.py [options] [email] [password]

    If you omit email / password you will be prompted for them interactively
    (safer than passing credentials on the command line where they may appear
    in shell history).

OPTIONS
-------
    --output-dir DIR    Where to save transcripts.
                        Default: OtterImport/ folder next to this script.
    --retry             Re-attempt only the transcripts listed in _errors.csv.
                        Run this after a normal run to chase any failures.
    --full-index        Discard the cached account index and rebuild it from
                        scratch by paging through every speech in your account.
                        Normally not needed; the index updates incrementally.
    --verbose           Print extra diagnostic output (API response keys,
                        pagination cursors, etc.).

OUTPUT FILES (all saved inside --output-dir)
--------------------------------------------
    *.txt               One file per transcript, named "<date>_<title>.txt".
                        Each file has a short header (title, date, duration,
                        URL) followed by the transcript body.
    _index.json         Local copy of your full Otter account index.
                        Updated incrementally on every run.
    _downloaded.csv     Append-only log of every transcript saved to disk.
    _errors.csv         Failures from the most recent run.  Pass --retry to
                        attempt these again.

EXAMPLES
--------
    # First run — builds the index and downloads everything
    python otter_export_v10.py me@example.com

    # Subsequent run — only fetches new transcripts
    python otter_export_v10.py me@example.com

    # Retry anything that failed last time
    python otter_export_v10.py me@example.com --retry

    # Save to a custom folder
    python otter_export_v10.py me@example.com --output-dir ~/Documents/Otter

REQUIREMENTS
------------
    pip install requests

NOTES
-----
  * This script uses Otter.ai's unofficial internal API (the same endpoints
    the web app uses).  It is not an official integration and could break if
    Otter changes their API.
  * bulk_export returns a ZIP archive; this script extracts the .txt inside.
  * For large accounts (1,000+ transcripts) the first run takes several
    minutes because of the 1.5-second courtesy delay between downloads.
  * Some older recordings may have no transcript content at all (the recording
    ran but Otter never processed it).  Those appear in _errors.csv with the
    reason "no transcript segments in response" and cannot be recovered.
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import requests

# ── DEFAULT CONFIG ─────────────────────────────────────────────────────────────
# Adjust these if you run into rate-limit or timeout issues.

SLEEP_BETWEEN  = 1.5   # seconds between individual transcript downloads
SLEEP_PAGE     = 0.5   # seconds between pagination requests
PAGE_SIZE      = 50    # speeches per API page (Otter's max)
DL_TIMEOUT     = 30    # seconds before a download request times out
MAX_DL_RETRIES = 3     # download attempts before giving up on a transcript
RETRY_BACKOFF  = [5, 15, 30]  # wait times (seconds) between retry attempts
MIN_WORDS      = 15    # minimum word count for a transcript to be saved
# ──────────────────────────────────────────────────────────────────────────────

API_BASE = "https://otter.ai/forward/api/v1/"

DOWNLOADED_FIELDS = ["speech_id", "otid", "filename", "title",
                     "date", "url", "downloaded_at"]
ERRORS_FIELDS     = ["speech_id", "otid", "title", "date", "url", "reason"]


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTHENTICATION
# ═══════════════════════════════════════════════════════════════════════════════

def make_session(username: str, password: str) -> Tuple[requests.Session, str]:
    """
    Create an authenticated requests.Session and return (session, userid).

    Otter requires both HTTP Basic Auth on every request AND a GET to /login
    to obtain the userid that must be passed in subsequent API calls.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://otter.ai/",
        "Origin":  "https://otter.ai",
    })
    # Basic Auth is sent on every request via the session.
    session.auth = (username, password)
    # Store credentials so we can re-authenticate if a token expires mid-run.
    session._otter_credentials = (username, password)  # type: ignore[attr-defined]

    print("🔐 Logging in…")
    resp = session.get(
        API_BASE + "login",
        params={"username": username},
        timeout=30,
    )
    if resp.status_code != 200:
        sys.exit(f"❌ Login failed (HTTP {resp.status_code}): {resp.text[:300]}")

    userid = resp.json().get("userid")
    if not userid:
        sys.exit(f"❌ Login response contained no userid.  "
                 f"Keys returned: {list(resp.json().keys())}")

    print(f"✅ Logged in  (userid={userid})")
    return session, userid


def reauth(session: requests.Session) -> Tuple[requests.Session, str]:
    """
    Re-create the session from stored credentials.
    Called automatically when an API request returns HTTP 401 or 403.
    """
    u, p = session._otter_credentials  # type: ignore[attr-defined]
    print("   🔄 Re-authenticating (session expired)…")
    new_session, userid = make_session(u, p)
    new_session._otter_credentials = (u, p)  # type: ignore[attr-defined]
    return new_session, userid


# ═══════════════════════════════════════════════════════════════════════════════
#  ZIP EXTRACTION
#
#  The bulk_export endpoint returns a ZIP archive rather than raw text.
#  We detect this via the "PK" magic bytes at the start of the response body
#  and unzip in memory to get the .txt file inside.
# ═══════════════════════════════════════════════════════════════════════════════

def extract_text_from_zip(data: bytes) -> Optional[str]:
    """
    If *data* is a ZIP file (starts with the PK magic bytes 0x50 0x4B),
    open it in memory, find the first .txt file inside, and return its
    decoded text.  Returns None if *data* is not a valid ZIP or contains
    no usable text file.
    """
    if not data or data[:2] != b'PK':
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # Prefer files with a .txt extension; fall back to any file.
            names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
            if not names:
                names = zf.namelist()
            if not names:
                return None
            return zf.read(names[0]).decode("utf-8", errors="replace").strip()
    except (zipfile.BadZipFile, Exception):
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  ACCOUNT INDEX
#
#  _index.json is a local cache of every speech in the Otter account.
#  On first run it is built by paging through the full API.
#  On subsequent runs only new speeches are fetched (incremental update).
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_source(session: requests.Session, userid: str,
                  source: str, existing_ids: set,
                  full: bool, verbose: bool
                  ) -> Tuple[dict, requests.Session, str]:
    """
    Page through all speeches for one *source* ("owned" or "shared").

    Otter's API uses a dual-cursor approach for pagination:
      • last_load_ts   — taken from the "last_load_ts" field in the response
      • modified_after — taken from the "last_modified_at" field in the response
    Both must be sent back on the next request to advance the page.

    In incremental mode we stop early once a full page consists entirely of
    speeches we already know about.
    """
    result         = {}
    last_load_ts   = None
    modified_after = None
    page           = 0

    label = "full fetch" if full else "checking for new speeches"
    print(f"\n   [{source}] {label}…")

    while True:
        page += 1
        params: dict = {
            "userid":    userid,
            "folder":    0,
            "page_size": PAGE_SIZE,
            "source":    source,
        }
        if last_load_ts:
            params["last_load_ts"]   = last_load_ts
        if modified_after:
            params["modified_after"] = modified_after

        if verbose:
            # Omit credentials from output even in verbose mode.
            safe = {k: v for k, v in params.items()
                    if k not in ("userid",)}
            print(f"      [verbose] page {page} params: {safe}")

        resp = session.get(API_BASE + "speeches", params=params)

        if resp.status_code in (401, 403):
            session, userid = reauth(session)
            resp = session.get(API_BASE + "speeches", params=params)
            if resp.status_code != 200:
                print(f"   ❌ Auth failure after re-auth — stopping {source}")
                break

        if resp.status_code != 200:
            print(f"   ❌ HTTP {resp.status_code} on page {page} — "
                  f"{resp.text[:120].strip()}")
            break

        data = resp.json()

        if verbose:
            print(f"      [verbose] keys={list(data.keys())}  "
                  f"end_of_list={data.get('end_of_list')}  "
                  f"last_load_ts={data.get('last_load_ts')}  "
                  f"last_modified_at={data.get('last_modified_at')}")

        speeches       = data.get("speeches", [])
        end_of_list    = data.get("end_of_list", True)
        last_load_ts   = data.get("last_load_ts")
        modified_after = data.get("last_modified_at")  # NOTE: different key name

        new_on_page = 0
        for sp in speeches:
            otid = sp.get("otid") or sp.get("speech_id", "")
            sid  = sp.get("speech_id", otid)
            if sid and sid not in existing_ids:
                result[sid] = sp
                new_on_page += 1

        print(f"      page {page:>3}: {len(speeches):>3} fetched, "
              f"{new_on_page:>3} new  (running total new: {len(result)})")

        if end_of_list or not speeches:
            break
        if new_on_page == 0 and not full:
            print("      (all speeches on this page already known — "
                  "stopping incremental fetch)")
            break
        if len(speeches) < PAGE_SIZE:
            break
        if page > 500:
            print("   ⚠️  Safety limit: stopped after 500 pages.")
            break

        time.sleep(SLEEP_PAGE)

    return result, session, userid


def load_index(session: requests.Session, userid: str,
               index_file: Path, force_full: bool = False,
               verbose: bool = False
               ) -> Tuple[dict, requests.Session, str]:
    """
    Load _index.json if it exists and is marked complete; otherwise (or when
    *force_full* is True) rebuild it from the API.  Returns (speeches, session,
    userid).
    """
    speeches: dict = {}
    meta: dict     = {"complete": False, "last_check": None, "count": 0}

    if not force_full and index_file.exists():
        try:
            raw = json.loads(index_file.read_text(encoding="utf-8"))
            if raw.get("_meta", {}).get("complete"):
                speeches = {k: v for k, v in raw.items() if k != "_meta"}
                meta     = raw["_meta"]
                print(f"📖 Loaded index: {len(speeches):,} speeches  "
                      f"(last checked {meta.get('last_check', '?')})")
            else:
                print("⚠️  Index is incomplete — doing a full fetch.")
                force_full = True
        except (json.JSONDecodeError, OSError):
            print("⚠️  Index file is unreadable — doing a full fetch.")
            force_full = True
    elif force_full:
        print("🔄 --full-index: rebuilding index from API.")
    else:
        print("📋 No index found — building from API (one-time operation).")

    existing_ids = set(speeches.keys())
    total_new    = 0

    for source in ("owned", "shared"):
        new, session, userid = _fetch_source(
            session, userid, source, existing_ids,
            full=force_full, verbose=verbose,
        )
        speeches.update(new)
        existing_ids.update(new.keys())
        total_new += len(new)

    if total_new:
        print(f"\n   ✅ {total_new:,} new speech(es) added  "
              f"(total: {len(speeches):,})")
    else:
        print(f"\n   ✅ Index is up to date — {len(speeches):,} speeches total.")

    # Persist the updated index.
    meta["complete"]   = True
    meta["last_check"] = datetime.now().isoformat(timespec="seconds")
    meta["count"]      = len(speeches)
    payload            = dict(speeches)
    payload["_meta"]   = meta
    index_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return speeches, session, userid


# ═══════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD LOG  (_downloaded.csv)
#
#  Append-only CSV that records every transcript written to disk.
#  On first run it is bootstrapped by scanning existing .txt files for the
#  "URL: https://otter.ai/u/<id>" header line so we don't re-download files
#  from a previous run of an older script version.
# ═══════════════════════════════════════════════════════════════════════════════

def _read_speech_id_from_file(filepath: Path) -> Optional[str]:
    """Extract the Otter speech ID from the URL: header in a transcript file."""
    try:
        if filepath.stat().st_size < 100:
            return None
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for _ in range(12):
                line = f.readline()
                if not line:
                    break
                m = re.search(r"otter\.ai/u/([A-Za-z0-9_\-]+)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def _bootstrap_download_log(output_dir: Path) -> dict:
    """
    Scan existing .txt files and build a download log from them.
    Called automatically when _downloaded.csv doesn't exist yet.
    """
    txt_files = [f for f in output_dir.glob("*.txt")
                 if not f.name.startswith("_")]
    if not txt_files:
        return {}

    print(f"\n🔍 First run — scanning {len(txt_files):,} existing .txt files…")
    downloaded: dict = {}
    no_id = 0
    for fp in txt_files:
        sid = _read_speech_id_from_file(fp)
        if sid:
            downloaded[sid] = {
                "speech_id":    sid,
                "otid":         sid,
                "filename":     fp.name,
                "title":        "",
                "date":         "",
                "url":          f"https://otter.ai/u/{sid}",
                "downloaded_at": "",
            }
        else:
            no_id += 1

    if no_id:
        print(f"   ⚠️  {no_id} file(s) had no URL header and were skipped")
    print(f"   ✅ Found {len(downloaded):,} existing transcripts")
    _append_to_download_log(list(downloaded.values()), output_dir)
    return downloaded


def _append_to_download_log(rows: list, output_dir: Path):
    df        = output_dir / "_downloaded.csv"
    write_hdr = not df.exists()
    with open(df, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=DOWNLOADED_FIELDS, extrasaction="ignore"
        )
        if write_hdr:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def load_downloaded(output_dir: Path) -> dict:
    """Load _downloaded.csv, bootstrapping from existing files if needed."""
    df = output_dir / "_downloaded.csv"
    if not df.exists():
        return _bootstrap_download_log(output_dir)

    downloaded: dict = {}
    try:
        with open(df, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sid = (row.get("speech_id") or "").strip()
                if sid:
                    downloaded[sid] = row
        print(f"📥 Download log: {len(downloaded):,} transcripts already on disk")
    except (OSError, csv.Error) as e:
        print(f"⚠️  Could not read _downloaded.csv ({e}) — scanning files instead")
        return _bootstrap_download_log(output_dir)

    return downloaded


def _record_downloaded(speech: dict, filename: str, output_dir: Path):
    otid      = speech.get("otid") or speech.get("speech_id", "")
    speech_id = speech.get("speech_id", otid)
    try:
        date_str = datetime.fromtimestamp(
            float(speech.get("created_at") or 0)
        ).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        date_str = ""

    _append_to_download_log([{
        "speech_id":     speech_id,
        "otid":          otid,
        "filename":      filename,
        "title":         speech.get("title") or "",
        "date":          date_str,
        "url":           f"https://otter.ai/u/{speech_id}",
        "downloaded_at": datetime.now().isoformat(timespec="seconds"),
    }], output_dir)


# ═══════════════════════════════════════════════════════════════════════════════
#  ERROR LOG  (_errors.csv)
#
#  Written immediately whenever a download fails so it survives interruption.
#  On --retry mode this file is read back to build the retry list.
# ═══════════════════════════════════════════════════════════════════════════════

def load_errors(output_dir: Path) -> dict:
    errors: dict = {}
    ef = output_dir / "_errors.csv"
    if not ef.exists():
        return errors
    try:
        with open(ef, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sid = (row.get("speech_id") or "").strip()
                if sid:
                    errors[sid] = row
        print(f"⚠️  Error log: {len(errors):,} previous failure(s) in _errors.csv")
    except (OSError, csv.Error) as e:
        print(f"⚠️  Could not read _errors.csv: {e}")
    return errors


def save_errors(errors: dict, output_dir: Path):
    """Rewrite _errors.csv in full.  Deletes the file if errors is empty."""
    ef = output_dir / "_errors.csv"
    if not errors:
        if ef.exists():
            ef.unlink()
        return
    with open(ef, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=ERRORS_FIELDS, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(errors.values())


# ═══════════════════════════════════════════════════════════════════════════════
#  FILENAME HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _make_stem(title: str, date_str: str) -> str:
    """Build a filesystem-safe filename stem: "<date>_<title>"."""
    clean = re.sub(r'[\\/*?:"<>|]', "", title).strip()
    clean = re.sub(r"\s+", " ", clean)
    return f"{date_str}_{clean or 'Untitled'}"


def find_filepath(output_dir: Path, title: str, date_str: str,
                  speech_id: str, downloaded: dict) -> Optional[Path]:
    """
    Determine the output path for a transcript.

    Returns None if the transcript is already on disk (skip it).
    If the natural filename is taken by a different transcript, appends _2,
    _3, … until a free name is found.
    Falls back to appending the first 8 characters of the speech ID if the
    counter would exceed 500.
    """
    if speech_id in downloaded:
        return None

    stem = _make_stem(title, date_str)
    for counter in range(1, 501):
        suffix   = f"_{counter}" if counter > 1 else ""
        filepath = output_dir / f"{stem}{suffix}.txt"
        if not filepath.exists():
            return filepath
        if _read_speech_id_from_file(filepath) == speech_id:
            return None  # already on disk under this exact name

    return output_dir / f"{stem}_{speech_id[:8]}.txt"


# ═══════════════════════════════════════════════════════════════════════════════
#  CONTENT VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

def _is_valid_content(text: str, min_words: int) -> bool:
    """
    Return True if *text* looks like a real transcript.

    Checks that the body (everything after the "===…===" separator in the
    header) contains at least *min_words* words.
    """
    if not text or len(text) < 50:
        return False
    sep  = text.find("=" * 10)
    body = text[sep + 10:] if sep != -1 else text
    return len(body.split()) >= min_words


# ═══════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD — INDIVIDUAL TRANSCRIPT
# ═══════════════════════════════════════════════════════════════════════════════

def _do_download(session: requests.Session, userid: str,
                 otid: str) -> Tuple[Optional[bytes], int, str]:
    """
    POST to bulk_export for a single speech.

    Returns (raw_bytes, http_status, snippet).
      • raw_bytes is the raw response body (may be a ZIP; caller handles it).
      • raw_bytes is None on failure; http_status and snippet explain why.
    """
    csrf    = session.cookies.get("csrftoken", "")
    params  = {
        "userid":                      userid,
        "speaker_names":               1,
        "speaker_timestamps":          1,
        "merge_same_speaker_segments": 0,
        "show_highlights":             0,
        "inline_pictures":             0,
        "monologue":                   0,
        "highlight_only":              0,
        "branding":                    "false",
        "annotations":                 0,
    }
    payload = {"formats": "txt", "speech_otid_list": [otid]}
    headers = {"x-csrftoken": csrf, "Referer": "https://otter.ai/"}

    try:
        resp = session.post(
            API_BASE + "bulk_export",
            params=params, headers=headers, data=payload,
            timeout=DL_TIMEOUT,
        )
        snippet = resp.text[:120].strip() if resp.text else ""
        if resp.status_code == 200 and len(resp.content) > 50:
            return resp.content, 200, ""
        return None, resp.status_code, snippet
    except requests.exceptions.Timeout:
        return None, 0, "request timed out"
    except requests.RequestException as e:
        return None, 0, str(e)[:120]


def _speech_fallback(session: requests.Session, userid: str,
                     otid: str, speech_id: str
                     ) -> Tuple[Optional[str], int, str]:
    """
    Fallback path: reconstruct the transcript by fetching raw segment data
    from the speech/ endpoint.  Returns (text_or_none, status, snippet).
    """
    try:
        r = session.get(
            API_BASE + "speech",
            params={"userid": userid, "otid": otid, "speech_id": speech_id},
            timeout=DL_TIMEOUT,
        )
        snippet = r.text[:120].strip() if r.text else ""
        if r.status_code != 200:
            return None, r.status_code, snippet

        data   = r.json()
        speech = data.get("speech") or data
        segs   = (speech.get("transcripts") or speech.get("transcript")
                  or speech.get("segments") or [])
        if not segs:
            return None, 200, "no transcript segments in response"

        lines = []
        for seg in segs:
            speaker = seg.get("speaker_name") or seg.get("speaker") or "Speaker"
            words   = seg.get("words") or seg.get("transcript") or ""
            if isinstance(words, list):
                words = " ".join(
                    w.get("word", "") for w in words if isinstance(w, dict)
                )
            ts = seg.get("start_offset") or seg.get("start") or 0
            try:
                mins, secs = divmod(int(float(ts)), 60)
                stamp = f"{mins}:{secs:02d}"
            except (TypeError, ValueError):
                stamp = "0:00"
            if words.strip():
                lines.append(f"{speaker} ({stamp}): {words.strip()}")

        if not lines:
            return None, 200, "segments present but no words extracted"
        return "\n".join(lines), 200, ""

    except Exception as e:
        return None, 0, str(e)[:120]


def download_one(session: requests.Session, userid: str,
                 speech: dict, output_dir: Path, downloaded: dict,
                 min_words: int = MIN_WORDS
                 ) -> Tuple[str, requests.Session, str]:
    """
    Download and save a single transcript.

    Returns ('ok' | 'skip' | 'fail:<reason>', session, userid).

    Handles three bulk_export response formats:
      1. ZIP archive (PK magic bytes) containing a .txt file — most common.
      2. Plain UTF-8 text — used by some older recordings.
      3. Empty / too short — falls through to the speech/ endpoint fallback.
    """
    title      = speech.get("title") or "Untitled"
    otid       = speech.get("otid") or speech.get("speech_id", "")
    speech_id  = speech.get("speech_id", otid)
    created_at = speech.get("created_at") or speech.get("start_time") or 0

    try:
        date_str = datetime.fromtimestamp(float(created_at)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        date_str = "0000-00-00"

    try:
        dur    = int(float(speech.get("duration") or 0))
        h, rem = divmod(dur, 3600)
        m, s   = divmod(rem, 60)
        dur_str: Optional[str] = f"{h}h {m}m" if h else f"{m}m {s}s"
    except (TypeError, ValueError):
        dur_str = None

    filepath = find_filepath(output_dir, title, date_str, speech_id, downloaded)
    if filepath is None:
        return "skip", session, userid

    # ── Attempt bulk_export with retries ──────────────────────────────────────
    raw_bytes    = None
    last_status  = 0
    last_snippet = ""

    for attempt in range(MAX_DL_RETRIES):
        raw_bytes, status, snippet = _do_download(session, userid, otid)

        if raw_bytes:
            break

        last_status  = status
        last_snippet = snippet
        print(f"      ⚠️  Attempt {attempt + 1} failed — "
              f"HTTP {status}  {snippet[:80]}")

        if status in (401, 403):
            # Auth expired — re-authenticate and retry immediately.
            try:
                session, userid = reauth(session)
            except Exception as e:
                print(f"      ⚠️  Re-auth failed: {e}")
            continue

        if attempt < MAX_DL_RETRIES - 1:
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            print(f"      ↩️  Waiting {wait}s…")
            time.sleep(wait)

    # ── Decode the response — ZIP or plain text ───────────────────────────────
    content_str: Optional[str]  = None
    content_note                = ""

    if raw_bytes:
        if raw_bytes[:2] == b'PK':
            # ZIP archive — extract the .txt inside.
            extracted = extract_text_from_zip(raw_bytes)
            if extracted:
                content_str  = extracted
                content_note = " (extracted from ZIP)"
            else:
                last_snippet = "bulk_export returned a ZIP but no text could be extracted"
        else:
            # Plain text response.
            decoded = raw_bytes.decode("utf-8", errors="replace").strip()
            if _is_valid_content(decoded, min_words):
                content_str = decoded
            else:
                last_snippet = (f"bulk_export returned plain text but only "
                                f"{len(decoded.split())} words (min={min_words})")

    # ── Fallback: reconstruct from speech/ segment data ───────────────────────
    if not content_str:
        print("      🔄 Trying speech/ fallback…")
        fb_text, fb_status, fb_snippet = _speech_fallback(
            session, userid, otid, speech_id
        )
        if fb_text and _is_valid_content(fb_text, min_words):
            content_str = fb_text
        else:
            parts = []
            if last_snippet:
                parts.append(f"bulk_export HTTP {last_status}: {last_snippet}")
            if fb_snippet:
                parts.append(f"speech/ HTTP {fb_status}: {fb_snippet}")
            last_snippet = " | ".join(parts) if parts else "both methods failed"

    if not content_str:
        return f"fail:{last_snippet}", session, userid

    # ── Write the transcript file ─────────────────────────────────────────────
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"Title:    {title}\n")
        f.write(f"Date:     {date_str}\n")
        if dur_str:
            f.write(f"Duration: {dur_str}\n")
        f.write(f"URL:      https://otter.ai/u/{speech_id}\n")
        f.write(f"\n{'=' * 70}\n\n")
        f.write(content_str)

    # Set the file modification time to match the recording date.
    if created_at:
        try:
            ts = float(created_at)
            os.utime(filepath, (ts, ts))
        except (TypeError, ValueError):
            pass

    print(f"   ✅ saved{content_note}")
    _record_downloaded(speech, filepath.name, output_dir)
    downloaded[speech_id] = {"speech_id": speech_id, "filename": filepath.name}
    return "ok", session, userid


# ═══════════════════════════════════════════════════════════════════════════════
#  BATCH RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

def run_batch(session: requests.Session, userid: str,
              speeches: list, output_dir: Path,
              downloaded: dict, errors: dict,
              min_words: int = MIN_WORDS,
              run_label: str = ""
              ) -> Tuple[int, int, int, requests.Session, str]:
    """
    Download *speeches* one by one with progress reporting.

    Errors are written to _errors.csv immediately after each failure so that
    the log is accurate even if the run is interrupted (Ctrl-C).
    """
    total         = len(speeches)
    ok = skip = fail = 0

    for i, speech in enumerate(speeches, 1):
        title     = speech.get("title") or "Untitled"
        otid      = speech.get("otid") or speech.get("speech_id", "")
        speech_id = speech.get("speech_id", otid)
        try:
            date_str = datetime.fromtimestamp(
                float(speech.get("created_at") or 0)
            ).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            date_str = "0000-00-00"

        tag = f"[{run_label}{i}/{total}]"
        print(f"{tag} {title[:58]}")

        result, session, userid = download_one(
            session, userid, speech, output_dir, downloaded,
            min_words=min_words,
        )

        if result == "ok":
            ok += 1
            errors.pop(speech_id, None)

        elif result == "skip":
            skip += 1
            errors.pop(speech_id, None)
            print("   ⏭  already downloaded")

        else:
            fail += 1
            reason = result[5:] if result.startswith("fail:") else result
            errors[speech_id] = {
                "speech_id": speech_id,
                "otid":      otid,
                "title":     title,
                "date":      date_str,
                "url":       f"https://otter.ai/u/{speech_id}",
                "reason":    reason,
            }
            print(f"   ❌ failed — {reason[:80]}")
            save_errors(errors, output_dir)  # write immediately — survives Ctrl-C

        if i % 25 == 0:
            print(f"   💾 checkpoint [{i}/{total}] — "
                  f"{ok} saved, {skip} skipped, {fail} failed")

        time.sleep(SLEEP_BETWEEN)

    return ok, skip, fail, session, userid


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="otter_export",
        description=(
            "Download all your Otter.ai transcripts as plain-text files.\n\n"
            "On first run the script builds a full account index (~1 min for "
            "large accounts) then downloads every transcript.  Subsequent "
            "runs are incremental — only new transcripts are fetched."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  %(prog)s me@example.com\n"
            "  %(prog)s me@example.com --retry\n"
            "  %(prog)s me@example.com --output-dir ~/Documents/Otter\n"
        ),
    )
    parser.add_argument(
        "email", nargs="?",
        help="Otter.ai account email (prompted interactively if omitted)",
    )
    parser.add_argument(
        "password", nargs="?",
        help="Otter.ai account password (prompted interactively if omitted)",
    )
    parser.add_argument(
        "--output-dir", default=None, metavar="DIR",
        help="Directory to save transcripts.  "
             "Default: OtterImport/ next to this script.",
    )
    parser.add_argument(
        "--retry", action="store_true",
        help="Re-attempt only the transcripts listed in _errors.csv.",
    )
    parser.add_argument(
        "--full-index", action="store_true",
        help="Discard the cached index and rebuild it from scratch.",
    )
    parser.add_argument(
        "--min-words", type=int, default=MIN_WORDS, metavar="N",
        help=f"Minimum word count for a transcript to be saved.  "
             f"Default: {MIN_WORDS}.  "
             f"Lower this (e.g. --min-words 3) to capture very short clips.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print extra diagnostic output (API keys, pagination cursors).",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print("=" * 60)
    print("  Otter.ai Transcript Exporter")
    print("=" * 60)

    # Collect credentials — prompt if not provided on the command line.
    username = args.email    or input("Otter.ai email:    ").strip()
    password = args.password or input("Otter.ai password: ").strip()

    # Resolve output directory.
    if args.output_dir:
        output_dir = Path(args.output_dir).expanduser()
    else:
        output_dir = Path(__file__).parent / "OtterImport"
    output_dir.mkdir(parents=True, exist_ok=True)

    index_file = output_dir / "_index.json"

    print(f"\n📁 Output directory: {output_dir}\n")

    session, userid        = make_session(username, password)
    downloaded             = load_downloaded(output_dir)
    index, session, userid = load_index(
        session, userid, index_file,
        force_full=args.full_index, verbose=args.verbose,
    )
    errors                 = load_errors(output_dir)

    if not index:
        sys.exit("❌ Account index is empty — please check your credentials.")

    # ── RETRY MODE ────────────────────────────────────────────────────────────
    if args.retry:
        if not errors:
            print("\n✅ _errors.csv is empty — nothing to retry.")
            return

        to_retry: list     = []
        unresolvable: list = []

        for sid in list(errors.keys()):
            if sid in downloaded:
                errors.pop(sid)
                continue
            sp = index.get(sid)
            if sp:
                to_retry.append(sp)
            else:
                unresolvable.append(sid)

        if unresolvable:
            print(f"\n⚠️  {len(unresolvable)} failed transcript(s) "
                  f"no longer appear in your account index:")
            for sid in unresolvable:
                print(f"   {errors[sid].get('title', '?')}  [{sid}]")

        if not to_retry:
            print("\n✅ All previously failed transcripts are now on disk.")
            save_errors(errors, output_dir)
            return

        print(f"\n🔄 Retrying {len(to_retry)} transcript(s)…\n")
        ok, skip, fail, session, userid = run_batch(
            session, userid, to_retry, output_dir,
            downloaded, errors,
            min_words=args.min_words, run_label="retry ",
        )

    # ── NORMAL MODE ───────────────────────────────────────────────────────────
    else:
        gap = [sp for sid, sp in index.items() if sid not in downloaded]

        print(f"\n📊 Status:")
        print(f"   In your Otter account : {len(index):>6,}")
        print(f"   Already on disk       : {len(downloaded):>6,}")
        print(f"   To download now       : {len(gap):>6,}")
        if errors:
            print(f"   Previous failures     : {len(errors):>6,}  "
                  f"(run with --retry after this finishes)")

        if not gap:
            print("\n✅ All transcripts are already on disk!")
            save_errors(errors, output_dir)
            return

        print(f"\n⬇️  Downloading {len(gap):,} transcript(s)…\n")
        ok, skip, fail, session, userid = run_batch(
            session, userid, gap, output_dir,
            downloaded, errors, min_words=args.min_words,
        )

    save_errors(errors, output_dir)

    # ── SUMMARY ───────────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    if args.retry:
        print(f"  Retry complete —  Recovered: {ok}  "
              f"Already had: {skip}  Still failing: {fail}")
    else:
        print(f"  Run complete —  Downloaded: {ok}  "
              f"Already had: {skip}  Failed: {fail}")

    if errors:
        ef = output_dir / "_errors.csv"
        print(f"\n⚠️  {len(errors)} transcript(s) could not be downloaded.")
        print(f"   Review : {ef}")
        print(f"   Retry  : python {Path(__file__).name} [email] --retry")
    else:
        print("\n🎉 No failures!")

    print(f"\n📁 Transcripts saved to: {output_dir}")
    print("=" * 60)


if __name__ == "__main__":
    main()
