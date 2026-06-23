"""
Hermes Session → ExecutionLogRecord Ingestion Pipeline.

Converts native Hermes Agent session files into structured ExecutionLogRecords
for use by the quality gates pipeline and causal attribution engine.

This is the "run it on real data" step — turns your existing agent's
execution history into a data flywheel asset.

Usage:
    python -m agent_prod.ingest.hermes_sessions  # ingest all sessions
    python -m agent_prod.ingest.hermes_sessions --recent 10  # last 10
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click

from agent_prod.observability.execution_log import ExecutionLogRecord, ExecutionLogger

HERMES_SESSIONS_DIR = Path.home() / ".hermes" / "sessions"
DEFAULT_OUTPUT = "data/execution_log.jsonl"


def parse_hermes_session(session_path: Path) -> Optional[ExecutionLogRecord]:
    """Convert a single Hermes session file to an ExecutionLogRecord."""
    try:
        with open(session_path) as f:
            sess = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return None

    session_id = sess.get("session_id", "")
    if not session_id:
        return None

    messages = sess.get("messages", [])
    message_count = sess.get("message_count", len(messages))
    model = sess.get("model", "unknown")
    tools_used = [t.get("name", t) if isinstance(t, dict) else str(t)
                  for t in sess.get("tools", [])]

    # Extract last assistant response
    response = ""
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("content"):
            response = msg["content"]
            break

    # Extract last user prompt
    prompt = ""
    for msg in messages:
        if msg.get("role") == "user" and msg.get("content"):
            prompt = msg["content"]

    # Estimate tokens (rough heuristic: ~4 chars per token)
    prompt_chars = sum(len(str(m.get("content", "")))
                       for m in messages if m.get("role") in ("user", "system"))
    response_chars = sum(len(str(m.get("content", "")))
                         for m in messages if m.get("role") == "assistant")
    prompt_tokens = max(1, prompt_chars // 4)
    completion_tokens = max(1, response_chars // 4)

    # Parse timestamps
    session_start = sess.get("session_start", "")
    last_updated = sess.get("last_updated", "")
    try:
        start_dt = datetime.fromisoformat(session_start)
        end_dt = datetime.fromisoformat(last_updated)
        duration_ms = (end_dt - start_dt).total_seconds() * 1000 if end_dt > start_dt else 0
    except (ValueError, TypeError):
        duration_ms = 0

    return ExecutionLogRecord(
        run_id=session_id,
        session_id=session_id,
        prompt=prompt[:2000],
        response=response[:5000],
        turns=max(1, message_count // 2),  # rough: 2 messages per turn
        costs={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "duration_ms": duration_ms,
            "model": model,
        },
        duration_ms=duration_ms,
        tokens_used=prompt_tokens + completion_tokens,
        gate_passed=True,  # no gates ran on these historical sessions
        quality_gate_result={"status": "historical", "note": "Imported from Hermes session"},
        created_at=session_start or datetime.now(timezone.utc).isoformat(),
    )


def ingest_sessions(
    sessions_dir: Path = HERMES_SESSIONS_DIR,
    output_path: str = DEFAULT_OUTPUT,
    limit: Optional[int] = None,
) -> int:
    """Ingest Hermes session files into execution logs.

    Returns number of records ingested.
    """
    if not sessions_dir.exists():
        print(f"Sessions directory not found: {sessions_dir}")
        return 0

    session_files = sorted(sessions_dir.glob("session_*.json"), reverse=True)
    if limit:
        session_files = session_files[:limit]

    logger = ExecutionLogger(output_path)
    ingested = 0

    for fpath in session_files:
        record = parse_hermes_session(fpath)
        if record:
            logger.log_execution(record)
            ingested += 1

    return ingested


@click.command()
@click.option("--recent", type=int, default=None, help="Only ingest last N sessions")
@click.option("--output", default=DEFAULT_OUTPUT, help="Output JSONL path")
@click.option("--sessions-dir", default=str(HERMES_SESSIONS_DIR),
              help="Hermes sessions directory")
def main(recent: Optional[int], output: str, sessions_dir: str):
    """Ingest Hermes Agent sessions into ExecutionLogRecords.

    Produces a JSONL file consumable by the quality gates pipeline
    and causal attribution engine.
    """
    count = ingest_sessions(
        sessions_dir=Path(sessions_dir),
        output_path=output,
        limit=recent,
    )
    print(f"Ingested {count} execution records into {output}")

    # Show quick stats
    if count > 0:
        from agent_prod.observability.execution_log import ExecutionLogger
        logger = ExecutionLogger(output)
        stats = logger.get_stats()
        print(f"\nStats: {stats['total_records']} records")
        print(f"  Models: {stats.get('models', {})}")
        print(f"  Avg tokens: {stats.get('avg_tokens_used', 0):.0f}")
        print(f"  Avg duration: {stats.get('avg_duration_ms', 0):.0f}ms")


if __name__ == "__main__":
    main()
