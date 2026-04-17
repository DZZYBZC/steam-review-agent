"""
Manual CLI for managing cluster note lifecycle.

Cluster notes are written automatically by the agent (investigator auto-saves
known_issues, human_approval saves response_history / human_feedback), but
nothing flips them to 'resolved' when the underlying issue is fixed. This CLI
also exposes the promotion gate: notes with promoted_at=NULL are invisible to
the investigator at read time. known_issue notes always start unpromoted and
must be manually promoted here before they influence future runs.

Usage:
    python resolve_note.py list <app_id> <category> [--include-resolved] [--include-stale] [--include-unpromoted]
    python resolve_note.py resolve <note_id>
    python resolve_note.py reactivate <note_id>
    python resolve_note.py promote <note_id>
    python resolve_note.py demote <note_id>
"""

import argparse
import logging
import sys

from config import CLUSTER_NOTE_STATUSES
from pipeline.storage import (
    get_connection,
    update_cluster_note_status,
    promote_cluster_note,
    demote_cluster_note,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def cmd_list(args: argparse.Namespace) -> int:
    """
    load_cluster_notes filters promoted_at IS NOT NULL for production reads,
    so listing for the promotion workflow needs a direct query that optionally
    includes unpromoted candidates.
    """
    query = (
        "SELECT id, note_type, status, promoted_at, promoted_by, "
        "source_review_id, created_by, created_at, updated_at, "
        "COALESCE(is_eval, 0), note_text "
        "FROM cluster_notes WHERE app_id = ? AND category = ?"
    )
    params: list = [args.app_id, args.category]
    if not args.include_resolved:
        query += " AND status = 'active'"
    if not args.include_unpromoted:
        query += " AND promoted_at IS NOT NULL"
    query += " ORDER BY updated_at DESC"

    conn = get_connection()
    try:
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
    finally:
        conn.close()

    if not rows:
        print(f"No notes found for {args.app_id}/{args.category}.")
        return 0

    print(f"{len(rows)} note(s) for {args.app_id}/{args.category}:")
    print("-" * 110)
    for r in rows:
        (note_id, note_type, status, promoted_at, promoted_by, source,
         created_by, created_at, updated_at, is_eval, note_text) = r
        text_preview = (note_text or "").replace("\n", " ")[:70]
        promoted_tag = f"PROMOTED({promoted_by})" if promoted_at else "unpromoted"
        eval_tag = " [eval]" if is_eval else ""
        print(
            f"  id={note_id:<5} "
            f"status={status:<9} "
            f"type={note_type:<18} "
            f"{promoted_tag:<25} "
            f"src={source or '-':<12}{eval_tag} "
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


def cmd_promote(args: argparse.Namespace) -> int:
    conn = get_connection()
    try:
        ok = promote_cluster_note(conn, args.note_id, promoted_by="manual")
    finally:
        conn.close()
    if not ok:
        print(f"Failed to promote note {args.note_id} — see log above.", file=sys.stderr)
        return 1
    print(f"Note {args.note_id} promoted.")
    return 0


def cmd_demote(args: argparse.Namespace) -> int:
    conn = get_connection()
    try:
        ok = demote_cluster_note(conn, args.note_id)
    finally:
        conn.close()
    if not ok:
        print(f"Failed to demote note {args.note_id} — see log above.", file=sys.stderr)
        return 1
    print(f"Note {args.note_id} demoted.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage cluster note lifecycle (list / resolve / reactivate / promote / demote).",
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
        help="(kept for CLI compatibility — not enforced; use direct SQL if needed)",
    )
    p_list.add_argument(
        "--include-unpromoted",
        action="store_true",
        help="Also show notes that have not been promoted (candidates for `promote`).",
    )

    p_resolve = sub.add_parser("resolve", help="Mark a note as resolved.")
    p_resolve.add_argument("note_id", type=int)

    p_reactivate = sub.add_parser("reactivate", help="Flip a resolved note back to active.")
    p_reactivate.add_argument("note_id", type=int)

    p_promote = sub.add_parser(
        "promote",
        help="Promote a note (makes it visible to the investigator at read time).",
    )
    p_promote.add_argument("note_id", type=int)

    p_demote = sub.add_parser(
        "demote",
        help="Demote a note (clears promoted_at, hides from investigator).",
    )
    p_demote.add_argument("note_id", type=int)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "list":
        return cmd_list(args)
    if args.command == "resolve":
        return cmd_set_status(args, "resolved")
    if args.command == "reactivate":
        return cmd_set_status(args, "active")
    if args.command == "promote":
        return cmd_promote(args)
    if args.command == "demote":
        return cmd_demote(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
