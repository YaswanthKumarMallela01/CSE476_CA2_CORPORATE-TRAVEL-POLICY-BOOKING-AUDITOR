"""
CLI entry point for the Corporate Travel Policy Booking Auditor.

Commands:
  python main.py audit "<request text>"    - Audit a single travel request
  python main.py eval                      - Run full evaluation suite
  python main.py review list               - List pending reviews
  python main.py review show <request_id>  - Show review details
  python main.py review decide <id> --verdict approve|reject|modify --note "..."
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import config
from core.memory import MemoryStore
from core.rag import PolicyRAG
from core.schemas import Decision

console = Console()


def get_memory() -> MemoryStore:
    """Create and seed a MemoryStore."""
    memory = MemoryStore(config.SQLITE_DB_PATH)
    # Only seed if employees table is empty
    row = memory.conn.execute("SELECT COUNT(*) as cnt FROM employees").fetchone()
    if row["cnt"] == 0:
        memory.seed_from_json(
            config.EMPLOYEES_PATH,
            config.BUDGETS_PATH,
            config.TRIP_HISTORY_SEED_PATH,
        )
    return memory


def get_rag() -> PolicyRAG:
    """Create and ingest the PolicyRAG."""
    rag = PolicyRAG(
        persist_dir=config.CHROMA_PERSIST_DIR,
        embedding_model=config.EMBEDDING_MODEL_NAME,
        top_k=config.RAG_TOP_K,
    )
    rag.ingest(config.POLICY_PATH)
    return rag


async def cmd_audit(raw_text: str, use_llm: bool = True, allow_clarification: bool = False) -> None:
    """Audit a single travel request (with optional interactive follow-up questions)."""
    from agents.approval_manager import ApprovalManager

    memory = get_memory()
    rag = get_rag()
    manager = ApprovalManager(memory, rag, use_llm=use_llm)

    console.print(Panel(raw_text, title="Travel Request", border_style="blue"))
    result = await manager.process_request(raw_text, allow_clarification=allow_clarification)

    # Interactive follow-up loop if clarification is required
    if result.decision == Decision.NEEDS_INFO:
        console.print(Panel(result.clarification_message, title="Follow-Up Questions Required", border_style="yellow"))
        clarifications = {}
        for q in result.follow_up_questions:
            if not q.get("required", True):
                continue
            opts = q.get("options")
            if opts and q["field"] == "employee_id":
                console.print("[cyan]Available Employees:[/cyan] " + ", ".join(o["label"] for o in opts))
            val = console.input(f"[bold yellow]{q['question']}[/bold yellow] ").strip()
            field_map = {"origin_iata": "origin", "destination_iata": "destination"}
            clarifications[field_map.get(q["field"], q["field"])] = val
        result = await manager.process_request(
            raw_text,
            allow_clarification=False,
            clarifications=clarifications,
            request_id=result.request_id,
        )

    # Display result
    color = {"APPROVE": "green", "MODIFY": "yellow", "ESCALATE": "red"}.get(result.decision.value, "white")
    table = Table(title="Audit Result", show_header=True)
    table.add_column("Field", style="cyan")
    table.add_column("Value", style=color)
    table.add_row("Request ID", result.request_id)
    if result.employee_id:
        table.add_row("Employee", f"{result.employee_name} ({result.employee_id}, {result.employee_department}, {result.employee_grade})")
    if result.origin_iata or result.destination_iata:
        table.add_row("Route & Date", f"{result.origin_iata} -> {result.destination_iata} | Depart: {result.depart_date}")
    table.add_row("Decision", result.decision.value)
    table.add_row("Primary Clause", result.primary_clause_id)
    table.add_row("Secondary Clauses", ", ".join(result.secondary_clause_ids) or "None")
    if result.best_compliant_fare:
        fare = result.best_compliant_fare
        table.add_row("Best Fare", f"{fare.airline} INR {fare.price_inr:,.0f} ({fare.cabin.value})")
    elif result.recommended_fare:
        rf = result.recommended_fare
        table.add_row("Best Fare", f"None (Lowest Searched: {rf.airline} INR {rf.price_inr:,.0f})")
    else:
        table.add_row("Best Fare", "None")
    table.add_row("Justification", result.justification)
    table.add_row("Status", result.status.value)
    table.add_row("Routed to Human", str(result.routed_to_human))
    console.print(table)

    memory.close()


async def cmd_eval() -> None:
    """Run the full evaluation suite."""
    # Import here to avoid circular imports
    sys.path.insert(0, str(Path(__file__).parent))
    from eval.run_eval import run_evaluation
    await run_evaluation()


def cmd_review_list() -> None:
    """List review queue items."""
    from core.hitl import get_all_reviews
    memory = get_memory()
    items = get_all_reviews(memory)
    if not items:
        console.print("[yellow]No items in review queue.[/yellow]")
        return
    table = Table(title="Review Queue")
    table.add_column("Request ID", style="cyan")
    table.add_column("Employee")
    table.add_column("Route / Date")
    table.add_column("Fare (INR)")
    table.add_column("Clause")
    table.add_column("Status", style="bold")
    table.add_column("Verdict")
    table.add_column("Note")
    for item in items:
        aud = json.loads(item.audit_result_json) if item.audit_result_json else {}
        emp_lbl = f"{aud.get('employee_name', '')} ({aud.get('employee_id', '?')})"
        route_lbl = f"{aud.get('origin_iata', '?')}->{aud.get('destination_iata', '?')} ({aud.get('depart_date', '?')})"
        fare_obj = aud.get("best_compliant_fare") or aud.get("recommended_fare") or {}
        fare_lbl = f"INR {fare_obj['price_inr']:,.0f}" if fare_obj.get("price_inr") else "-"
        color = {"PENDING": "yellow", "HUMAN_APPROVED": "green", "HUMAN_REJECTED": "red", "HUMAN_MODIFIED": "blue"}.get(item.status, "white")
        table.add_row(
            item.request_id,
            emp_lbl,
            route_lbl,
            fare_lbl,
            aud.get("primary_clause_id", "-"),
            f"[{color}]{item.status}[/{color}]",
            item.human_verdict or "-",
            (item.human_note or "-")[:40],
        )
    console.print(table)
    memory.close()


def cmd_review_show(request_id: str) -> None:
    """Show details of a review item."""
    memory = get_memory()
    item = memory.get_review_item(request_id)
    if not item:
        console.print(f"[red]Request {request_id} not found in review queue.[/red]")
        return

    # Parse and display the audit result
    console.print(Panel(f"Review Item: {request_id}", border_style="cyan"))
    console.print(f"Status: [bold]{item.status}[/bold]")
    if item.audit_result_json:
        audit = json.loads(item.audit_result_json)
        console.print(f"Decision: {audit.get('decision', 'N/A')}")
        console.print(f"Primary Clause: {audit.get('primary_clause_id', 'N/A')}")
        console.print(f"Justification: {audit.get('justification', 'N/A')}")
    if item.human_verdict:
        console.print(f"Human Verdict: {item.human_verdict}")
        console.print(f"Note: {item.human_note}")
        console.print(f"Reviewed At: {item.reviewed_at}")

    # Show audit trail
    logs = memory.get_audit_log(request_id)
    if logs:
        log_table = Table(title="Audit Trail")
        log_table.add_column("Agent")
        log_table.add_column("Action")
        log_table.add_column("Time")
        for entry in logs:
            log_table.add_row(entry.agent, entry.action, str(entry.ts or ""))
        console.print(log_table)

    memory.close()


def cmd_review_decide(request_id: str, verdict: str, note: str, reviewer: str = "admin") -> None:
    """Submit a human verdict."""
    from core.hitl import submit_verdict
    memory = get_memory()

    # Determine fare from audit result
    item = memory.get_review_item(request_id)
    fare_inr = None
    if item and item.audit_result_json:
        audit = json.loads(item.audit_result_json)
        if audit.get("best_compliant_fare"):
            fare_inr = audit["best_compliant_fare"].get("price_inr")

    try:
        result = submit_verdict(request_id, verdict, note, reviewer, memory, fare_inr)
        console.print(f"[green]✓ Verdict recorded:[/green] {result}")
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
    memory.close()


def main():
    parser = argparse.ArgumentParser(
        description="Corporate Travel Policy Booking Auditor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

    # audit command
    audit_parser = subparsers.add_parser("audit", help="Audit a single travel request")
    audit_parser.add_argument("request", help="Natural language travel request text")
    audit_parser.add_argument("--no-llm", action="store_true", help="Disable LLM (use fallbacks)")
    audit_parser.add_argument("--interactive", action="store_true", help="Ask follow-up questions if info is missing")

    # eval command
    subparsers.add_parser("eval", help="Run full evaluation suite")

    # review commands
    review_parser = subparsers.add_parser("review", help="Manage human review queue")
    review_sub = review_parser.add_subparsers(dest="review_action")
    review_sub.add_parser("list", help="List all reviews")
    show_parser = review_sub.add_parser("show", help="Show review details")
    show_parser.add_argument("request_id", help="Request ID to show")
    decide_parser = review_sub.add_parser("decide", help="Submit a verdict")
    decide_parser.add_argument("request_id", help="Request ID")
    decide_parser.add_argument("--verdict", required=True, choices=["approve", "reject", "modify"])
    decide_parser.add_argument("--note", default="", help="Reviewer note")
    decide_parser.add_argument("--reviewer", default="admin", help="Reviewer name")

    args = parser.parse_args()

    if args.command == "audit":
        asyncio.run(cmd_audit(args.request, use_llm=not args.no_llm, allow_clarification=args.interactive))
    elif args.command == "eval":
        asyncio.run(cmd_eval())
    elif args.command == "review":
        if args.review_action == "list":
            cmd_review_list()
        elif args.review_action == "show":
            cmd_review_show(args.request_id)
        elif args.review_action == "decide":
            cmd_review_decide(args.request_id, args.verdict, args.note, args.reviewer)
        else:
            review_parser.print_help()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
