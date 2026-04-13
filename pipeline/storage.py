"""
SQLite database operations for storing and retrieving reviews.
"""

import sqlite3
import logging
import pandas as pd
import json
from datetime import datetime, timedelta
from typing import Any, Mapping
from config import (
    DB_PATH,
    CONFIDENCE_THRESHOLD,
    CLUSTER_NOTE_STALENESS_DAYS,
    CLUSTER_NOTE_DEDUP_WINDOW_HOURS,
)

logger = logging.getLogger(__name__)


def _apply_migration(
    conn: sqlite3.Connection,
    version: int,
    description: str,
    sql_statements: list[str],
) -> None:
    """
    Apply a numbered schema migration if not already applied.

    Each statement is wrapped in try/except OperationalError to make
    re-application safe on existing DBs that already have the column —
    preserving the semantics of the previous ad-hoc ALTER pattern.

    Records the migration in schema_version after all statements run.
    """
    cur = conn.execute("SELECT 1 FROM schema_version WHERE version = ?", (version,))
    if cur.fetchone():
        return
    for sql in sql_statements:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            logger.debug(f"Migration {version} statement skipped (likely already applied): {e}")
    conn.execute(
        "INSERT INTO schema_version (version, description) VALUES (?, ?)",
        (version, description),
    )
    logger.info(f"Applied schema migration {version}: {description}")


def get_connection() -> sqlite3.Connection:
    """
    Open a connection to the SQLite database.

    Returns:
        A database connection object.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def create_tables(conn: sqlite3.Connection) -> None:
    """
    Create the reviews table if it doesn't already exist.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            review_id TEXT PRIMARY KEY,
            app_id TEXT,
            steam_id TEXT,
            review_text TEXT,
            voted_up BOOLEAN,
            timestamp TEXT,
            playtime_hours REAL,
            votes_up INTEGER DEFAULT 0,
            votes_funny INTEGER DEFAULT 0,
            weighted_vote_score REAL DEFAULT 0,
            is_near_duplicate BOOLEAN DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS classifications (
            review_id TEXT PRIMARY KEY,
            app_id TEXT,
            primary_category TEXT NOT NULL,
            secondary_categories TEXT,
            secondary_aspects TEXT,
            confidence REAL NOT NULL,
            reasoning TEXT,
            needs_review BOOLEAN DEFAULT 0,
            model_used TEXT,
            classified_at TEXT,
            FOREIGN KEY (review_id) REFERENCES reviews(review_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id TEXT NOT NULL,
            review_id TEXT NOT NULL,
            category TEXT NOT NULL,
            review_text TEXT NOT NULL,
            review_tone TEXT,
            evidence_summary TEXT,
            evidence_confidence REAL,
            drafted_response TEXT NOT NULL,
            proposed_action TEXT,
            source_ids_cited TEXT,
            critique TEXT,
            human_decision TEXT NOT NULL,
            human_feedback TEXT,
            iteration_count INTEGER,
            stop_reason TEXT,
            retrieval_decision TEXT,
            token_usage TEXT,
            run_id TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Backfill retrieval_decision column on pre-existing audit_log tables (idempotent)
    try:
        conn.execute("ALTER TABLE audit_log ADD COLUMN retrieval_decision TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log_iterations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id TEXT NOT NULL,
            review_id TEXT NOT NULL,
            iteration INTEGER NOT NULL,
            drafted_response TEXT,
            proposed_action TEXT,
            source_ids_cited TEXT,
            critique TEXT,
            approved INTEGER NOT NULL,
            revision_reason TEXT,
            reason_type TEXT,
            retrieval_hint TEXT,
            token_usage TEXT,
            run_id TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS cluster_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id TEXT NOT NULL,
            category TEXT NOT NULL,
            note_type TEXT NOT NULL,
            tags TEXT,
            note_text TEXT NOT NULL,
            created_by TEXT DEFAULT 'system',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Schema version tracking — bookkeeping table for numbered migrations.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Numbered schema migrations. Each runs at most once per DB.
    _apply_migration(
        conn,
        version=1,
        description="cluster_notes: add status and source_review_id",
        sql_statements=[
            "ALTER TABLE cluster_notes ADD COLUMN status TEXT DEFAULT 'active'",
            "ALTER TABLE cluster_notes ADD COLUMN source_review_id TEXT",
        ],
    )

    _apply_migration(
        conn,
        version=2,
        description="audit_log + audit_log_iterations: add run_id for join correctness",
        sql_statements=[
            "ALTER TABLE audit_log ADD COLUMN run_id TEXT",
            "ALTER TABLE audit_log_iterations ADD COLUMN run_id TEXT",
        ],
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_app_id ON reviews(app_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_classifications_app_id ON classifications(app_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_app_category ON audit_log(app_id, category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_feedback ON audit_log(app_id, category, human_decision)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cluster_notes_app_category ON cluster_notes(app_id, category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cluster_notes_status ON cluster_notes(app_id, category, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cluster_notes_review ON cluster_notes(source_review_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_iter_review ON audit_log_iterations(app_id, review_id, iteration)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_run ON audit_log(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_iter_run ON audit_log_iterations(run_id)")

    conn.commit()
    logger.info("Database tables ready.")


def save_reviews(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """
    Save a DataFrame of reviews to the database.

    Returns:
        The number of new reviews inserted.
    """
    cursor = conn.cursor()
    inserted = 0

    for _, row in df.iterrows():
        try:
            cursor.execute("""
                INSERT OR IGNORE INTO reviews
                (review_id, app_id, steam_id, review_text, voted_up, timestamp, playtime_hours,
                 votes_up, votes_funny, weighted_vote_score, is_near_duplicate)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["review_id"],
                row["app_id"],
                row["steam_id"],
                row["review_text"],
                bool(row["voted_up"]),
                str(row["timestamp"]),
                row["playtime_hours"],
                int(row["votes_up"]),
                int(row["votes_funny"]),
                row["weighted_vote_score"],
                bool(row["is_near_duplicate"]),
            ))
            if cursor.rowcount > 0:
                inserted += 1
        except sqlite3.Error as e:
            logger.warning(f"Failed to insert review {row['review_id']}: {e}")

    conn.commit()
    logger.info(f"Saved {inserted} new reviews to database (skipped {len(df) - inserted} duplicates).")

    return inserted


def load_reviews(conn: sqlite3.Connection, app_id: str | None = None, exclude_duplicates: bool = True) -> pd.DataFrame:
    """
    Load reviews from the database into a DataFrame.

    Parameters:
        conn: Database connection.
        exclude_duplicates: If True, skip reviews flagged as near-duplicates.

    Returns:
        A DataFrame of reviews.
    """
    query = "SELECT * FROM reviews WHERE 1=1"
    params = []
    if app_id:
        query += " AND app_id = ?"
        params.append(app_id)
    if exclude_duplicates:
        query += " AND is_near_duplicate = 0"
    df = pd.read_sql_query(query, conn, params=params)

    logger.info(f"Loaded {len(df)} reviews from database.")
    return df


def count_reviews(conn: sqlite3.Connection) -> int:
    """Return the total number of reviews in the database."""
    cursor = conn.execute("SELECT COUNT(*) FROM reviews")
    return cursor.fetchone()[0]

def save_classification(
    conn: sqlite3.Connection,
    review_id: str,
    app_id: str,
    result,  # ClassificationResult from Pydantic in classify.py
    model_used: str,
) -> bool:
    """
    Save a classification result to the database.

    Returns:
        True if inserted, False if this review was already classified.
    """
    try:
        cursor = conn.execute("""
            INSERT OR IGNORE INTO classifications
            (review_id, app_id, primary_category, secondary_categories,
             secondary_aspects, confidence, reasoning, needs_review,
             model_used, classified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            review_id,
            app_id,
            result.primary_category,
            json.dumps(result.secondary_categories),
            json.dumps(result.secondary_aspects),
            result.confidence,
            result.reasoning,
            result.confidence < CONFIDENCE_THRESHOLD,
            model_used,
        ))
        conn.commit()
        inserted = cursor.rowcount > 0
        if inserted:
            logger.debug(f"Saved classification for review {review_id}")
        return inserted
    except sqlite3.Error as e:
        logger.error(f"Failed to save classification for {review_id}: {e}")
        return False
    
def get_unclassified_reviews(
    conn: sqlite3.Connection,
    app_id: str,
) -> pd.DataFrame:
    """
    Load reviews that haven't been classified yet.
    """
    query = """
        SELECT r.* FROM reviews r
        LEFT JOIN classifications c ON r.review_id = c.review_id
        WHERE c.review_id IS NULL
          AND r.app_id = ?
          AND r.is_near_duplicate = 0
    """
    df = pd.read_sql_query(query, conn, params=[app_id])
    logger.info(f"Found {len(df)} unclassified reviews for app {app_id}.")
    return df

def load_classifications(
    conn: sqlite3.Connection,
    app_id: str | None = None,
) -> pd.DataFrame:
    """
    Load all classifications, optionally filtered by app_id.
    Parses the secondary_categories and secondary_aspects JSON strings back into lists.
    """
    query = "SELECT * FROM classifications WHERE 1=1"
    params = []
    if app_id:
        query += " AND app_id = ?"
        params.append(app_id)

    df = pd.read_sql_query(query, conn, params=params)

    if len(df) > 0:
        if "secondary_categories" in df.columns:
            df["secondary_categories"] = df["secondary_categories"].apply(
                lambda x: json.loads(x) if x else []
            )
        if "secondary_aspects" in df.columns:
            df["secondary_aspects"] = df["secondary_aspects"].apply(
                lambda x: json.loads(x) if x else []
            )

    logger.info(f"Loaded {len(df)} classifications.")
    return df

def load_classified_reviews(
    conn: sqlite3.Connection,
    app_id: str,
    include_other: bool = False,
) -> pd.DataFrame:
    """
    Load non-duplicate reviews joined with their classifications.

    By default the 'other' bucket is excluded — it's a catchall category
    that the production pipeline (clustering, agent runs) ignores. Eval
    code that needs to inspect 'other' cases (golden set annotation,
    gating accuracy scorer) passes include_other=True.
    """
    query = """
        SELECT r.review_id, r.app_id, r.review_text, r.voted_up, r.timestamp,
               r.playtime_hours, r.votes_up, r.weighted_vote_score,
               c.primary_category, c.secondary_categories,
               c.secondary_aspects, c.confidence
        FROM reviews r
        JOIN classifications c ON r.review_id = c.review_id
        WHERE r.app_id = ?
          AND r.is_near_duplicate = 0
    """
    if not include_other:
        query += " AND c.primary_category != 'other'"
    df = pd.read_sql_query(query, conn, params=[app_id])

    if len(df) > 0:
        if "secondary_categories" in df.columns:
            df["secondary_categories"] = df["secondary_categories"].apply(
                lambda x: json.loads(x) if x else []
            )
        if "secondary_aspects" in df.columns:
            df["secondary_aspects"] = df["secondary_aspects"].apply(
                lambda x: json.loads(x) if x else []
            )

    logger.info(f"Loaded {len(df)} classified reviews for app {app_id}.")
    return df


def save_audit_entry(conn: sqlite3.Connection, state: dict) -> bool:
    """
    Save a completed agent run to the audit log.

    Extracts evidence_summary and evidence_confidence from the
    evidence_package dict. Serializes lists/dicts as JSON strings.

    Returns:
        True if inserted successfully, False on error.
    """
    evidence = state.get("evidence_package", {})
    cluster = state.get("cluster_summary", {})

    try:
        conn.execute("""
            INSERT INTO audit_log
            (app_id, review_id, category, review_text, review_tone,
             evidence_summary, evidence_confidence, drafted_response,
             proposed_action, source_ids_cited, critique, human_decision,
             human_feedback, iteration_count, stop_reason, retrieval_decision,
             token_usage, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            state.get("app_id", ""),
            state.get("review_id", ""),
            cluster.get("category", ""),
            state.get("review_text", ""),
            state.get("review_tone", ""),
            evidence.get("summary", ""),
            evidence.get("confidence", 0.0),
            state.get("drafted_response", ""),
            state.get("proposed_action", ""),
            json.dumps(state.get("source_ids_cited", [])),
            state.get("critique", ""),
            state.get("human_decision", ""),
            state.get("human_feedback", ""),
            state.get("iteration_count", 0),
            state.get("stop_reason", ""),
            evidence.get("retrieval_decision", ""),
            json.dumps(state.get("token_usage", {})),
            state.get("run_id", ""),
        ))
        conn.commit()
        logger.info(f"Saved audit entry for review {state.get('review_id', '???')}.")
        return True
    except sqlite3.Error as e:
        logger.error(f"Failed to save audit entry: {e}")
        return False


def save_audit_iteration(
    conn: sqlite3.Connection,
    state: Mapping[str, Any],
    iteration: int,
    approved: bool,
    critique: str,
    revision_reason: str,
    reason_type: str | None,
    retrieval_hint: str | None,
    tokens: dict | None,
) -> bool:
    """
    Save one critic pass (one iteration) to audit_log_iterations.

    Called fire-and-forget from the Critic node. Captures per-iteration
    rejection reasons that audit_log (one-row-per-run) overwrites.

    Returns:
        True if inserted successfully, False on error.
    """
    try:
        conn.execute("""
            INSERT INTO audit_log_iterations
            (app_id, review_id, iteration, drafted_response, proposed_action,
             source_ids_cited, critique, approved, revision_reason,
             reason_type, retrieval_hint, token_usage, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            state.get("app_id", ""),
            state.get("review_id", ""),
            iteration,
            state.get("drafted_response", ""),
            state.get("proposed_action", ""),
            json.dumps(state.get("source_ids_cited", [])),
            critique,
            1 if approved else 0,
            revision_reason,
            reason_type,
            retrieval_hint,
            json.dumps(tokens or {}),
            state.get("run_id", ""),
        ))
        conn.commit()
        logger.debug(
            f"Saved audit iteration {iteration} "
            f"for review {state.get('review_id', '???')} "
            f"(approved={approved})."
        )
        return True
    except sqlite3.Error as e:
        logger.error(f"Failed to save audit iteration: {e}")
        return False


def get_iteration_drafts_batch(
    conn: sqlite3.Connection,
    run_ids: list[str],
    iteration: int = 0,
) -> dict[str, dict]:
    """
    Batch-fetch one iteration's row per run_id from audit_log_iterations.

    Used by evals/scorers/pairwise.py to reconstruct iter-0 drafts so the
    pairwise judge can compare against the final approved draft. Filtering
    by run_id (not app_id+review_id) is critical: the same review may have
    multiple historical runs, and joining without run_id would pull stale
    rows from a previous run.

    Returns a dict {run_id: row_dict}. Missing run_ids simply absent from
    the result — caller treats absence as "no iter-0 to compare against."
    """
    if not run_ids:
        return {}
    placeholders = ",".join("?" * len(run_ids))
    cursor = conn.execute(
        f"""
        SELECT run_id, drafted_response, proposed_action, source_ids_cited,
               critique, reason_type
        FROM audit_log_iterations
        WHERE iteration = ? AND run_id IN ({placeholders})
        """,
        (iteration, *run_ids),
    )
    out: dict[str, dict] = {}
    for row in cursor.fetchall():
        rid = row[0]
        try:
            cited = json.loads(row[3]) if row[3] else []
        except (json.JSONDecodeError, TypeError):
            cited = []
        out[rid] = {
            "drafted_response": row[1] or "",
            "proposed_action": row[2] or "",
            "source_ids_cited": cited,
            "critique": row[4] or "",
            "reason_type": row[5] or "",
        }
    return out


def load_feedback_examples(
    conn: sqlite3.Connection,
    app_id: str,
    category: str,
    n: int = 3,
    pool_size: int = 10,
) -> tuple[list[dict], list[dict]]:
    """
    Load approved drafts from the audit log for few-shot examples.

    Fetches a pool of recent approved entries, then selects up to *n*
    with diversity across action types and confidence levels:
      - Slot 1: most recent (captures current approver style)
      - Slot 2: most recent with a different proposed_action than slot 1
      - Slot 3: highest evidence_confidence among remaining candidates

    Falls back to next-most-recent for any unfillable slot.

    Returns:
        A tuple of (examples, selection_log):
        - examples: list of dicts with review_text, drafted_response,
          evidence_summary, review_tone, proposed_action, evidence_confidence.
        - selection_log: list of dicts with pool_index, action, confidence,
          reason for each selected example (for node_log debugging).
    """
    cursor = conn.execute("""
        SELECT review_text, drafted_response, evidence_summary,
               review_tone, proposed_action, evidence_confidence
        FROM audit_log
        WHERE app_id = ?
          AND category = ?
          AND human_decision = 'approved'
          AND drafted_response IS NOT NULL
          AND drafted_response != ''
        ORDER BY created_at DESC
        LIMIT ?
    """, (app_id, category, pool_size))

    rows = cursor.fetchall()
    columns = [
        "review_text", "drafted_response", "evidence_summary",
        "review_tone", "proposed_action", "evidence_confidence",
    ]
    pool = [dict(zip(columns, row)) for row in rows]
    logger.debug(f"Loaded {len(pool)} feedback candidates for {app_id}/{category}.")

    if not pool:
        return [], []

    selected: list[dict] = []
    selection_log: list[dict] = []
    used_indices: set[int] = set()

    def _pick(index: int, reason: str) -> None:
        used_indices.add(index)
        selected.append(pool[index])
        ex = pool[index]
        selection_log.append({
            "pool_index": index,
            "action": ex.get("proposed_action", ""),
            "confidence": ex.get("evidence_confidence"),
            "reason": reason,
        })

    # Slot 1: most recent
    _pick(0, "recent")

    if len(selected) >= n:
        return selected, selection_log

    # Slot 2: most recent with a different action than slot 1
    slot1_action = pool[0].get("proposed_action", "")
    filled_slot2 = False
    for i, ex in enumerate(pool):
        if i in used_indices:
            continue
        if ex.get("proposed_action", "") != slot1_action:
            _pick(i, "different_action")
            filled_slot2 = True
            break
    if not filled_slot2:
        # Fall back to next most recent unused
        for i in range(len(pool)):
            if i not in used_indices:
                _pick(i, "recent_fallback")
                break

    if len(selected) >= n:
        return selected, selection_log

    # Slot 3: highest evidence_confidence among remaining
    best_idx = -1
    best_conf = -1.0
    for i, ex in enumerate(pool):
        if i in used_indices:
            continue
        conf = ex.get("evidence_confidence") or 0.0
        if conf > best_conf:
            best_conf = conf
            best_idx = i
    if best_idx >= 0:
        _pick(best_idx, "high_confidence")

    return selected, selection_log


def save_cluster_note(
    conn: sqlite3.Connection,
    app_id: str,
    category: str,
    note_type: str,
    note_text: str,
    tags: list[str] | None = None,
    created_by: str = "system",
    source_review_id: str | None = None,
) -> bool:
    """
    Save a note to the cluster_notes table.

    Parameters:
        note_type: One of "known_issue", "response_history",
                   "investigation", "human_feedback".
        tags: Optional list of tags for filtering.
        created_by: "system" or "human".
        source_review_id: Review ID that triggered this note.

    Returns:
        True if inserted successfully, False on error.
    """
    try:
        conn.execute("""
            INSERT INTO cluster_notes
            (app_id, category, note_type, tags, note_text, created_by, source_review_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            app_id,
            category,
            note_type,
            json.dumps(tags) if tags else None,
            note_text,
            created_by,
            source_review_id,
        ))
        conn.commit()
        logger.info(f"Saved {note_type} note for {app_id}/{category}.")
        return True
    except sqlite3.Error as e:
        logger.error(f"Failed to save cluster note: {e}")
        return False


def load_cluster_notes(
    conn: sqlite3.Connection,
    app_id: str,
    category: str,
    status: str | None = "active",
    include_stale: bool = False,
) -> list[dict]:
    """
    Load notes for a given app and category cluster.

    Parameters:
        status: Filter by status ("active", "resolved"), or None for all.
        include_stale: If False, exclude notes older than CLUSTER_NOTE_STALENESS_DAYS.

    Returns:
        A list of dicts with all cluster_notes columns,
        with tags parsed from JSON back into a list.
    """
    query = """
        SELECT id, app_id, category, note_type, tags, note_text,
               created_by, created_at, updated_at, status, source_review_id
        FROM cluster_notes
        WHERE app_id = ?
          AND category = ?
    """
    params: list = [app_id, category]

    if status is not None:
        query += " AND status = ?"
        params.append(status)

    if not include_stale:
        cutoff = datetime.utcnow() - timedelta(days=CLUSTER_NOTE_STALENESS_DAYS)
        query += " AND updated_at >= ?"
        params.append(cutoff.strftime("%Y-%m-%d %H:%M:%S"))

    query += " ORDER BY updated_at DESC"

    cursor = conn.execute(query, params)

    rows = cursor.fetchall()
    columns = ["id", "app_id", "category", "note_type", "tags", "note_text",
               "created_by", "created_at", "updated_at", "status", "source_review_id"]

    results = []
    for row in rows:
        entry = dict(zip(columns, row))
        entry["tags"] = json.loads(entry["tags"]) if entry["tags"] else []
        results.append(entry)

    logger.debug(f"Loaded {len(results)} cluster notes for {app_id}/{category}.")
    return results


def update_cluster_note_status(
    conn: sqlite3.Connection,
    note_id: int,
    status: str,
) -> bool:
    """
    Update the status of a cluster note.

    Parameters:
        note_id: The note's primary key.
        status: New status ("active" or "resolved").

    Returns:
        True if updated successfully, False on error.
    """
    try:
        cursor = conn.execute("""
            UPDATE cluster_notes
            SET status = ?, updated_at = datetime('now')
            WHERE id = ?
        """, (status, note_id))
        conn.commit()
        if cursor.rowcount == 0:
            logger.warning(f"No cluster note with id={note_id} — nothing updated.")
            return False
        logger.info(f"Updated cluster note {note_id} status to '{status}'.")
        return True
    except sqlite3.Error as e:
        logger.error(f"Failed to update cluster note {note_id}: {e}")
        return False


def find_recent_similar_note(
    conn: sqlite3.Connection,
    app_id: str,
    category: str,
    note_type: str,
    source_review_id: str | None = None,
    window_hours: int | None = None,
) -> dict | None:
    """
    Find a recent active note that matches the given criteria.

    Two-tier dedup:
    1. If source_review_id is provided, check for an exact match first.
    2. Fall back to time-window dedup within window_hours.

    Returns:
        The most recent matching note dict, or None.
    """
    # Tier 1: exact match by source_review_id
    if source_review_id:
        cursor = conn.execute("""
            SELECT id, note_text, updated_at
            FROM cluster_notes
            WHERE app_id = ? AND category = ? AND note_type = ?
              AND source_review_id = ? AND status = 'active'
            ORDER BY updated_at DESC
            LIMIT 1
        """, (app_id, category, note_type, source_review_id))
        row = cursor.fetchone()
        if row:
            return {"id": row[0], "note_text": row[1], "updated_at": row[2]}

    # Tier 2: time-window dedup
    hours = window_hours or CLUSTER_NOTE_DEDUP_WINDOW_HOURS
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    cursor = conn.execute("""
        SELECT id, note_text, updated_at
        FROM cluster_notes
        WHERE app_id = ? AND category = ? AND note_type = ?
          AND status = 'active'
          AND created_at >= ?
        ORDER BY updated_at DESC
        LIMIT 1
    """, (app_id, category, note_type, cutoff.strftime("%Y-%m-%d %H:%M:%S")))
    row = cursor.fetchone()
    if row:
        return {"id": row[0], "note_text": row[1], "updated_at": row[2]}

    return None


def update_cluster_note_text(
    conn: sqlite3.Connection,
    note_id: int,
    note_text: str,
) -> bool:
    """
    Update the text of an existing cluster note (refreshes updated_at).

    Used by the dedup path to refresh an existing note instead of duplicating.

    Returns:
        True if updated successfully, False on error.
    """
    try:
        conn.execute("""
            UPDATE cluster_notes
            SET note_text = ?, updated_at = datetime('now')
            WHERE id = ?
        """, (note_text, note_id))
        conn.commit()
        logger.info(f"Updated cluster note {note_id} text.")
        return True
    except sqlite3.Error as e:
        logger.error(f"Failed to update cluster note {note_id} text: {e}")
        return False