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


@app.command("eval")
def eval_cmd(
    scenario: str = typer.Option(None, "--scenario", "-s", help="Run one scenario by id."),
    workflow: str = typer.Option(None, "--workflow", "-w", help="Run one workflow's scenarios."),
    target: str = typer.Option(None, "--target", "-t"),
    headed: bool = typer.Option(False, "--headed"),
) -> None:
    """Run the eval suite and write EVAL_REPORT.md."""
    from evals.harness import run_suite
    from evals.scenarios import SCENARIOS, by_id, by_workflow

    if scenario:
        chosen = [by_id(scenario)]
    elif workflow:
        chosen = by_workflow(workflow)
    else:
        chosen = SCENARIOS

    console.print(f"Running {len(chosen)} scenario(s) against "
                  f"[bold]{target or settings.target}[/]\n")
    report = asyncio.run(run_suite(chosen, target_name=target, headless=not headed))

    summary = report.summary()
    table = Table(title="Eval summary", show_header=False)
    for key, value in summary.items():
        table.add_row(key.replace("_", " "), str(value))
    console.print(table)
    console.print("\nWrote [bold]EVAL_REPORT.md[/] and eval_results.json")
    raise typer.Exit(0 if report.passed == report.total else 1)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8090, "--port"),
) -> None:
    """Start the operator API and console."""
    import uvicorn

    console.print(f"Operator console: [bold]http://{host}:{port}/[/]")
    uvicorn.run("pi_operator.api.main:app", host=host, port=port, log_level="info")


@app.command()
def dms(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8080, "--port"),
    reset: bool = typer.Option(False, "--reset", help="Reseed the fixture database first."),
) -> None:
    """Start the mock DMS (the eval fixture)."""
    import uvicorn

    if reset:
        from mock_dms import db

        db.reset()
        console.print("Reseeded the mock DMS database.")
    console.print(f"Mock DMS: [bold]http://{host}:{port}/[/]  (operator / operator)")
    uvicorn.run("mock_dms.app:app", host=host, port=port, log_level="info")


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
