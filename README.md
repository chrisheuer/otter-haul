# otter-haul

Download your [Otter.ai](https://otter.ai) library: every transcript as plain text and, with v2, Otter's AI summaries and the original audio.

Built for people who want a local backup of years of meeting recordings, interviews and voice notes, without clicking through the Otter UI one transcript at a time.

New here? **[HOWTO.md](HOWTO.md)** walks through setup, the first run and keeping it up to date.

---

## Features

- **Downloads your entire Otter library**: owned and shared transcripts
- **AI summaries** (`--summaries`): Otter's summary, action items and outline, as Markdown
- **Audio** (`--audio`): the original recording, as `.mp3`
- **Filters**: `--since YYYY-MM-DD` for a period, `--match REGEX` for one series ("weekly standup", a client name)
- **Keychain login on macOS** (`--keychain SERVICE`): no password prompt and nothing in shell history. `OTTER_PASSWORD` in the environment also works
- **Resumable**: safe to stop and restart; files already on disk are skipped
- **Incremental**: after the first run, only new recordings are fetched
- **Backfill**: with `--summaries` or `--audio`, transcripts you already have get their summary and audio added
- **Persistent logs**: `_downloaded.csv` and `_errors.csv` survive interruptions
- **Retry mode**: re-attempt any failures with one flag
- **Collision-safe filenames**: duplicate titles get `_2`, `_3`, … suffixes

### Versions

| Script | What it does |
|---|---|
| `otter_haul_v2.py` | **Current.** Transcripts, plus optional summaries, audio, filters and Keychain login |
| `otter_haul_v1.py` | The original release: transcripts only. Kept unchanged for anyone scripting against it |

v2 also fixes a v1 crash: an audio download sets a second `csrftoken` cookie, and v1's cookie lookup raised `CookieConflictError` on the next transcript.

---

## Requirements

- Python 3.8 or later
- The `requests` library

```bash
pip install -r requirements.txt
```

---

## Usage

```bash
# Everything: transcripts only (prompts for email and password)
python otter_haul_v2.py

# Store the password once in the macOS Keychain, then never type it again
security add-generic-password -s otter-haul -a me@example.com -w
python otter_haul_v2.py me@example.com --keychain otter-haul

# Transcripts, summaries and audio for everything since September
python otter_haul_v2.py me@example.com --keychain otter-haul --since 2026-09-01 --summaries --audio

# One recurring series across your whole history, with audio
python otter_haul_v2.py me@example.com --keychain otter-haul --match "weekly standup" --audio

# Save to a specific folder
python otter_haul_v2.py me@example.com --output-dir ~/Documents/Otter

# Retry anything that failed on the previous run
python otter_haul_v2.py me@example.com --retry

# Capture very short transcripts (default minimum is 15 words)
python otter_haul_v2.py me@example.com --retry --min-words 3

# Force a full rebuild of the account index
python otter_haul_v2.py me@example.com --full-index
```

Run `python otter_haul_v2.py --help` for the full option reference.

### All options

| Option | Meaning |
|---|---|
| `email`, `password` | Otter.ai login. Omit them to be prompted (the password prompt is hidden) |
| `--keychain SERVICE` | macOS: read the password from this Keychain service |
| `--output-dir DIR` | Where to save everything. Default: `OtterImport/` next to the script |
| `--since YYYY-MM-DD` | Only recordings created on or after this date |
| `--match REGEX` | Only recordings whose title matches (case-insensitive) |
| `--summaries` | Also save `<date>_<title>.summary.md` |
| `--audio` | Also save `<date>_<title>.mp3` |
| `--retry` | Re-attempt only what's listed in `_errors.csv` |
| `--full-index` | Discard the cached account index and rebuild it |
| `--min-words N` | Skip transcripts shorter than N words (default 15) |
| `--verbose` | Extra diagnostics (pagination cursors, API keys) |

---

## Output

```
OtterImport/
├── 2024-03-15_Team standup.txt
├── 2024-03-15_Team standup.summary.md   ← with --summaries
├── 2024-03-15_Team standup.mp3          ← with --audio
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

Each `.summary.md` holds Otter's AI summary, its action items as a checklist and the outline, under `## Summary`, `## Action items` and `## Outline`. A section is left out when Otter has nothing for it; older recordings often have no AI summary at all.

Audio runs about **11 MB per recorded hour**, so a large library can be several GB. Text is tiny by comparison.

---

## How it works

Otter.ai's web app talks to an internal REST API at `https://otter.ai/forward/api/v1/`. This script uses the same endpoints the browser uses.

- **Authentication**: HTTP Basic Auth (email and password) on every request, plus a call to `/login` for the `userid` the other calls need.
- **Pagination**: each page returns a `last_load_ts` and a `last_modified_at`. Both go back as `last_load_ts` and `modified_after` on the next request.
- **Transcripts**: `bulk_export` returns a ZIP archive. The script detects the `PK` magic bytes, unzips in memory and extracts the `.txt`. When `bulk_export` returns nothing, a fallback call to `speech/` rebuilds the transcript from raw segments.
- **Summaries** (v2): `abstract_summary` returns the AI summary, `speech_action_items` the action items, and the outline comes with each recording in the account index.
- **Audio** (v2): each recording in the index carries a `download_url`. The file streams to a `.part` file and is renamed only when complete, so an interrupted download never looks finished.

---

## What to expect on first run

For large accounts (1,000+ recordings) the first run takes a while:

- **Index build**: several minutes (pages through your account at 50 recordings per request)
- **Transcripts**: ~30–40 minutes for 2,000 (a 1.5-second courtesy delay between files avoids rate limits)
- **Audio**: add the download time for the files themselves

Later runs are fast: only new recordings are fetched.

---

## Failures and retries

Some transcripts can't be downloaded:

| Reason in `_errors.csv` | Meaning |
|---|---|
| `no transcript segments in response` | Otter has no transcript for this recording: it started but was never processed. Nothing more can be done. |
| `plain text but only N words` | The transcript exists but is very short. Re-run with `--min-words 3` to capture it. |
| `bulk_export returned a ZIP but no text could be extracted` | Empty ZIP, same as above. |

Summary and audio problems are printed as warnings (`⚠️ summary.md: …`, `⚠️ mp3: …`) and don't stop the run. Run the same command again to fill them in.

After a normal run, check for failures and retry:

```bash
python otter_haul_v2.py me@example.com --retry
```

---

## Notes and caveats

- **Unofficial API**: these are the internal endpoints the Otter.ai web app uses, not an official integration. Otter could change them without notice.
- **Rate limiting**: the 1.5-second delay between downloads is intentional. Don't cut it much, or you risk being throttled.
- **Shared transcripts**: both `owned` and `shared` recordings are fetched, so recordings others shared with you are included.
- **Your data stays yours**: `.gitignore` excludes every output file (transcripts, summaries, audio, logs), so a fork can't commit someone's library by accident.

---

## Credits

Otter Haul was built by Chris Heuer ([GitHub](https://github.com/guruhuey) · [LinkedIn](https://linkedin.com/in/chrisheuer)) using Claude CoWork and Claude Code (Anthropic).

---

## License

MIT. Do whatever you like with it. Attribution requested.
