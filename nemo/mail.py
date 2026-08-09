from __future__ import annotations

import argparse
from pathlib import Path

from .state import RunStore, read_json, read_jsonl


def _roles(run: Path) -> set[str]:
    meta = read_json(run / "run.json", {}) or {}
    return {str(item.get("id")) for item in meta.get("team", []) if isinstance(item, dict)} | {"all", "controller"}


def post(run: Path, sender: str, target: str, text: str, kind: str = "message") -> None:
    valid = _roles(run)
    if sender not in valid - {"all"}:
        raise ValueError(f"Unknown sender: {sender}")
    if target not in valid:
        raise ValueError(f"Unknown target: {target}")
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("Message cannot be empty")
    if len(cleaned) > 2400:
        raise ValueError("Message is too long; team chat is for concise coordination")
    RunStore(run).chat(sender, target, cleaned, kind)


def read(run: Path, recipient: str, limit: int = 30) -> list[dict[str, object]]:
    rows = read_jsonl(run / "chat.jsonl")
    visible = [row for row in rows if row.get("to") in {"all", recipient} or row.get("from") == recipient]
    return visible[-max(1, limit) :]


def main() -> int:
    parser = argparse.ArgumentParser(description="Nemo's concise inter-engineer chat helper.")
    sub = parser.add_subparsers(dest="command", required=True)
    send = sub.add_parser("post")
    send.add_argument("--run", type=Path, required=True)
    send.add_argument("--from", dest="sender", required=True)
    send.add_argument("--to", dest="target", default="all")
    send.add_argument("--text", required=True)
    send.add_argument("--kind", default="message", choices=("message", "claim", "blocker", "handoff", "interface"))
    inbox = sub.add_parser("read")
    inbox.add_argument("--run", type=Path, required=True)
    inbox.add_argument("--for", dest="recipient", required=True)
    inbox.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    run = args.run.resolve()
    if not (run / "run.json").is_file():
        parser.error(f"Not a Nemo run: {run}")
    if args.command == "post":
        post(run, args.sender, args.target, args.text, args.kind)
        print("message posted")
        return 0
    rows = read(run, args.recipient, args.limit)
    if not rows:
        print("No team messages yet. Continue coding; do not wait.")
        return 0
    for row in rows:
        target = "" if row.get("to") == "all" else f" -> {str(row.get('to')).upper()}"
        print(f"[{row.get('at')}] {str(row.get('from')).upper()}{target} ({row.get('kind')}): {row.get('text')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
