# ApplyPilot - Daily Usage Guide

Your personal job application autopilot. Here's how to use it every day.

---

## Quick Start (Daily Routine)

### 1. Discover New Jobs
```bash
applypilot run discover
```
Finds jobs from Greenhouse, Lever, LinkedIn, and more based on your `~/.applypilot/searches.yaml`.

### 2. Score Jobs
```bash
applypilot run score
```
AI scores each job 1-10 against your resume. Uses Gemini 2.5 Flash (~$0.15/day).

### 3. Open Dashboard
```bash
# Option A: Live dashboard (recommended - has auto-apply buttons)
applypilot serve --port 8888
# Then open http://localhost:8888

# Option B: Quick start without CLI
python -c "import uvicorn; from applypilot.server import app; uvicorn.run(app, host='127.0.0.1', port=8888)"
```

### 4. Review & Apply
- Open `http://localhost:8888`
- Browse jobs sorted by fit score
- Click **Auto Apply** on jobs you want to apply to
- Chrome opens, Claude fills the form automatically (~5 min per job, ~$2-3)
- Click **Review** after to see screenshots + logs of what was submitted

### 5. Verify Applications
- Go to `http://localhost:8888/review/{url_hash}` for any job
- Check: screenshots of filled form, Claude's action log, resume used
- All screenshots saved in `~/.applypilot/screenshots/`

---

## Full Pipeline (One Command)

```bash
# Run everything: discover -> enrich -> score
applypilot run

# With parallel workers (faster discovery)
applypilot run -w 4
```

---

## Apply Commands

```bash
# Auto-apply to top 5 jobs (real submission)
applypilot apply --limit 5 --model sonnet

# Dry run first (fills forms but doesn't submit)
applypilot apply --dry-run --model sonnet --limit 1

# Apply with visible browser (not headless)
applypilot apply --limit 3 --model sonnet --no-headless
```

**Always use `--model sonnet`** (Haiku doesn't work reliably for apply).

---

## Cost Breakdown

| Action | Cost | Notes |
|---|---|---|
| Discover + Enrich | Free | Web scraping |
| Score (Gemini Flash) | ~$0.15/day | Free tier: 15 RPM limit |
| Score (Ollama local) | Free | `LLM_MODEL=llama3.2` in .env |
| Apply (Sonnet) | ~$2-3/job | Claude Code CLI usage |
| Apply (dry run) | Same cost | Still uses Claude, just doesn't click Submit |

---

## Configuration Files

All in `~/.applypilot/`:

| File | What it does |
|---|---|
| `.env` | API keys, LLM model selection |
| `profile.json` | Your resume data, skills, preferences |
| `searches.yaml` | Job search queries, locations, exclusions |
| `resume.pdf` | Your resume (uploaded during apply) |
| `resume.txt` | Text version (used for scoring) |

### Switch LLM Model

Edit `~/.applypilot/.env`:

```bash
# Gemini (current - fast, cheap, rate limited on free tier)
LLM_MODEL=gemini-2.5-flash
GEMINI_API_KEY=your_key_here

# Ollama (free, local, no rate limits)
# LLM_URL=http://localhost:11434/v1
# LLM_MODEL=llama3.2
```

---

## Troubleshooting

### "Port already in use"
```bash
# Find what's using port 8888
netstat -ano | findstr :8888
# Kill it
taskkill /PID <the_pid> /F
```

### "429 Too Many Requests" during scoring
Gemini free tier = 15 requests/minute. Options:
- Wait and retry (it auto-retries)
- Switch to Ollama (free, no limits): set `LLM_URL=http://localhost:11434/v1` and `LLM_MODEL=llama3.2` in `.env`
- Enable Gemini billing for higher limits

### Chrome shows "Who's using Chrome?" dialog
This should be fixed (fresh profiles are created). If it happens:
```bash
# Delete old worker profiles
rd /s /q %USERPROFILE%\.applypilot\chrome-workers
```

### Apply seems stuck / blank browser
- Make sure no other Chrome debug instances are running
- Check logs: `~/.applypilot/logs/`
- Try with `--no-headless` to see what's happening

### Reset a failed job to retry
```bash
python -c "
import sqlite3
conn = sqlite3.connect(r'C:\Users\furqa\.applypilot\applypilot.db')
conn.execute(\"UPDATE jobs SET apply_status = NULL, apply_error = NULL WHERE url LIKE '%job-url-fragment%'\")
conn.commit()
print('Done')
"
```

---

## Useful DB Queries

```bash
# Open database
sqlite3 ~/.applypilot/applypilot.db

# How many jobs by status?
SELECT apply_status, COUNT(*) FROM jobs GROUP BY apply_status;

# Top unapplied jobs
SELECT title, fit_score, site FROM jobs WHERE fit_score >= 7 AND apply_status IS NULL ORDER BY fit_score DESC LIMIT 20;

# Today's new jobs
SELECT title, fit_score, site FROM jobs WHERE discovered_at >= date('now') ORDER BY fit_score DESC;

# Jobs applied to
SELECT title, applied_at, apply_duration_ms/1000 as seconds FROM jobs WHERE apply_status = 'applied' ORDER BY applied_at DESC;
```

---

## File Locations

```
~/.applypilot/
  applypilot.db          # Job database (SQLite)
  .env                   # API keys & config
  profile.json           # Your profile data
  searches.yaml          # Search queries
  resume.pdf             # Your resume
  resume.txt             # Text resume for scoring
  screenshots/           # Per-job screenshots (by URL hash)
    4c2992aba220/
      screenshot-0.png
      log.txt
      meta.json
  logs/                  # Claude apply logs
  tailored_resumes/      # Custom resumes per job
  cover_letters/         # Generated cover letters
  chrome-workers/        # Temporary Chrome profiles
```
