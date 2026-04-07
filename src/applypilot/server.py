"""ApplyPilot Live Dashboard Server.

FastAPI app serving an interactive dashboard with auto-apply buttons,
real-time status, screenshots, and apply logs.

Usage:
    applypilot serve          # starts on http://127.0.0.1:8899
    applypilot serve --port 9000
"""

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import shutil
import threading
from datetime import datetime
from html import escape
from pathlib import Path

from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse

from applypilot.config import APP_DIR, SCREENSHOT_DIR, LOG_DIR, RESUME_PDF_PATH
from applypilot.database import get_connection
from urllib.parse import urlparse

app = FastAPI(title="ApplyPilot Dashboard")

# Track running apply jobs
_active_applies: dict[str, dict] = {}
_lock = threading.Lock()


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:12]


# ── API Endpoints ─────────────────────────────────────────────────────

@app.get("/api/status")
def api_status():
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    high_fit = conn.execute("SELECT COUNT(*) FROM jobs WHERE fit_score >= 7").fetchone()[0]
    applied = conn.execute("SELECT COUNT(*) FROM jobs WHERE apply_status = 'applied'").fetchone()[0]
    failed = conn.execute("SELECT COUNT(*) FROM jobs WHERE apply_status = 'failed'").fetchone()[0]
    ready = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score >= 7 "
        "AND tailored_resume_path IS NOT NULL "
        "AND (apply_status IS NULL OR apply_status = 'failed')"
    ).fetchone()[0]
    return {"total": total, "high_fit": high_fit, "applied": applied, "failed": failed, "ready": ready}


@app.get("/api/jobs")
def api_jobs(min_score: int = 5):
    conn = get_connection()
    rows = conn.execute("""
        SELECT url, title, salary, location, site,
               fit_score, score_reasoning,
               apply_status, apply_error, applied_at, apply_duration_ms,
               tailored_resume_path, application_url
        FROM jobs
        WHERE fit_score >= ?
        ORDER BY fit_score DESC, site, title
    """, (min_score,)).fetchall()

    jobs = []
    for r in rows:
        url_hash = _url_hash(r["url"])
        # Check for screenshots
        art_dir = SCREENSHOT_DIR / url_hash
        has_screenshots = art_dir.exists() and any(art_dir.glob("*.png"))
        has_log = art_dir.exists() and (art_dir / "log.txt").exists()

        # Check if currently applying
        with _lock:
            is_applying = r["url"] in _active_applies

        jobs.append({
            "url": r["url"],
            "url_hash": url_hash,
            "title": r["title"],
            "salary": r["salary"],
            "location": r["location"],
            "site": r["site"],
            "score": r["fit_score"],
            "reasoning": r["score_reasoning"],
            "apply_status": "applying" if is_applying else (r["apply_status"] or ""),
            "apply_error": r["apply_error"],
            "applied_at": r["applied_at"],
            "duration_ms": r["apply_duration_ms"],
            "resume_path": Path(r["tailored_resume_path"]).name if r["tailored_resume_path"] else "",
            "apply_url": r["application_url"],
            "has_screenshots": has_screenshots,
            "has_log": has_log,
        })
    return jobs


@app.get("/api/jobs/{url_hash}/screenshots")
def api_screenshots(url_hash: str):
    art_dir = SCREENSHOT_DIR / url_hash
    images = []
    if art_dir.exists():
        for png in sorted(art_dir.glob("*.png")):
            b64 = base64.b64encode(png.read_bytes()).decode()
            images.append({"name": png.name, "data": b64})
    return images


@app.get("/api/jobs/{url_hash}/log")
def api_log(url_hash: str):
    art_dir = SCREENSHOT_DIR / url_hash
    log_file = art_dir / "log.txt"
    if log_file.exists():
        return {"log": log_file.read_text(encoding="utf-8", errors="replace")[:10000]}
    return {"log": ""}


@app.post("/api/apply")
def api_apply(body: dict, background_tasks: BackgroundTasks):
    url = body.get("url", "")
    dry_run = body.get("dry_run", True)
    model = body.get("model", "sonnet")

    if not url:
        return JSONResponse({"error": "url required"}, status_code=400)

    with _lock:
        if url in _active_applies:
            return JSONResponse({"error": "already applying"}, status_code=409)
        _active_applies[url] = {"started": datetime.now().isoformat(), "dry_run": dry_run}

    background_tasks.add_task(_run_apply, url, dry_run, model)
    return {"status": "started", "url": url, "dry_run": dry_run, "model": model}


def _run_apply(url: str, dry_run: bool, model: str):
    """Run apply using launcher directly (in-process, in a thread)."""
    import logging
    logger = logging.getLogger(__name__)
    try:
        from applypilot.config import ensure_dirs, load_env
        load_env()
        ensure_dirs()

        from applypilot.apply.launcher import worker_loop
        applied, failed = worker_loop(
            worker_id=0,
            limit=1,
            target_url=url,
            min_score=1,  # bypass score filter since user explicitly chose this job
            headless=False,
            model=model,
            dry_run=dry_run,
        )
        logger.info("Apply finished for %s: applied=%d failed=%d", url[:60], applied, failed)
    except Exception as e:
        logger.exception("Apply error for %s: %s", url[:60], e)
    finally:
        with _lock:
            _active_applies.pop(url, None)


@app.get("/api/active")
def api_active():
    with _lock:
        return dict(_active_applies)


# ── Quick Apply by URL ─────────────────────────────────────────────────────

def _guess_site(url: str) -> str:
    """Guess the ATS/site name from URL domain."""
    domain = urlparse(url).hostname or ""
    if "greenhouse" in domain or "boards.greenhouse" in domain:
        return "greenhouse"
    if "lever" in domain or "jobs.lever" in domain:
        return "lever"
    if "workday" in domain or "myworkday" in domain:
        return "workday"
    if "linkedin" in domain:
        return "linkedin"
    if "ashbyhq" in domain:
        return "ashby"
    if "icims" in domain:
        return "icims"
    return domain.split(".")[-2] if "." in domain else "unknown"


def _ensure_job_in_db(url: str, enrich: bool = False) -> dict:
    """Make sure a URL exists in the jobs table so acquire_job() can find it.

    If the job already exists, just ensures tailored_resume_path is set.
    If not, inserts a minimal row. Optionally runs enrichment + scoring.

    Returns: {"status": "existing"|"inserted"|"enriched", "url": str}
    """
    conn = get_connection()
    row = conn.execute("SELECT url, tailored_resume_path, fit_score FROM jobs WHERE url = ?", (url,)).fetchone()

    resume_path = str(RESUME_PDF_PATH) if RESUME_PDF_PATH.exists() else None

    if row:
        # Job exists — just make sure resume path is set
        if not row["tailored_resume_path"] and resume_path:
            conn.execute(
                "UPDATE jobs SET tailored_resume_path = ? WHERE url = ?",
                (resume_path, url),
            )
            conn.commit()
        return {"status": "existing", "url": url, "score": row["fit_score"]}

    # Insert new job
    site = _guess_site(url)
    now = datetime.now().isoformat()
    conn.execute("""
        INSERT OR IGNORE INTO jobs (url, title, site, discovered_at, tailored_resume_path)
        VALUES (?, ?, ?, ?, ?)
    """, (url, f"Quick Apply ({site})", site, now, resume_path))
    conn.commit()

    result = {"status": "inserted", "url": url, "score": None}

    if enrich:
        try:
            _enrich_single_job(conn, url)
            result["status"] = "enriched"
            # Re-read score after enrichment
            updated = conn.execute("SELECT fit_score, title FROM jobs WHERE url = ?", (url,)).fetchone()
            if updated:
                result["score"] = updated["fit_score"]
                if updated["title"]:
                    result["title"] = updated["title"]
        except Exception as e:
            result["enrich_error"] = str(e)[:200]

    return result


def _enrich_single_job(conn, url: str):
    """Run enrichment (scrape + score) for a single URL."""
    import logging
    _logger = logging.getLogger(__name__)

    try:
        # Step 1: Scrape the job page for description + apply URL
        from playwright.sync_api import sync_playwright
        from applypilot.enrichment.detail import scrape_detail_page

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            detail = scrape_detail_page(page, url)
            browser.close()

        if detail.get("full_description"):
            conn.execute("""
                UPDATE jobs SET
                    full_description = ?,
                    application_url = ?,
                    detail_scraped_at = ?
                WHERE url = ?
            """, (
                detail["full_description"],
                detail.get("application_url"),
                datetime.now().isoformat(),
                url,
            ))
            # Update title from description if we got something better
            if detail.get("full_description"):
                # Try to extract a better title from the page
                lines = detail["full_description"].strip().split("\n")
                if lines and len(lines[0]) < 120:
                    conn.execute(
                        "UPDATE jobs SET title = ? WHERE url = ? AND title LIKE 'Quick Apply%'",
                        (lines[0].strip("# ").strip(), url),
                    )
            conn.commit()

        # Step 2: Score the job if we got a description
        if detail.get("full_description"):
            from applypilot.config import RESUME_PATH
            from applypilot.scoring.scorer import score_job

            resume_text = ""
            if RESUME_PATH.exists():
                resume_text = RESUME_PATH.read_text(encoding="utf-8", errors="replace")

            if resume_text:
                job_dict = dict(conn.execute(
                    "SELECT url, title, description, full_description, location, salary FROM jobs WHERE url = ?",
                    (url,),
                ).fetchone())
                score_result = score_job(resume_text, job_dict)
                conn.execute("""
                    UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ?
                    WHERE url = ?
                """, (
                    score_result.get("score", 0),
                    score_result.get("reasoning", ""),
                    datetime.now().isoformat(),
                    url,
                ))
                conn.commit()

    except ImportError as e:
        _logger.warning("Enrichment dependencies not available: %s", e)
    except Exception as e:
        _logger.exception("Enrichment failed for %s: %s", url[:60], e)


@app.post("/api/apply-url")
def api_apply_url(body: dict, background_tasks: BackgroundTasks):
    """Apply to a single URL — inserts into DB if needed, then applies.

    Body: {url, enrich?: bool, dry_run?: bool, model?: str}
    """
    url = body.get("url", "").strip()
    enrich = body.get("enrich", False)
    dry_run = body.get("dry_run", True)
    model = body.get("model", "sonnet")

    if not url:
        return JSONResponse({"error": "url required"}, status_code=400)
    if not url.startswith("http"):
        return JSONResponse({"error": "url must start with http:// or https://"}, status_code=400)

    with _lock:
        if url in _active_applies:
            return JSONResponse({"error": "already applying to this URL"}, status_code=409)

    # Ensure the job is in the DB (sync — fast for existing jobs, ~30s with enrich)
    db_result = _ensure_job_in_db(url, enrich=enrich)

    # Now trigger apply in background
    with _lock:
        _active_applies[url] = {
            "started": datetime.now().isoformat(),
            "dry_run": dry_run,
            "source": "quick-apply",
        }

    background_tasks.add_task(_run_apply, url, dry_run, model)
    return {
        "status": "started",
        "url": url,
        "dry_run": dry_run,
        "model": model,
        "db": db_result,
    }


@app.post("/api/apply-bulk")
def api_apply_bulk(body: dict, background_tasks: BackgroundTasks):
    """Apply to multiple URLs in sequence.

    Body: {urls: [str], enrich?: bool, dry_run?: bool, model?: str}
    """
    urls = body.get("urls", [])
    enrich = body.get("enrich", False)
    dry_run = body.get("dry_run", True)
    model = body.get("model", "sonnet")

    if not urls:
        return JSONResponse({"error": "urls list required"}, status_code=400)

    # Deduplicate and validate
    clean_urls = []
    for u in urls:
        u = u.strip()
        if u and u.startswith("http") and u not in clean_urls:
            clean_urls.append(u)

    if not clean_urls:
        return JSONResponse({"error": "no valid URLs found"}, status_code=400)

    # Ensure all jobs are in the DB first (sync)
    db_results = []
    for u in clean_urls:
        db_results.append(_ensure_job_in_db(u, enrich=enrich))

    # Queue them for sequential apply in background
    background_tasks.add_task(_run_bulk_apply, clean_urls, dry_run, model)

    return {
        "status": "queued",
        "count": len(clean_urls),
        "dry_run": dry_run,
        "model": model,
        "jobs": db_results,
    }


def _run_bulk_apply(urls: list[str], dry_run: bool, model: str):
    """Apply to a list of URLs sequentially."""
    import logging
    logger = logging.getLogger(__name__)

    for url in urls:
        with _lock:
            if url in _active_applies:
                continue  # skip if already running
            _active_applies[url] = {
                "started": datetime.now().isoformat(),
                "dry_run": dry_run,
                "source": "bulk-apply",
            }

        try:
            from applypilot.config import ensure_dirs, load_env
            load_env()
            ensure_dirs()

            from applypilot.apply.launcher import worker_loop
            applied, failed = worker_loop(
                worker_id=0,
                limit=1,
                target_url=url,
                min_score=1,
                headless=False,
                model=model,
                dry_run=dry_run,
            )
            logger.info("Bulk apply %s: applied=%d failed=%d", url[:60], applied, failed)
        except Exception as e:
            logger.exception("Bulk apply error for %s: %s", url[:60], e)
        finally:
            with _lock:
                _active_applies.pop(url, None)


# ── Review Page (per-job detail with screenshots + log) ───────────────

@app.get("/review/{url_hash}", response_class=HTMLResponse)
def review_page(url_hash: str):
    conn = get_connection()
    # Find the job by hash
    rows = conn.execute("""
        SELECT url, title, salary, location, site,
               fit_score, score_reasoning,
               apply_status, apply_error, applied_at, apply_duration_ms,
               tailored_resume_path, application_url, full_description
        FROM jobs WHERE fit_score >= 1
    """).fetchall()

    job = None
    for r in rows:
        if _url_hash(r["url"]) == url_hash:
            job = r
            break

    if not job:
        return HTMLResponse("<h1>Job not found</h1>", status_code=404)

    title = escape(job["title"] or "")
    site = escape(job["site"] or "")
    location = escape(job["location"] or "")
    salary = escape(job["salary"] or "")
    score = job["fit_score"] or 0
    reasoning = escape(job["score_reasoning"] or "")
    status = job["apply_status"] or "not applied"
    error = escape(job["apply_error"] or "")
    applied_at = job["applied_at"] or ""
    duration_s = round((job["apply_duration_ms"] or 0) / 1000)
    resume = Path(job["tailored_resume_path"]).name if job["tailored_resume_path"] else "N/A"
    job_url = escape(job["url"] or "")
    apply_url = escape(job["application_url"] or "")
    desc = escape(job["full_description"] or "No description available")

    # Status badge
    if status == "applied":
        status_html = '<span class="inline-block px-4 py-2 rounded-lg bg-emerald-900 text-emerald-300 text-lg font-bold">✅ Applied Successfully</span>'
    elif status == "failed":
        status_html = f'<span class="inline-block px-4 py-2 rounded-lg bg-red-900 text-red-300 text-lg font-bold">❌ Failed: {error}</span>'
    else:
        status_html = '<span class="inline-block px-4 py-2 rounded-lg bg-slate-700 text-slate-300 text-lg font-bold">⏳ Not Applied Yet</span>'

    # Screenshots
    art_dir = SCREENSHOT_DIR / url_hash
    screenshots_html = ""
    if art_dir.exists():
        pngs = sorted(art_dir.glob("*.png"))
        if pngs:
            imgs = ""
            for png in pngs:
                b64 = base64.b64encode(png.read_bytes()).decode()
                name = escape(png.name)
                imgs += f'''
                <div class="mb-6">
                    <p class="text-sm text-slate-400 mb-2 font-semibold">{name}</p>
                    <img src="data:image/png;base64,{b64}" class="rounded-lg border border-slate-600 max-w-full cursor-pointer hover:scale-[1.02] transition-transform"
                         onclick="document.getElementById('overlay-img').src=this.src;document.getElementById('overlay').classList.add('active')">
                </div>'''
            screenshots_html = f'<div>{imgs}</div>'
        else:
            screenshots_html = '<p class="text-slate-500">No screenshots captured for this job.</p>'
    else:
        screenshots_html = '<p class="text-slate-500">No screenshots captured for this job.</p>'

    # Log
    log_html = ""
    log_file = art_dir / "log.txt" if art_dir.exists() else None
    if log_file and log_file.exists():
        log_text = escape(log_file.read_text(encoding="utf-8", errors="replace")[:15000])
        log_html = f'<pre class="bg-slate-900 rounded-lg p-4 text-sm text-slate-300 whitespace-pre-wrap overflow-x-auto leading-relaxed">{log_text}</pre>'
    else:
        log_html = '<p class="text-slate-500">No apply log available for this job.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Review: {title}</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-900 text-slate-200 min-h-screen">

<div id="overlay" class="fixed inset-0 bg-black/90 z-50 hidden justify-center items-center cursor-pointer" onclick="this.classList.add('hidden')" style="display:none">
    <img id="overlay-img" src="" class="max-w-[90vw] max-h-[90vh] rounded-lg">
</div>
<script>
document.getElementById('overlay').addEventListener('click', function() {{ this.style.display='none'; }});
function showOverlay(el) {{ document.getElementById('overlay-img').src=el.src; document.getElementById('overlay').style.display='flex'; }}
document.querySelectorAll('.screenshot-img').forEach(img => img.addEventListener('click', function() {{ showOverlay(this); }}));
</script>

<div class="max-w-4xl mx-auto px-4 py-8">

    <!-- Back link -->
    <a href="/" class="text-blue-400 hover:text-blue-300 text-sm mb-6 inline-block">&larr; Back to Dashboard</a>

    <!-- Header -->
    <div class="flex items-start gap-4 mb-6">
        <span class="bg-emerald-500 text-slate-900 font-bold text-2xl px-4 py-2 rounded-xl flex-shrink-0">{score}</span>
        <div>
            <h1 class="text-2xl font-bold mb-1">{title}</h1>
            <p class="text-slate-400">{site} &middot; {location}</p>
            {f'<p class="text-emerald-400 text-sm mt-1">{salary}</p>' if salary else ''}
        </div>
    </div>

    <!-- Status -->
    <div class="mb-8">
        {status_html}
        {f'<p class="text-slate-400 text-sm mt-2">Applied: {escape(applied_at[:19])} &middot; Duration: {duration_s}s</p>' if applied_at else ''}
    </div>

    <!-- Application Details -->
    <div class="bg-slate-800 rounded-xl p-6 mb-8">
        <h2 class="text-lg font-bold mb-4 text-slate-300">📋 Application Details</h2>
        <table class="w-full text-sm">
            <tr class="border-b border-slate-700"><td class="py-2 text-slate-400 w-40">Resume Submitted</td><td class="py-2 font-semibold">{escape(resume)}</td></tr>
            <tr class="border-b border-slate-700"><td class="py-2 text-slate-400">Job URL</td><td class="py-2"><a href="{job_url}" target="_blank" class="text-blue-400 hover:text-blue-300 break-all">{job_url[:80]}...</a></td></tr>
            <tr class="border-b border-slate-700"><td class="py-2 text-slate-400">Apply URL</td><td class="py-2"><a href="{apply_url}" target="_blank" class="text-blue-400 hover:text-blue-300 break-all">{apply_url[:80] if apply_url else 'N/A'}...</a></td></tr>
            <tr class="border-b border-slate-700"><td class="py-2 text-slate-400">Platform</td><td class="py-2">{site}</td></tr>
            <tr><td class="py-2 text-slate-400">Status</td><td class="py-2">{escape(status)}</td></tr>
        </table>
    </div>

    <!-- AI Scoring -->
    <div class="bg-slate-800 rounded-xl p-6 mb-8">
        <h2 class="text-lg font-bold mb-4 text-slate-300">🤖 AI Scoring Reasoning</h2>
        <pre class="text-sm text-slate-300 whitespace-pre-wrap leading-relaxed">{reasoning}</pre>
    </div>

    <!-- Screenshots -->
    <div class="bg-slate-800 rounded-xl p-6 mb-8">
        <h2 class="text-lg font-bold mb-4 text-slate-300">📸 Form Screenshots</h2>
        <p class="text-xs text-slate-500 mb-4">Click any screenshot to view full size</p>
        {screenshots_html}
    </div>

    <!-- Apply Log -->
    <div class="bg-slate-800 rounded-xl p-6 mb-8">
        <h2 class="text-lg font-bold mb-4 text-slate-300">📝 Claude Apply Log</h2>
        <p class="text-xs text-slate-500 mb-4">Full step-by-step log of what Claude did during the application</p>
        {log_html}
    </div>

    <!-- Job Description -->
    <div class="bg-slate-800 rounded-xl p-6 mb-8">
        <h2 class="text-lg font-bold mb-4 text-slate-300">📄 Job Description</h2>
        <pre class="text-sm text-slate-300 whitespace-pre-wrap leading-relaxed max-h-[600px] overflow-y-auto">{desc}</pre>
    </div>

</div>
</body>
</html>"""


# ── Dashboard HTML ────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ApplyPilot Dashboard</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config = { darkMode: 'class' }</script>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; }
  .overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,.92); z-index:50; justify-content:center; align-items:center; cursor:pointer; }
  .overlay.active { display:flex; }
  .overlay img { max-width:90vw; max-height:90vh; border-radius:8px; }
  .spin { animation: spin 1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body class="bg-slate-900 text-slate-200 min-h-screen">

<div id="app" class="max-w-7xl mx-auto px-4 py-6">
  <!-- Header -->
  <h1 class="text-2xl font-bold mb-1">ApplyPilot Dashboard</h1>
  <p id="subtitle" class="text-slate-400 text-sm mb-6"></p>

  <!-- Stats -->
  <div id="stats" class="grid grid-cols-2 md:grid-cols-5 gap-3 mb-6"></div>

  <!-- Quick Apply Section -->
  <div class="bg-slate-800 rounded-xl p-4 mb-6">
    <div class="flex items-center gap-2 mb-3">
      <h2 class="text-sm font-bold text-slate-300">Quick Apply</h2>
      <button onclick="document.getElementById('bulk-modal').classList.remove('hidden')"
              class="text-xs px-2 py-1 rounded bg-slate-700 text-slate-400 hover:bg-slate-600 ml-auto">Bulk Apply</button>
    </div>
    <div class="flex flex-wrap gap-2 items-center">
      <input id="quick-url" type="text" placeholder="Paste a job URL (Greenhouse, Lever, etc.)"
             class="flex-1 min-w-[300px] bg-slate-700 border border-slate-600 text-slate-200 px-3 py-2 rounded text-sm">
      <label class="flex items-center gap-1 text-xs text-slate-400 cursor-pointer select-none">
        <input id="quick-enrich" type="checkbox" class="accent-blue-500"> Enrich &amp; Score
      </label>
      <button onclick="quickApply(true)" class="text-xs px-4 py-2 rounded bg-blue-600 hover:bg-blue-500 text-white font-semibold">Dry Run</button>
      <button onclick="quickApply(false)" class="text-xs px-4 py-2 rounded bg-emerald-600 hover:bg-emerald-500 text-white font-semibold">Apply</button>
    </div>
    <p id="quick-status" class="text-xs text-slate-500 mt-2 hidden"></p>
  </div>

  <!-- Bulk Apply Modal -->
  <div id="bulk-modal" class="fixed inset-0 bg-black/80 z-40 hidden overflow-y-auto">
    <div class="max-w-2xl mx-auto my-12 bg-slate-800 rounded-xl p-6 relative">
      <button onclick="document.getElementById('bulk-modal').classList.add('hidden')" class="absolute top-4 right-4 text-slate-400 hover:text-white text-xl">&times;</button>
      <h2 class="text-lg font-bold mb-4 text-slate-200">Bulk Apply</h2>
      <p class="text-sm text-slate-400 mb-3">Paste URLs (one per line) or upload a CSV file with a column of URLs.</p>
      <textarea id="bulk-urls" rows="8" placeholder="https://boards.greenhouse.io/company/jobs/123&#10;https://jobs.lever.co/company/abc-def&#10;..."
                class="w-full bg-slate-700 border border-slate-600 text-slate-200 px-3 py-2 rounded text-sm mb-3 font-mono"></textarea>
      <div class="flex items-center gap-3 mb-4">
        <label class="text-xs text-slate-400 cursor-pointer bg-slate-700 px-3 py-2 rounded hover:bg-slate-600">
          Upload CSV <input id="bulk-csv" type="file" accept=".csv,.xlsx,.txt" class="hidden" onchange="loadCSV(this)">
        </label>
        <span id="csv-status" class="text-xs text-slate-500"></span>
        <label class="flex items-center gap-1 text-xs text-slate-400 cursor-pointer select-none ml-auto">
          <input id="bulk-enrich" type="checkbox" class="accent-blue-500"> Enrich &amp; Score
        </label>
      </div>
      <div class="flex gap-2">
        <button onclick="bulkApply(true)" class="text-xs px-4 py-2 rounded bg-blue-600 hover:bg-blue-500 text-white font-semibold">Dry Run All</button>
        <button onclick="bulkApply(false)" class="text-xs px-4 py-2 rounded bg-emerald-600 hover:bg-emerald-500 text-white font-semibold">Apply All</button>
      </div>
      <p id="bulk-status" class="text-xs text-slate-500 mt-3 hidden"></p>
    </div>
  </div>

  <!-- Filters -->
  <div class="bg-slate-800 rounded-xl p-3 mb-6 flex flex-wrap gap-2 items-center">
    <span class="text-xs text-slate-400 font-semibold">Score:</span>
    <button onclick="setMinScore(5)" class="score-btn px-3 py-1 rounded text-xs bg-slate-700 text-slate-300 hover:bg-slate-600" data-min="5">All</button>
    <button onclick="setMinScore(7)" class="score-btn px-3 py-1 rounded text-xs bg-blue-600 text-white font-semibold" data-min="7">7+</button>
    <button onclick="setMinScore(8)" class="score-btn px-3 py-1 rounded text-xs bg-slate-700 text-slate-300 hover:bg-slate-600" data-min="8">8+</button>
    <button onclick="setMinScore(9)" class="score-btn px-3 py-1 rounded text-xs bg-slate-700 text-slate-300 hover:bg-slate-600" data-min="9">9+</button>
    <span class="text-xs text-slate-400 font-semibold ml-3">Status:</span>
    <button onclick="setStatus('all')" class="status-btn px-3 py-1 rounded text-xs bg-blue-600 text-white font-semibold" data-st="all">All</button>
    <button onclick="setStatus('applied')" class="status-btn px-3 py-1 rounded text-xs bg-slate-700 text-slate-300 hover:bg-slate-600" data-st="applied">Applied</button>
    <button onclick="setStatus('failed')" class="status-btn px-3 py-1 rounded text-xs bg-slate-700 text-slate-300 hover:bg-slate-600" data-st="failed">Failed</button>
    <button onclick="setStatus('ready')" class="status-btn px-3 py-1 rounded text-xs bg-slate-700 text-slate-300 hover:bg-slate-600" data-st="ready">Ready</button>
    <input id="search" type="text" placeholder="Search title, site..." oninput="render()"
      class="ml-auto bg-slate-700 border border-slate-600 text-slate-200 px-3 py-1 rounded text-xs w-48">
  </div>

  <!-- Job Count -->
  <p id="job-count" class="text-slate-400 text-xs mb-3"></p>

  <!-- Jobs Grid -->
  <div id="jobs" class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4"></div>
</div>

<!-- Screenshot Overlay -->
<div id="overlay" class="overlay" onclick="this.classList.remove('active')">
  <img id="overlay-img" src="">
</div>

<!-- Detail Modal -->
<div id="detail-modal" class="fixed inset-0 bg-black/80 z-40 hidden overflow-y-auto">
  <div class="max-w-3xl mx-auto my-8 bg-slate-800 rounded-xl p-6 relative">
    <button onclick="closeDetail()" class="absolute top-4 right-4 text-slate-400 hover:text-white text-xl">&times;</button>
    <div id="detail-content"></div>
  </div>
</div>

<script>
let jobs = [];
let minScore = 7;
let statusFilter = 'all';

async function loadData() {
  const [statusRes, jobsRes] = await Promise.all([
    fetch('/api/status').then(r => r.json()),
    fetch('/api/jobs?min_score=5').then(r => r.json())
  ]);
  jobs = jobsRes;

  document.getElementById('subtitle').textContent =
    `${statusRes.total} jobs · ${statusRes.high_fit} strong fit · ${statusRes.applied} applied · ${statusRes.ready} ready`;

  document.getElementById('stats').innerHTML = `
    <div class="bg-slate-800 rounded-xl p-4"><div class="text-2xl font-bold">${statusRes.total}</div><div class="text-slate-400 text-xs">Total</div></div>
    <div class="bg-slate-800 rounded-xl p-4"><div class="text-2xl font-bold text-amber-400">${statusRes.high_fit}</div><div class="text-slate-400 text-xs">Strong Fit (7+)</div></div>
    <div class="bg-slate-800 rounded-xl p-4"><div class="text-2xl font-bold text-indigo-400">${statusRes.ready}</div><div class="text-slate-400 text-xs">Ready to Apply</div></div>
    <div class="bg-slate-800 rounded-xl p-4"><div class="text-2xl font-bold text-emerald-400">${statusRes.applied}</div><div class="text-slate-400 text-xs">Applied</div></div>
    <div class="bg-slate-800 rounded-xl p-4"><div class="text-2xl font-bold text-red-400">${statusRes.failed}</div><div class="text-slate-400 text-xs">Failed</div></div>
  `;
  render();
}

function render() {
  const q = document.getElementById('search').value.toLowerCase();
  const filtered = jobs.filter(j => {
    if (j.score < minScore) return false;
    if (statusFilter !== 'all') {
      const st = j.apply_status || '';
      if (statusFilter === 'ready' && st !== '' && st !== 'failed') return false;
      if (statusFilter === 'applied' && st !== 'applied') return false;
      if (statusFilter === 'failed' && st !== 'failed') return false;
    }
    if (q && !(j.title||'').toLowerCase().includes(q) && !(j.site||'').toLowerCase().includes(q) && !(j.location||'').toLowerCase().includes(q)) return false;
    return true;
  });

  document.getElementById('job-count').textContent = `Showing ${filtered.length} of ${jobs.length} jobs`;

  const html = filtered.map(j => {
    const scoreColor = j.score >= 9 ? 'bg-emerald-500' : j.score >= 7 ? 'bg-emerald-600' : 'bg-amber-500';
    const borderColor = j.score >= 9 ? 'border-emerald-500' : j.score >= 7 ? 'border-blue-500' : 'border-amber-500';

    let statusBadge = '';
    if (j.apply_status === 'applied') statusBadge = '<span class="text-xs px-2 py-0.5 rounded bg-emerald-900 text-emerald-300 font-semibold">Applied ✓</span>';
    else if (j.apply_status === 'failed') statusBadge = `<span class="text-xs px-2 py-0.5 rounded bg-red-900 text-red-300 font-semibold">Failed</span>`;
    else if (j.apply_status === 'applying') statusBadge = '<span class="text-xs px-2 py-0.5 rounded bg-yellow-900 text-yellow-300 font-semibold"><span class="spin inline-block">⟳</span> Applying...</span>';
    else if (j.resume_path) statusBadge = '<span class="text-xs px-2 py-0.5 rounded bg-indigo-900 text-indigo-300 font-semibold">Ready</span>';

    let applyBtns = '';
    if (j.apply_status === 'applied') {
      applyBtns = `<a href="/review/${j.url_hash}" target="_blank" class="text-xs px-3 py-1 rounded bg-purple-600 hover:bg-purple-500 text-white font-semibold">View Results</a>`;
    } else if (j.apply_status !== 'applying') {
      applyBtns = `
        <button onclick="triggerApply('${encodeURIComponent(j.url)}', true)" class="text-xs px-3 py-1 rounded bg-blue-600 hover:bg-blue-500 text-white font-semibold">Dry Run</button>
        <button onclick="triggerApply('${encodeURIComponent(j.url)}', false)" class="text-xs px-3 py-1 rounded bg-emerald-600 hover:bg-emerald-500 text-white font-semibold">Auto Apply</button>
      `;
    }

    let extraInfo = '';
    if (j.applied_at) {
      const dur = j.duration_ms ? Math.round(j.duration_ms/1000) + 's' : '';
      extraInfo += `<div class="text-xs text-slate-500 mt-1">Applied: ${j.applied_at.slice(0,19)} · ${dur}</div>`;
    }
    if (j.resume_path) extraInfo += `<div class="text-xs text-slate-500">Resume: ${j.resume_path}</div>`;

    let artifactBtns = '';
    if (j.has_screenshots || j.has_log) artifactBtns += `<a href="/review/${j.url_hash}" target="_blank" class="text-xs px-3 py-1 rounded bg-purple-600 hover:bg-purple-500 text-white font-semibold">View Results</a>`;

    return `
    <div class="bg-slate-800 rounded-xl p-4 border-l-4 ${borderColor} hover:translate-y-[-2px] transition-all cursor-pointer" onclick="showDetail('${encodeURIComponent(j.url)}')">
      <div class="flex items-center gap-2 mb-2">
        <span class="${scoreColor} text-slate-900 font-bold text-xs px-2 py-1 rounded">${j.score}</span>
        <a href="${j.url}" target="_blank" class="font-semibold text-sm hover:text-blue-400 truncate" onclick="event.stopPropagation()">${j.title}</a>
        ${statusBadge}
      </div>
      <div class="flex flex-wrap gap-1 mb-1">
        <span class="text-xs px-2 py-0.5 rounded bg-slate-700 text-slate-300">${j.site}</span>
        ${j.salary ? `<span class="text-xs px-2 py-0.5 rounded bg-emerald-900/50 text-emerald-300">${j.salary}</span>` : ''}
        ${j.location ? `<span class="text-xs px-2 py-0.5 rounded bg-blue-900/50 text-blue-300">${(j.location||'').slice(0,35)}</span>` : ''}
      </div>
      ${extraInfo}
      <div class="flex items-center gap-2 mt-3" onclick="event.stopPropagation()">
        ${applyBtns}
        ${artifactBtns}
      </div>
    </div>`;
  }).join('');
  document.getElementById('jobs').innerHTML = html || '<p class="text-slate-500 col-span-3">No jobs match filters.</p>';
}

function setMinScore(s) {
  minScore = s;
  document.querySelectorAll('.score-btn').forEach(b => {
    b.className = b.className.replace('bg-blue-600 text-white font-semibold', 'bg-slate-700 text-slate-300');
    if (parseInt(b.dataset.min) === s) b.className = b.className.replace('bg-slate-700 text-slate-300', 'bg-blue-600 text-white font-semibold');
  });
  render();
}

function setStatus(s) {
  statusFilter = s;
  document.querySelectorAll('.status-btn').forEach(b => {
    b.className = b.className.replace('bg-blue-600 text-white font-semibold', 'bg-slate-700 text-slate-300');
    if (b.dataset.st === s) b.className = b.className.replace('bg-slate-700 text-slate-300', 'bg-blue-600 text-white font-semibold');
  });
  render();
}

async function triggerApply(encodedUrl, dryRun) {
  const url = decodeURIComponent(encodedUrl);
  if (!dryRun && !confirm('Submit application for real? (not a dry run)')) return;

  const res = await fetch('/api/apply', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url, dry_run: dryRun, model: 'sonnet'})
  });
  const data = await res.json();
  if (data.error) { alert(data.error); return; }

  // Mark as applying in local state
  const job = jobs.find(j => j.url === url);
  if (job) job.apply_status = 'applying';
  render();

  // Poll until done
  pollApply(url);
}

async function pollApply(url) {
  const check = async () => {
    const active = await fetch('/api/active').then(r => r.json());
    if (url in active) {
      setTimeout(check, 3000);
    } else {
      // Done - reload data
      await loadData();
    }
  };
  setTimeout(check, 5000);
}

async function showDetail(encodedUrl) {
  const url = decodeURIComponent(encodedUrl);
  const job = jobs.find(j => j.url === url);
  if (!job) return;

  let screenshots = '';
  if (job.has_screenshots) {
    const imgs = await fetch(`/api/jobs/${job.url_hash}/screenshots`).then(r => r.json());
    screenshots = '<h3 class="font-semibold mt-4 mb-2">Screenshots</h3><div class="flex flex-wrap gap-2">' +
      imgs.map(i => `<img src="data:image/png;base64,${i.data}" class="max-w-xs rounded border border-slate-600 cursor-pointer hover:scale-105 transition-transform" onclick="event.stopPropagation();document.getElementById(\\'overlay-img\\').src=this.src;document.getElementById(\\'overlay\\').classList.add(\\'active\\')">`).join('') +
      '</div>';
  }

  let logHtml = '';
  if (job.has_log) {
    const logData = await fetch(`/api/jobs/${job.url_hash}/log`).then(r => r.json());
    if (logData.log) {
      logHtml = `<h3 class="font-semibold mt-4 mb-2">Apply Log</h3><pre class="bg-slate-900 rounded p-3 text-xs text-slate-300 max-h-80 overflow-y-auto whitespace-pre-wrap">${logData.log.replace(/</g,'&lt;')}</pre>`;
    }
  }

  const reasoning = (job.reasoning || '').replace(/</g, '&lt;');

  document.getElementById('detail-content').innerHTML = `
    <div class="flex items-center gap-3 mb-4">
      <span class="bg-emerald-500 text-slate-900 font-bold text-lg px-3 py-1 rounded">${job.score}</span>
      <div>
        <h2 class="text-xl font-bold">${job.title}</h2>
        <p class="text-slate-400 text-sm">${job.site} · ${job.location || 'No location'}</p>
      </div>
    </div>
    ${job.apply_status === 'applied' ? '<div class="bg-emerald-900/50 text-emerald-300 px-3 py-2 rounded mb-3 text-sm font-semibold">✓ Applied' + (job.applied_at ? ' on ' + job.applied_at.slice(0,19) : '') + (job.duration_ms ? ' · ' + Math.round(job.duration_ms/1000) + 's' : '') + '</div>' : ''}
    ${job.apply_status === 'failed' ? '<div class="bg-red-900/50 text-red-300 px-3 py-2 rounded mb-3 text-sm">✗ Failed: ' + (job.apply_error||'unknown') + '</div>' : ''}
    ${job.resume_path ? '<div class="text-xs text-slate-400 mb-3">Resume: ' + job.resume_path + '</div>' : ''}
    <h3 class="font-semibold mb-1">AI Scoring</h3>
    <pre class="bg-slate-900 rounded p-3 text-xs text-slate-300 mb-3 whitespace-pre-wrap">${reasoning}</pre>
    <div class="flex gap-2 mb-4">
      <a href="${job.url}" target="_blank" class="text-xs px-3 py-1 rounded bg-slate-700 hover:bg-slate-600 text-blue-400">View Job ↗</a>
      ${job.apply_url ? `<a href="${job.apply_url}" target="_blank" class="text-xs px-3 py-1 rounded bg-slate-700 hover:bg-slate-600 text-blue-400">Apply Page ↗</a>` : ''}
      ${job.apply_status !== 'applied' ? `<button onclick="triggerApply('${encodeURIComponent(job.url)}', true);closeDetail()" class="text-xs px-3 py-1 rounded bg-blue-600 hover:bg-blue-500 text-white font-semibold">Dry Run</button><button onclick="triggerApply('${encodeURIComponent(job.url)}', false);closeDetail()" class="text-xs px-3 py-1 rounded bg-emerald-600 hover:bg-emerald-500 text-white font-semibold">Auto Apply</button>` : ''}
    </div>
    ${screenshots}
    ${logHtml}
  `;
  document.getElementById('detail-modal').classList.remove('hidden');
}

function closeDetail() {
  document.getElementById('detail-modal').classList.add('hidden');
}

// Close modal on Escape
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    closeDetail();
    document.getElementById('overlay').classList.remove('active');
    document.getElementById('bulk-modal').classList.add('hidden');
  }
});

// ── Quick Apply ──────────────────────────────────────────────

async function quickApply(dryRun) {
  const url = document.getElementById('quick-url').value.trim();
  if (!url) { alert('Please paste a job URL first.'); return; }
  if (!url.startsWith('http')) { alert('URL must start with http:// or https://'); return; }
  if (!dryRun && !confirm('Submit application for real? (not a dry run)')) return;

  const enrich = document.getElementById('quick-enrich').checked;
  const statusEl = document.getElementById('quick-status');
  statusEl.classList.remove('hidden');
  statusEl.textContent = enrich ? 'Enriching & queuing apply...' : 'Queuing apply...';
  statusEl.className = 'text-xs text-yellow-400 mt-2';

  try {
    const res = await fetch('/api/apply-url', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url, enrich, dry_run: dryRun, model: 'sonnet'})
    });
    const data = await res.json();
    if (data.error) {
      statusEl.textContent = 'Error: ' + data.error;
      statusEl.className = 'text-xs text-red-400 mt-2';
      return;
    }
    const dbInfo = data.db || {};
    let msg = dryRun ? 'Dry run started' : 'Apply started';
    if (dbInfo.status === 'existing') msg += ' (job was already in DB)';
    else if (dbInfo.status === 'enriched') msg += ` (enriched, score: ${dbInfo.score || '?'})`;
    else if (dbInfo.status === 'inserted') msg += ' (new job added to DB)';
    statusEl.textContent = msg;
    statusEl.className = 'text-xs text-emerald-400 mt-2';

    // Refresh job list & poll for completion
    await loadData();
    pollApply(url);
  } catch (e) {
    statusEl.textContent = 'Network error: ' + e.message;
    statusEl.className = 'text-xs text-red-400 mt-2';
  }
}

// Allow Enter key in the URL input
document.getElementById('quick-url').addEventListener('keydown', e => {
  if (e.key === 'Enter') quickApply(true);
});

// ── Bulk Apply ───────────────────────────────────────────────

function loadCSV(input) {
  const file = input.files[0];
  if (!file) return;
  const statusEl = document.getElementById('csv-status');
  const reader = new FileReader();
  reader.onload = function(e) {
    const text = e.target.result;
    // Extract URLs from CSV — find anything that looks like a URL
    const urlRegex = /https?:\\/\\/[^\\s,\"'<>]+/g;
    const urls = [...new Set(text.match(urlRegex) || [])];
    if (urls.length === 0) {
      statusEl.textContent = 'No URLs found in file.';
      return;
    }
    document.getElementById('bulk-urls').value = urls.join('\\n');
    statusEl.textContent = `Loaded ${urls.length} URLs from ${file.name}`;
  };
  reader.readAsText(file);
}

async function bulkApply(dryRun) {
  const text = document.getElementById('bulk-urls').value.trim();
  if (!text) { alert('Please paste some URLs or upload a CSV.'); return; }

  const urls = text.split(/[\\n\\r]+/).map(u => u.trim()).filter(u => u.startsWith('http'));
  if (urls.length === 0) { alert('No valid URLs found.'); return; }
  if (!dryRun && !confirm(`Submit ${urls.length} real applications? (not dry runs)`)) return;

  const enrich = document.getElementById('bulk-enrich').checked;
  const statusEl = document.getElementById('bulk-status');
  statusEl.classList.remove('hidden');
  statusEl.textContent = `Processing ${urls.length} URLs...`;
  statusEl.className = 'text-xs text-yellow-400 mt-3';

  try {
    const res = await fetch('/api/apply-bulk', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({urls, enrich, dry_run: dryRun, model: 'sonnet'})
    });
    const data = await res.json();
    if (data.error) {
      statusEl.textContent = 'Error: ' + data.error;
      statusEl.className = 'text-xs text-red-400 mt-3';
      return;
    }
    statusEl.textContent = `Queued ${data.count} jobs for ${dryRun ? 'dry run' : 'apply'}. They will run sequentially.`;
    statusEl.className = 'text-xs text-emerald-400 mt-3';

    // Close modal after short delay & refresh
    setTimeout(() => {
      document.getElementById('bulk-modal').classList.add('hidden');
      loadData();
    }, 2000);

    // Poll for all
    urls.forEach(u => pollApply(u));
  } catch (e) {
    statusEl.textContent = 'Network error: ' + e.message;
    statusEl.className = 'text-xs text-red-400 mt-3';
  }
}

// Initial load + auto-refresh every 30s
loadData();
setInterval(loadData, 30000);
</script>
</body>
</html>"""
