"""
SQLite database operations for storing and retrieving reviews.
"""

import sqlite3
import logging
import pandas as pd
import json
from config import DB_PATH, CONFIDENCE_THRESHOLD

logger = logging.getLogger(__name__)


def get_connection() -> sqlite3.Connection:
    """
    Open a connection to the SQLite database.

    Returns:
        A database connection object.
    """
    conn = sqlite3.connect(DB_PATH)
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
            confidence REAL NOT NULL,
            reasoning TEXT,
            needs_review BOOLEAN DEFAULT 0,
            model_used TEXT,
            classified_at TEXT,
            FOREIGN KEY (review_id) REFERENCES reviews(review_id)
        )
    """)

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
             confidence, reasoning, needs_review, model_used, classified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            review_id,
            app_id,
            result.primary_category,
            json.dumps(result.secondary_categories),
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
    Parses the secondary_categories JSON string back into a list.
    """
    query = "SELECT * FROM classifications WHERE 1=1"
    params = []
    if app_id:
        query += " AND app_id = ?"
        params.append(app_id)

    df = pd.read_sql_query(query, conn, params=params)

    if len(df) > 0 and "secondary_categories" in df.columns:
        df["secondary_categories"] = df["secondary_categories"].apply(
            lambda x: json.loads(x) if x else []
        )

    logger.info(f"Loaded {len(df)} classifications.")
    return df