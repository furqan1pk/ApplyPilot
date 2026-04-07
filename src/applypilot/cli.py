"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("haiku", "--model", "-m", help="Claude model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Claude Code CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
    )


@app.command(name="apply-url")
def apply_url(
    url: str = typer.Argument(..., help="Job URL to apply to (Greenhouse, Lever, etc)."),
    enrich: bool = typer.Option(False, "--enrich", "-e", help="Scrape job page and score before applying."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    model: str = typer.Option("sonnet", "--model", "-m", help="Claude model name."),
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode."),
) -> None:
    """Apply to a specific job URL — even if it's not in the database yet.

    Inserts the URL into the DB (if needed), optionally enriches & scores,
    then launches auto-apply.

    Examples:
        applypilot apply-url https://boards.greenhouse.io/company/jobs/123 --dry-run
        applypilot apply-url https://jobs.lever.co/company/abc --enrich --dry-run
        applypilot apply-url https://some-job-url.com --enrich --model sonnet
    """
    _bootstrap()

    from applypilot.config import check_tier, RESUME_PDF_PATH
    from applypilot.database import get_connection

    # Tier 3 required for apply
    check_tier(3, "auto-apply")

    if not url.startswith("http"):
        console.print("[red]URL must start with http:// or https://[/red]")
        raise typer.Exit(code=1)

    conn = get_connection()

    # Ensure job exists in DB
    row = conn.execute("SELECT url, tailored_resume_path, fit_score FROM jobs WHERE url = ?", (url,)).fetchone()
    resume_path = str(RESUME_PDF_PATH) if RESUME_PDF_PATH.exists() else None

    if row:
        console.print(f"[green]Job already in DB[/green] (score: {row['fit_score'] or 'unscored'})")
        if not row["tailored_resume_path"] and resume_path:
            conn.execute("UPDATE jobs SET tailored_resume_path = ? WHERE url = ?", (resume_path, url))
            conn.commit()
    else:
        from urllib.parse import urlparse
        domain = urlparse(url).hostname or "unknown"
        from datetime import datetime
        now = datetime.now().isoformat()
        conn.execute("""
            INSERT OR IGNORE INTO jobs (url, title, site, discovered_at, tailored_resume_path)
            VALUES (?, ?, ?, ?, ?)
        """, (url, f"Quick Apply ({domain})", domain, now, resume_path))
        conn.commit()
        console.print(f"[blue]New job inserted into DB[/blue]")

    # Optionally enrich & score
    if enrich:
        console.print("[yellow]Enriching (scraping + scoring)...[/yellow]")
        try:
            from applypilot.server import _enrich_single_job
            _enrich_single_job(conn, url)
            updated = conn.execute("SELECT fit_score, title FROM jobs WHERE url = ?", (url,)).fetchone()
            if updated and updated["fit_score"]:
                console.print(f"[green]Enriched![/green] Score: {updated['fit_score']}, Title: {updated['title']}")
            else:
                console.print("[yellow]Enrichment completed (score may not be available)[/yellow]")
        except Exception as e:
            console.print(f"[yellow]Enrichment warning:[/yellow] {e}")
            console.print("[dim]Continuing with apply anyway...[/dim]")

    # Launch apply
    from applypilot.apply.launcher import main as apply_main

    console.print(f"\n[bold blue]Launching Apply[/bold blue]")
    console.print(f"  URL:      {url[:80]}...")
    console.print(f"  Model:    {model}")
    console.print(f"  Dry run:  {dry_run}")
    console.print(f"  Enrich:   {enrich}")
    console.print()

    apply_main(
        limit=1,
        target_url=url,
        min_score=1,  # bypass score filter — user explicitly chose this
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=False,
        workers=1,
    )


@app.command(name="apply-bulk")
def apply_bulk(
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Path to CSV/TXT file with URLs (one per line)."),
    enrich: bool = typer.Option(False, "--enrich", "-e", help="Scrape and score each job before applying."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    model: str = typer.Option("sonnet", "--model", "-m", help="Claude model name."),
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode."),
) -> None:
    """Apply to multiple job URLs from a file.

    Reads URLs from a CSV or text file (one URL per line), inserts into DB,
    and applies to each sequentially.

    Examples:
        applypilot apply-bulk --file urls.txt --dry-run
        applypilot apply-bulk --file jobs.csv --enrich --model sonnet
    """
    _bootstrap()

    from applypilot.config import check_tier
    import re

    check_tier(3, "auto-apply")

    if not file:
        console.print("[red]--file is required. Provide a CSV or text file with URLs.[/red]")
        raise typer.Exit(code=1)

    from pathlib import Path
    file_path = Path(file)
    if not file_path.exists():
        console.print(f"[red]File not found:[/red] {file}")
        raise typer.Exit(code=1)

    # Extract URLs from file
    text = file_path.read_text(encoding="utf-8", errors="replace")
    urls = re.findall(r'https?://[^\s,\"\'<>]+', text)
    urls = list(dict.fromkeys(urls))  # deduplicate preserving order

    if not urls:
        console.print("[red]No URLs found in file.[/red]")
        raise typer.Exit(code=1)

    console.print(f"[bold]Found {len(urls)} URLs[/bold]\n")
    for i, u in enumerate(urls[:10], 1):
        console.print(f"  {i}. {u[:80]}")
    if len(urls) > 10:
        console.print(f"  ... and {len(urls) - 10} more")
    console.print()

    # Process each URL
    from applypilot.apply.launcher import main as apply_main
    from applypilot.database import get_connection
    from applypilot.config import RESUME_PDF_PATH
    from urllib.parse import urlparse
    from datetime import datetime

    conn = get_connection()
    resume_path = str(RESUME_PDF_PATH) if RESUME_PDF_PATH.exists() else None

    for i, url in enumerate(urls, 1):
        console.print(f"\n[bold]── Job {i}/{len(urls)} ──[/bold]")
        console.print(f"  URL: {url[:80]}")

        # Ensure in DB
        row = conn.execute("SELECT url FROM jobs WHERE url = ?", (url,)).fetchone()
        if not row:
            domain = urlparse(url).hostname or "unknown"
            conn.execute("""
                INSERT OR IGNORE INTO jobs (url, title, site, discovered_at, tailored_resume_path)
                VALUES (?, ?, ?, ?, ?)
            """, (url, f"Quick Apply ({domain})", domain, datetime.now().isoformat(), resume_path))
            conn.commit()
        else:
            conn.execute(
                "UPDATE jobs SET tailored_resume_path = COALESCE(tailored_resume_path, ?) WHERE url = ?",
                (resume_path, url),
            )
            conn.commit()

        if enrich:
            console.print("  [yellow]Enriching...[/yellow]")
            try:
                from applypilot.server import _enrich_single_job
                _enrich_single_job(conn, url)
            except Exception as e:
                console.print(f"  [yellow]Enrich warning:[/yellow] {e}")

        console.print(f"  [blue]Applying ({'dry run' if dry_run else 'LIVE'})...[/blue]")
        try:
            apply_main(
                limit=1,
                target_url=url,
                min_score=1,
                headless=headless,
                model=model,
                dry_run=dry_run,
                continuous=False,
                workers=1,
            )
        except Exception as e:
            console.print(f"  [red]Error:[/red] {e}")

    console.print(f"\n[bold green]Done! Processed {len(urls)} URLs.[/bold green]")


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def serve(
    port: int = typer.Option(8899, help="Port to serve dashboard on."),
    host: str = typer.Option("127.0.0.1", help="Host to bind to."),
) -> None:
    """Launch the live dashboard with auto-apply buttons."""
    _bootstrap()

    import uvicorn
    from applypilot.server import app as web_app

    console.print(f"[green]Dashboard running at http://{host}:{port}[/green]")
    console.print("[dim]Press Ctrl+C to stop.[/dim]")
    uvicorn.run(web_app, host=host, port=port, log_level="warning")


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set GEMINI_API_KEY in ~/.applypilot/.env (run 'applypilot init')"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()


if __name__ == "__main__":
    app()
