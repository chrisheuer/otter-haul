# otter-haul

Download all your [Otter.ai](https://otter.ai) transcripts as plain-text `.txt` files.

Built for people who want a local backup of years of meeting recordings, interviews, and voice notes — without clicking through the Otter UI one transcript at a time.

---

## Features

- **Downloads your entire Otter library** — owned and shared transcripts
- **Resumable** — safe to stop and restart; already-downloaded files are skipped
- **Incremental** — after the first run, only new transcripts are fetched
- **Persistent logs** — `_downloaded.csv` and `_errors.csv` survive interruptions
- **Retry mode** — re-attempt any failures with a single flag
- **Collision-safe filenames** — duplicate titles get `_2`, `_3`, … suffixes
- **Handles both response formats** — Otter returns transcripts as ZIP archives; the script unzips them automatically

---

## Requirements

- Python 3.8 or later
- The `requests` library

```bash
pip install requests
```

---

## Usage

```bash
# Download everything (first run builds the index, then downloads all transcripts)
python otter_export_v10.py me@example.com

# Omit credentials to be prompted interactively (safer — avoids shell history)
python otter_export_v10.py

# Save to a specific folder
python otter_export_v10.py me@example.com --output-dir ~/Documents/Otter

# Retry any transcripts that failed on the previous run
python otter_export_v10.py me@example.com --retry

# Capture very short transcripts (default minimum is 15 words)
python otter_export_v10.py me@example.com --retry --min-words 3

# Force a full rebuild of the account index
python otter_export_v10.py me@example.com --full-index

# Show extra diagnostic output (pagination cursors, API response keys)
python otter_export_v10.py me@example.com --verbose
```

Run `python otter_export_v10.py --help` for the full option reference.

---

## Output

All files are saved in an `OtterImport/` folder next to the script (or the directory you specify with `--output-dir`).

```
OtterImport/
├── 2024-03-15_Team standup.txt
├── 2024-03-18_Interview with Jana.txt
├── ...
├── _index.json        ← local cache of your full account index
├── _downloaded.csv    ← append-only log of every transcript saved
└── _errors.csv        ← failures from the most recent run (if any)
```

Each `.txt` file starts with a short header:

```
Title:    Interview with Jana
Date:     2024-03-18
Duration: 42m 7s
URL:      https://otter.ai/u/XXXXXXXXXXXXXXXX

======================================================================

Jana (0:12): So tell me about your background…
Chris (1:04): Sure, I started…
```

---

## How it works

Otter.ai's web app communicates with an internal REST API at `https://otter.ai/forward/api/v1/`. This script uses the same API endpoints the browser uses.

**Authentication** uses HTTP Basic Auth (email + password) on every request, plus a call to `/login` to retrieve the `userid` required for subsequent calls.

**Pagination** uses a dual-cursor approach: each page response returns both a `last_load_ts` value and a `last_modified_at` value. Both must be sent back as `last_load_ts` and `modified_after` parameters on the next request.

**Download** calls `bulk_export` which returns a ZIP archive. The script detects the `PK` magic bytes, unzips in memory, and extracts the `.txt` file inside. For transcripts where `bulk_export` returns no content, a fallback call to the `speech/` endpoint attempts to reconstruct the transcript from raw segment data.

---

## What to expect on first run

For large accounts (1,000+ transcripts) the first run takes a while:

- **Index build**: several minutes (pages through your full account at 50 speeches/request)
- **Downloads**: ~30–40 minutes for 2,000 transcripts (1.5-second courtesy delay between files to avoid rate-limiting)

Subsequent runs are fast — only new transcripts are downloaded.

---

## Failures and retries

Some transcripts cannot be downloaded:

| Reason in `_errors.csv` | Meaning |
|---|---|
| `no transcript segments in response` | Otter has no transcript data for this recording — it was started but never fully processed. Nothing more can be done. |
| `plain text but only N words` | The transcript exists but is very short. Re-run with `--min-words 3` to capture it. |
| `bulk_export returned a ZIP but no text could be extracted` | Empty ZIP — same as above. |

After a normal run, always check for failures and retry:

```bash
python otter_haul_v1.py me@example.com --retry
```

---

## Notes and caveats

- **Unofficial API** — This uses the same internal endpoints as the Otter.ai web app. It is not an official integration and could break if Otter changes their API.
- **Rate limiting** — The 1.5-second delay between downloads is intentional. Don't reduce it significantly or you risk being throttled.
- **Shared transcripts** — The script fetches both `owned` and `shared` sources, so transcripts shared with you by others will also be included.
- **No audio** — Only the text transcript is downloaded, not the original audio recording.

---

## Credits

Otter Haul was built by [Chris Heuer](https://github.com/guruhuy and https://linkedin.com/in/chrisheuer) using Claude CoWork (Anthropic).

---

## License

MIT — do whatever you like with it. Attribution requested.
# otter-haul
