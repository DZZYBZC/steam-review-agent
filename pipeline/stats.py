"""
Compute basic statistics about the review dataset that don't need to use LLM.
"""

import logging
import pandas as pd
from pipeline.keywords import extract_keywords

logger = logging.getLogger(__name__)

def compute_basic_stats(df: pd.DataFrame) -> dict:
    """
    Compute summary statistics about the review dataset.

    Returns a dictionary with:
        - total_reviews: number of reviews
        - positive_pct / negative_pct: sentiment split
        - avg_playtime_hours: average reviewer playtime
        - date_range: earliest and latest review dates
        - duplicate_count: how many near-duplicates were flagged
    """
    total = len(df)

    if total == 0:
        logger.warning("No reviews to compute stats for.")
        return {"total_reviews": 0}

    positive = df["voted_up"].sum()
    negative = total - positive

    stats = {
        "total_reviews": total,
        "positive_count": int(positive),
        "negative_count": int(negative),
        "positive_pct": round(positive / total * 100, 1),
        "negative_pct": round(negative / total * 100, 1),
        "avg_playtime_hours": round(df["playtime_hours"].mean(), 1),
        "median_playtime_hours": round(df["playtime_hours"].median(), 1),
        "avg_votes_up": round(df["votes_up"].mean(), 1),
        "duplicate_count" : int(df["is_near_duplicate"].sum())
    }

    timestamps = pd.to_datetime(df["timestamp"], errors="coerce").dropna()
    if len(timestamps) > 0:
        stats["earliest_review"] = str(timestamps.min())
        stats["latest_review"] = str(timestamps.max())

    return stats


def compute_keyword_frequency(df: pd.DataFrame, top_n: int = 20) -> list[tuple]:
    """
    Find the most common words across all reviews except some stop words.

    Parameters:
        df: DataFrame with a "review_text" column.
        top_n: How many top words to return.

    Returns:
        A list of (word, count) tuples, sorted by count descending.
    """
    return extract_keywords(df["review_text"], n=top_n)


def print_stats_report(stats: dict, keywords: list[tuple]) -> None:
    """
    Print a human-readable summary of the stats for our own debugging and exploration.
    """
    print("\n" + "=" * 50)
    print("REVIEW DATASET SUMMARY")
    print("=" * 50)

    print(f"\nTotal reviews: {stats.get('total_reviews', 0)}")
    print(f"  Positive: {stats.get('positive_count', 0)} ({stats.get('positive_pct', 0)}%)")
    print(f"  Negative: {stats.get('negative_count', 0)} ({stats.get('negative_pct', 0)}%)")
    print(f"  Near-duplicates flagged: {stats['duplicate_count']}")

    print(f"\nPlaytime (hours):")
    print(f"  Average: {stats.get('avg_playtime_hours', 'N/A')}")
    print(f"  Median:  {stats.get('median_playtime_hours', 'N/A')}")

    if "earliest_review" in stats:
        print(f"\nDate range: {stats['earliest_review']} → {stats['latest_review']}")

    print(f"\nTop keywords:")
    for word, count in keywords:
        print(f"  {word:20s} {count:d}")

    print("=" * 50)