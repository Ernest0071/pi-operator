"""Command line interface."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from pi_operator.config import settings

app = typer.Typer(add_completion=False, help="PI Operator — autonomous dealership workflows.")
console = Console()


@app.command()
def run(
    goal: str = typer.Argument(..., help="What the operator should accomplish."),
    target: str = typer.Option(None, "--target", "-t", help="Target adapter name."),
    headed: bool = typer.Option(False, "--headed", help="Show the browser."),
    check: str = typer.Option(None, "--check", help="JSON verification assertion."),
    approve_all: bool = typer.Option(
        False, "--approve-all", help="Auto-approve gated actions (unattended)."
    ),
    resume: bool = typer.Option(False, "--resume", help="Checkpoint the run to SQLite."),
) -> None:
    """Run one goal to completion."""
    from pi_operator.runner import auto_approve, print_summary, run_goal

    async def prompt_human(payload: dict) -> dict:
        if payload.get("type") == "approval":
            console.print("\n[yellow]APPROVAL REQUIRED[/]")
            console.print(f"  action: [bold]{payload.get('action')}[/]")
            console.print(f"  reason: {payload.get('reason')}")
            console.print(f"  risk:   {payload.get('risk')}")
            console.print(f"  args:   {json.dumps(payload.get('args', {}))[:300]}")
            ok = typer.confirm("  approve?", default=False)
            note = "" if ok else typer.prompt("  reason for rejection", default="")
            return {"approved": ok, "note": note}
        console.print(f"\n[cyan]THE OPERATOR IS ASKING:[/] {payload.get('question')}")
        return {"answer": typer.prompt("  your answer")}

    try:
        state = asyncio.run(
            run_goal(
                goal,
                target_name=target,
                headless=not headed,
                verification_check=json.loads(check) if check else None,
                on_interrupt=auto_approve if approve_all else prompt_human,
                checkpoint_path=(settings.runs_dir / "checkpoints.sqlite") if resume else None,
            )
        )
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from None
    print_summary(state)
    raise typer.Exit(0 if state.status.value == "succeeded" else 1)


@app.command()
def login(target: str = typer.Option(None, "--target", "-t")) -> None:
    """Authenticate once by hand and save the session for the operators to reuse.

    Seezar sends a one-time code by email, so there is no password an agent
    could hold. A human completes the code once; every run afterwards reuses
    the saved session until it expires.
    """
    import asyncio as _asyncio

    from pi_operator.browser.session import BrowserSession
    from pi_operator.targets import get_target

    adapter = get_target(target)
    if not hasattr(adapter, "bootstrap_login"):
        console.print(f"[red]{adapter.name} does not use interactive login.[/]")
        raise typer.Exit(2)

    async def go() -> bool:
        session = BrowserSession(headless=False, base_url=adapter.base_url)
        await session.start()
        try:
            console.print(f"Opening [bold]{adapter.base_url}/login[/] in a browser window.")
            if settings.target_username:
                console.print(f"Email [bold]{settings.target_username}[/] will be pre-filled.")
            console.print("\n  1. Request the one-time code")
            console.print("  2. Fetch it from your email and enter it")
            console.print("  3. Wait until the dashboard has loaded\n")

            async def wait_for_human() -> None:
                await _asyncio.get_running_loop().run_in_executor(
                    None, lambda: typer.prompt(
                        "Press Enter once you are signed in and can see the dashboard",
                        default="", show_default=False)
                )

            ok = await adapter.bootstrap_login(session, wait_for_human)
            if ok:
                await session.save_auth(adapter.auth_state_path)
            return ok
        finally:
            await session.close()

    if asyncio.run(go()):
        console.print(f"\n[green]Session saved[/] to {adapter.auth_state_path}")
        console.print("It is gitignored. Re-run `pi login` whenever it expires.")
    else:
        console.print("\n[red]Still not authenticated.[/] The session was not saved.")
        raise typer.Exit(1)


@app.command()
def recon(headed: bool = typer.Option(False, "--headed", help="Watch it work.")) -> None:
    """Survey the dashboard's real structure and record it into recon/."""
    from pi_operator.recon import run_recon
    from pi_operator.targets import get_target

    adapter = get_target()
    if not adapter.auth_state_path.exists() and not settings.target_password:
        console.print(f"[red]No saved session for {adapter.name}.[/] Run `pi login` first.")
        raise typer.Exit(2)
    console.print(f"Reconnoitring [bold]{settings.target_base_url}[/]\n")
    asyncio.run(run_recon(headless=not headed))


@app.command("scenario")
def scenario_cmd(
    which: str = typer.Argument(..., help="Which scenario to run: 'one' or 'four'."),
    date_range: str = typer.Option("30 Days", "--range", help="Analytics range."),
    compare_range: str = typer.Option("90 Days", "--compare", help="Scenario IV: second range."),
    limit: int = typer.Option(10, "--limit", help="Scenario IV: how many dealerships."),
    headed: bool = typer.Option(False, "--headed", help="Watch the browser."),
) -> None:
    """Run an assessment scenario and write its report to reports/."""
    from pi_operator.browser.session import BrowserSession
    from pi_operator.targets import get_target

    adapter = get_target()
    if not adapter.auth_state_path.exists():
        console.print("[red]No saved session.[/] Run `pi login` first.")
        raise typer.Exit(2)

    async def go():
        session = BrowserSession(headless=not headed, base_url=adapter.base_url,
                                 storage_state=adapter.auth_state_path)
        await session.start()
        try:
            if which.lower() in ("one", "1", "i"):
                from pi_operator.scenarios import scenario_one as mod

                result = await mod.run_scenario_one(session, adapter, date_range=date_range)
                return mod.save(result), result
            if which.lower() in ("four", "4", "iv"):
                from pi_operator.scenarios import scenario_four as mod

                result = await mod.run_scenario_four(
                    session, adapter, primary=date_range,
                    comparison=compare_range, limit=limit,
                )
                return mod.save(result), result
            console.print(f"[red]Unknown scenario {which!r}.[/] Use 'one' or 'four'.")
            raise typer.Exit(2)
        finally:
            await session.close()

    path, result = asyncio.run(go())
    console.print(f"\n[green]Report written:[/] {path}")
    for note in (getattr(result, "warnings", None) or getattr(result, "notes", []))[:6]:
        console.print(f"  [yellow]note:[/] {note}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8090, "--port"),
) -> None:
    """Start the operator API and console."""
    import uvicorn

    console.print(f"Operator console: [bold]http://{host}:{port}/[/]")
    uvicorn.run("pi_operator.api.main:app", host=host, port=port, log_level="info")


skills_app = typer.Typer(help="Inspect the skill library.")
app.add_typer(skills_app, name="skills")


@skills_app.command("list")
def skills_list(target: str = typer.Option(None, "--target", "-t")) -> None:
    """List available skills."""
    from pi_operator.skills.registry import SkillRegistry

    registry = SkillRegistry()
    name = target or settings.target
    table = Table(title=f"Skills for {name}")
    table.add_column("Skill")
    table.add_column("Params")
    table.add_column("Steps", justify="right")
    table.add_column("Description")

    from pi_operator.skills.base import RecordedSkill

    found = registry.for_target(name)
    for skill in found:
        if isinstance(skill, RecordedSkill):
            table.add_row(skill.name, ", ".join(skill.params) or "—",
                          str(len(skill.steps)), skill.description)
        else:
            table.add_row(skill.skill_name, "—", "—", skill.skill_description)
    if not found:
        console.print("[yellow]No skills yet.[/] Skills are promoted from successful runs.")
        return
    console.print(table)


@app.command()
def runs(limit: int = typer.Option(10, "--limit", "-n")) -> None:
    """List recent runs."""
    root: Path = settings.runs_dir
    if not root.exists():
        console.print("No runs yet.")
        return

    table = Table(title="Recent runs")
    for column in ("Run", "Status", "Steps", "Cost", "Goal"):
        table.add_column(column)

    entries = sorted(root.glob("*/summary.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in entries[:limit]:
        data = json.loads(path.read_text())
        table.add_row(data["run_id"], data["status"], str(data["steps"]),
                      f"${data['usd']:.3f}", data["goal"][:60])
    console.print(table)


@app.command()
def trace(run_id: str = typer.Argument(...)) -> None:
    """Open a run's HTML audit report."""
    import webbrowser

    path = settings.runs_dir / run_id / "report.html"
    if not path.exists():
        console.print(f"[red]No report at {path}[/]")
        raise typer.Exit(1)
    webbrowser.open(path.as_uri())
    console.print(f"Opened {path}")


if __name__ == "__main__":
    app()
