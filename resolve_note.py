"""
Manual CLI for managing cluster note lifecycle.

Cluster notes are written automatically by the agent (investigator auto-saves
known_issues, human_approval saves response_history / human_feedback), but
nothing flips them to 'resolved' when the underlying issue is fixed. This CLI
exposes the existing update_cluster_note_status function so a human can clean
up stale notes by hand.

Usage:
    python resolve_note.py list <app_id> <category> [--include-resolved] [--include-stale]
    python resolve_note.py resolve <note_id>
    python resolve_note.py reactivate <note_id>
"""

import argparse
import logging
import sys

from config import CLUSTER_NOTE_STATUSES
from pipeline.storage import (
    get_connection,
    load_cluster_notes,
    update_cluster_note_status,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def cmd_list(args: argparse.Namespace) -> int:
    status = None if args.include_resolved else "active"
    conn = get_connection()
    try:
        notes = load_cluster_notes(
            conn,
            app_id=args.app_id,
            category=args.category,
            status=status,
            include_stale=args.include_stale,
        )
    finally:
        conn.close()

    if not notes:
        print(f"No notes found for {args.app_id}/{args.category}.")
        return 0

    print(f"{len(notes)} note(s) for {args.app_id}/{args.category}:")
    print("-" * 100)
    for n in notes:
        text_preview = (n["note_text"] or "").replace("\n", " ")[:80]
        source = n.get("source_review_id") or "-"
        print(
            f"  id={n['id']:<5} "
            f"status={n['status']:<9} "
            f"type={n['note_type']:<18} "
            f"updated={n['updated_at']} "
            f"src={source:<12} "
            f"| {text_preview}"
        )
    return 0


def cmd_set_status(args: argparse.Namespace, status: str) -> int:
    if status not in CLUSTER_NOTE_STATUSES:
        print(
            f"Invalid status '{status}'. Must be one of {CLUSTER_NOTE_STATUSES}.",
            file=sys.stderr,
        )
        return 2

    conn = get_connection()
    try:
        ok = update_cluster_note_status(conn, args.note_id, status)
    finally:
        conn.close()

    if not ok:
        print(f"Failed to update note {args.note_id} — see log above.", file=sys.stderr)
        return 1
    print(f"Note {args.note_id} -> {status}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage cluster note lifecycle (list / resolve / reactivate).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List cluster notes for an app+category.")
    p_list.add_argument("app_id", help="Steam app id (e.g. 2183900)")
    p_list.add_argument("category", help="Cluster category (e.g. technical_issues)")
    p_list.add_argument(
        "--include-resolved",
        action="store_true",
        help="Also show notes with status='resolved'.",
    )
    p_list.add_argument(
        "--include-stale",
        action="store_true",
        help="Also show notes older than CLUSTER_NOTE_STALENESS_DAYS.",
    )

    p_resolve = sub.add_parser("resolve", help="Mark a note as resolved.")
    p_resolve.add_argument("note_id", type=int)

    p_reactivate = sub.add_parser("reactivate", help="Flip a resolved note back to active.")
    p_reactivate.add_argument("note_id", type=int)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "list":
        return cmd_list(args)
    if args.command == "resolve":
        return cmd_set_status(args, "resolved")
    if args.command == "reactivate":
        return cmd_set_status(args, "active")
    return 2


if __name__ == "__main__":
    sys.exit(main())
