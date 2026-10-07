# How to use otter-haul

A step-by-step guide, from nothing installed to a library that keeps itself up to date. For what every option does, see the [README](README.md).

---

## 1. Get the code

```bash
git clone https://github.com/guruhuey/otter-haul.git
cd otter-haul
pip install -r requirements.txt
```

You need Python 3.8 or later. Check with `python3 --version`.

## 2. Make sure you can log in with a password

otter-haul logs in with your Otter **email and password**. If you normally sign in with "Continue with Google" or Microsoft, set a password first: in Otter, open your account settings and add one, or use "Forgot password" on the sign-in page to create one. Then confirm it works by signing in at otter.ai in a private browser window.

## 3. Store the password (macOS, recommended)

```bash
security add-generic-password -s otter-haul -a you@example.com -w
```

It prompts for the password twice and saves it in your Keychain. After that, pass `--keychain otter-haul` and you'll never type it again.

- **Changed your Otter password?** Overwrite the stored one with `-U`:
  `security add-generic-password -U -s otter-haul -a you@example.com -w`
- **Not on a Mac?** Set `OTTER_PASSWORD` in the environment for the run, or leave everything out and the script prompts (the input is hidden).

## 4. Do a small test run

Before pulling years of recordings, try one week:

```bash
python otter_haul_v2.py you@example.com --keychain otter-haul \
  --since 2026-09-28 --summaries --audio --output-dir ~/Otter
```

The first run builds an index of your whole account, which takes a few minutes for a large library. Then it downloads only what matches. Open `~/Otter` and check that one recording has its `.txt`, `.summary.md` and `.mp3`.

## 5. Pull everything

Choose what you want:

```bash
# All transcripts (text only, fastest)
python otter_haul_v2.py you@example.com --keychain otter-haul --output-dir ~/Otter

# All transcripts, summaries and audio
python otter_haul_v2.py you@example.com --keychain otter-haul --output-dir ~/Otter --summaries --audio

# Just one series, across your whole history
python otter_haul_v2.py you@example.com --keychain otter-haul --output-dir ~/Otter --match "board meeting" --audio
```

Audio is about 11 MB per recorded hour, so check your disk space before pulling a large library with `--audio`.

You can stop it at any time (Ctrl-C) and run the same command again. It picks up where it left off.

## 6. Already have transcripts? Add summaries and audio to them

Run against the same `--output-dir` with `--summaries` and/or `--audio`. Transcripts already on disk are skipped, and their summary and audio are added next to them.

## 7. Check for failures

```bash
python otter_haul_v2.py you@example.com --keychain otter-haul --output-dir ~/Otter --retry
```

`_errors.csv` lists anything that failed and why. The README's "Failures and retries" table explains each reason.

## 8. Keep it up to date automatically (optional)

Each run fetches only what's new, so a schedule is cheap.

**macOS (launchd), every 2 hours.** Save this as `~/Library/LaunchAgents/com.otter-haul.plist`, replacing the paths and email:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.otter-haul</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/you/otter-haul/otter_haul_v2.py</string>
    <string>you@example.com</string>
    <string>--keychain</string><string>otter-haul</string>
    <string>--output-dir</string><string>/Users/you/Otter</string>
    <string>--summaries</string><string>--audio</string>
  </array>
  <key>StartInterval</key><integer>7200</integer>
  <key>StandardOutPath</key><string>/tmp/otter-haul.log</string>
  <key>StandardErrorPath</key><string>/tmp/otter-haul.log</string>
</dict>
</plist>
```

Then load it:

```bash
launchctl load ~/Library/LaunchAgents/com.otter-haul.plist
```

The first scheduled run may ask permission to read the Keychain entry. Choose "Always Allow".

**Linux (cron), every 2 hours:**

```bash
0 */2 * * * OTTER_PASSWORD='…' /usr/bin/python3 /home/you/otter-haul/otter_haul_v2.py you@example.com --output-dir /home/you/Otter --summaries --audio >> /tmp/otter-haul.log 2>&1
```

A password inside crontab can be read by anything running as your user, so prefer a secrets manager if you have one.

## Troubleshooting

| What you see | What to do |
|---|---|
| `Login failed (HTTP 401) … User not logged in` | Wrong password, or the account only has Google/Microsoft sign-in. Check step 2, then overwrite the stored password (step 3, with `-U`). |
| `No Keychain entry for service …` | The service or email doesn't match what you stored. Both must be identical. |
| `⚠️ mp3: audio HTTP 403` | Otter didn't allow the download. Your plan may not include audio export, or the recording was shared without download rights. |
| `⚠️ summary.md: no summary available` | Otter never generated a summary, which is common for older recordings. The transcript is still saved. |
| It stops after a while with HTTP 429 | You're being rate-limited. Wait an hour and run again; it resumes. |
