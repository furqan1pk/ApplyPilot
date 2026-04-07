# ApplyPilot - Agent Handoff Document

> This document is for a Claude agent continuing work on Furqan's ApplyPilot setup.
> Last updated: 2026-04-07. Commit `31cbcd9`.

## Owner

Furqan Arshad - ML Scientist II @ Wayfair, H1B holder, UC Davis.
Target roles: ML Scientist, Applied Scientist, Research Scientist (0-4 YOE, IC only).
Hard exclusions: intern, co-op, manager, director, VP, staff, principal, senior director, SWE-only roles.

## Architecture Overview

ApplyPilot is a 6-stage autonomous job application pipeline:

```
discover -> enrich -> score -> tailor -> cover -> pdf -> apply
```

**Stack:** Python 3.11+, SQLite (WAL mode), FastAPI dashboard, Claude Code CLI + Playwright MCP for browser automation, Chrome DevTools Protocol for screenshots.

## Key Paths

| What | Path |
|---|---|
| Repo root | `C:/Users/furqa/OneDrive - University of California, Davis/Documents/GitHub/ApplyPilot/` |
| User data dir | `~/.applypilot/` |
| Database | `~/.applypilot/applypilot.db` |
| Environment | `~/.applypilot/.env` |
| Profile | `~/.applypilot/profile.json` |
| Search queries | `~/.applypilot/searches.yaml` |
| Resume (PDF) | `~/.applypilot/resume.pdf` |
| Resume (text) | `~/.applypilot/resume.txt` |
| Screenshots | `~/.applypilot/screenshots/{url_hash}/` |
| Logs | `~/.applypilot/logs/` |
| Tailored resumes | `~/.applypilot/tailored_resumes/` |
| Cover letters | `~/.applypilot/cover_letters/` |
| Chrome profiles | `~/.applypilot/chrome-workers/` |

## Key Source Files

| File | Purpose |
|---|---|
| `src/applypilot/server.py` | **FastAPI dashboard** with auto-apply buttons, review pages, screenshot viewer. ~500 lines. This was custom-built for Furqan. |
| `src/applypilot/apply/launcher.py` | Worker loop for auto-apply. Modified to save screenshots via CDP + save artifacts (PNGs, logs, meta.json) per job. |
| `src/applypilot/apply/chrome.py` | Chrome launch/cleanup. Modified to create fresh minimal profiles (not clone user's active Chrome). |
| `src/applypilot/cli.py` | Typer CLI. Added `serve` command for dashboard. |
| `src/applypilot/config.py` | All paths, tier detection, defaults. Added `SCREENSHOT_DIR`. |
| `src/applypilot/view.py` | Static HTML dashboard generator. Enhanced with apply status badges, screenshots, logs. |
| `src/applypilot/database.py` | SQLite schema, migrations, connection helpers. |
| `src/applypilot/scoring/` | LLM-based job scoring against profile. |
| `src/applypilot/discovery/` | Job board scraping (Greenhouse, Lever, LinkedIn, etc). |

## Database Schema (key columns)

```sql
-- Discovery
url TEXT PRIMARY KEY, title, salary, description, location, site, strategy, discovered_at

-- Enrichment
full_description, application_url, detail_scraped_at, detail_error

-- Scoring
fit_score INTEGER, score_reasoning TEXT, scored_at

-- Tailoring
tailored_resume_path, tailored_at, tailor_attempts

-- Apply
applied_at, apply_status TEXT, apply_error, apply_attempts,
apply_duration_ms, apply_task_id, verification_confidence
```

`apply_status` values: `applied`, `failed`, `skipped`, NULL (not attempted).

## How Auto-Apply Works

1. User clicks "Auto Apply" on dashboard (or runs `applypilot apply`)
2. `worker_loop()` in `launcher.py` picks jobs where `fit_score >= min_score` AND `apply_status IS NULL` AND `tailored_resume_path IS NOT NULL`
3. For each job:
   - Launches a fresh Chrome instance via CDP on a random port (9222+)
   - Spawns `claude` CLI subprocess with Playwright MCP tools
   - Claude navigates to the job URL, fills out the application form
   - After Claude finishes, CDP screenshot is captured via WebSocket (`Page.captureScreenshot`)
   - Artifacts saved to `~/.applypilot/screenshots/{sha256(url)[:12]}/`
   - Chrome cleaned up

The dashboard's `/api/apply` endpoint calls `worker_loop()` directly in-process (not subprocess) to avoid `taskkill` killing the server.

## LLM Configuration

Currently in `~/.applypilot/.env`:
```
GEMINI_API_KEY=AIzaSyCLOhT2ipB_h4IEk5LSxLHaLTJVR5oPEzE
LLM_MODEL=gemini-2.5-flash
```

**Options:**
- **Gemini 2.5 Flash** (current): Fast, cheap (~$0.15/day). Free tier = 15 RPM, will 429 on bulk scoring (800+ jobs). Need billing enabled for heavy use.
- **Ollama llama3.2** (local): Free, no rate limits, but too small (3B) for resume tailoring. Fine for scoring. Set `LLM_URL=http://localhost:11434/v1` and `LLM_MODEL=llama3.2`.
- **Claude Sonnet** (for apply): Used by the Claude CLI subprocess for browser automation. ~$2-3 per application. Haiku is unreliable (can't load Playwright MCP tools).

## Running the Dashboard

```bash
cd "C:/Users/furqa/OneDrive - University of California, Davis/Documents/GitHub/ApplyPilot"
python -c "import uvicorn; from applypilot.server import app; uvicorn.run(app, host='127.0.0.1', port=8888)"
# Or: applypilot serve --port 8888
```

Dashboard at `http://localhost:8888`. Features:
- Job list with scores, status badges
- "Auto Apply" button per job
- `/review/{url_hash}` - full review page with screenshots, log, job description
- `/api/jobs` - JSON API for all jobs
- `/api/active` - currently running applies

## Common Commands

```bash
# Full pipeline (discover + enrich + score)
applypilot run

# Just discover new jobs
applypilot run discover

# Just score (requires LLM)
applypilot run score

# Apply to top jobs
applypilot apply --limit 5 --model sonnet

# Dry run (fill forms but don't submit)
applypilot apply --dry-run --model sonnet --limit 1

# Static dashboard (HTML file)
applypilot dashboard
```

## Known Issues & Gotchas

1. **Gemini 429 rate limits**: Free tier is 15 RPM. Bulk scoring 800 jobs will fail. Either enable billing or use Ollama for scoring.

2. **Chrome profile picker**: If Chrome shows "Who's using Chrome?" dialog, the fresh profile fix in `chrome.py` should handle it. If it recurs, check that singleton lock files are being cleaned.

3. **Port conflicts**: If port 8888 is busy, kill the old process: `netstat -ano | findstr :8888` then `taskkill /PID <pid> /F`.

4. **Haiku unreliable for apply**: Claude Haiku can't reliably load Playwright MCP browser tools. Always use `--model sonnet` for apply.

5. **Resume tailoring with small models**: Ollama llama3.2 (3B) fails structured resume generation. Use Gemini or skip tailoring (set generic resume on jobs).

6. **Screenshots ephemeral**: Playwright MCP screenshots get deleted on Chrome cleanup. The CDP screenshot capture (`_capture_cdp_screenshot`) runs before cleanup to persist them.

7. **Workday requires accounts**: Unlike Greenhouse/Lever (guest apply), Workday needs a logged-in account. May need manual intervention.

8. **Scores > 10 in old data**: Original scorer used 100-point scale. If you see scores like 78, run: `UPDATE jobs SET fit_score = 10 WHERE fit_score > 10`.

## What's Working (as of 2026-04-07)

- Job discovery across 5+ boards (800 jobs found)
- AI scoring with Gemini 2.5 Flash
- Auto-apply on Greenhouse and Lever forms (tested, confirmed working)
- CDP screenshot capture
- Live dashboard with auto-apply buttons
- Review pages with screenshots + logs
- Dry-run mode

## What Still Needs Work

- **LinkedIn apply**: Not supported yet (requires login, complex flow)
- **Workday apply**: Account-based, partially working
- **Resume tailoring**: Skipped for now (using generic resume). Needs a larger model or Gemini.
- **Cover letters**: Pipeline exists but not tested end-to-end
- **Daily automation**: No cron/scheduler set up yet. User runs manually.
- **Error recovery**: If apply fails mid-form, no retry logic beyond re-running
- **Dashboard polish**: Could add filtering, sorting, bulk actions

## Quick DB Queries

```sql
-- Check job counts by status
SELECT apply_status, COUNT(*) FROM jobs GROUP BY apply_status;

-- High-fit unapplied jobs
SELECT title, fit_score, site FROM jobs WHERE fit_score >= 7 AND apply_status IS NULL ORDER BY fit_score DESC;

-- Mark a job ready for apply (needs resume path set)
UPDATE jobs SET tailored_resume_path = 'C:\Users\furqa\.applypilot\resume.pdf' WHERE url LIKE '%some-job%';

-- Reset a failed job for retry
UPDATE jobs SET apply_status = NULL, apply_error = NULL, apply_attempts = 0 WHERE url LIKE '%some-job%';
```

## Search Configuration

`~/.applypilot/searches.yaml` has 16 ML/AI queries across 3 tiers and 5 locations (SF, Boston, NYC, Seattle, Remote). Exclusions for senior/manager/intern titles are built in.

## Profile

`~/.applypilot/profile.json` contains Furqan's full profile: work history, skills, education, H1B status (no sponsorship needed), compensation range ($160K-$200K), EEO data. This is used by the scoring and apply stages.
