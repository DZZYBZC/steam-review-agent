"""
Cleans raw Steam review data and extracts useful features.
"""

import logging
import pandas as pd
from datasketch import MinHash, MinHashLSH
from config import REVIEW_MIN_CHARS, REVIEW_MIN_WORDS, NEAR_DUPLICATE_THRESHOLD

logger = logging.getLogger(__name__)

def extract_review_fields(raw_reviews: list[dict], app_id: str) -> pd.DataFrame:
    """
    Pull out the fields we need from raw Steam API data into a clean DataFrame.

    Parameters:
        raw_reviews: List of review dicts straight from the Steam API.
        app_id: Steam game app id

    Returns:
        A pandas DataFrame with columns:
        - review_id, steam_id, review_text, voted_up, timestamp,
          playtime_hours, votes_up, votes_funny, weighted_vote_score
    """
    cleaned = []

    for r in raw_reviews:
        if r.get("steam_purchase") == True and r.get("received_for_free") == False:
            author = r.get("author", {})

            cleaned.append({
                "review_id": r.get("recommendationid"),
                "app_id": app_id,
                "steam_id": author.get("steamid"),
                "review_text": r.get("review", ""),
                "voted_up": r.get("voted_up"),  # True = positive, False = negative
                "timestamp": r.get("timestamp_created"),
                "playtime_hours": round(author.get("playtime_forever", 0) / 60, 1),
                "votes_up": r.get("votes_up", 0),
                "votes_funny": r.get("votes_funny", 0),
                "weighted_vote_score": r.get("weighted_vote_score", 0)
            })

    df = pd.DataFrame(cleaned)

    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce")

    logger.info(f"Extracted {len(df)} reviews into DataFrame.")
    return df


def clean_reviews(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remove junk reviews that would hurt analysis quality.
    """
    original_count = len(df)

    df = df.dropna(subset=["review_text"]) # Reviews NA
    df = df[df["review_text"].str.len() >= REVIEW_MIN_CHARS] # Review too short
    df = df[df["review_text"].str.split().str.len() >= REVIEW_MIN_WORDS] # Review too short
    df = df.drop_duplicates(subset=["review_id"]) # Duplicated reviews

    removed = original_count - len(df)
    logger.info(f"Cleaned reviews: removed {removed} junk rows, {len(df)} remaining.")

    return df.reset_index(drop=True)


def detect_near_duplicates(
    df: pd.DataFrame,
    similarity_threshold: float = NEAR_DUPLICATE_THRESHOLD,
) -> pd.DataFrame:
    """
    Flag reviews that are near-duplicates using MinHash + LSH.

    Parameters:
        df: The cleaned DataFrame.
        similarity_threshold: Jaccard similarity threshold (0.0 to 1.0).
                              0.85 means reviews sharing 85%+ of their shingles are flagged as duplicates.

    Returns:
        The same DataFrame with a new column "is_near_duplicate" (True/False).
    """

    df = df.copy()
    df["is_near_duplicate"] = False

    texts = df["review_text"].tolist()

    if len(texts) < 2:
        return df

    shingle_size = 3
    minhashes = []

    for text in texts:
        words = text.lower().split()
        mh = MinHash(num_perm=128)
        for i in range(len(words) - shingle_size + 1):
            shingle = " ".join(words[i:i + shingle_size])
            mh.update(shingle.encode("utf-8"))

        minhashes.append(mh)

    lsh = MinHashLSH(threshold=similarity_threshold, num_perm=128)

    for idx, mh in enumerate(minhashes):
        try:
            lsh.insert(str(idx), mh)
        except ValueError:
            logger.debug(f"Review {idx} is an exact duplicate signature.")

    duplicate_indices = set()

    for idx, mh in enumerate(minhashes):
        if idx in duplicate_indices:
            continue

        candidates = lsh.query(mh)

        for candidate_key in candidates:
            candidate_idx = int(str(candidate_key))

            if candidate_idx <= idx or candidate_idx in duplicate_indices:
                continue

            similarity = minhashes[idx].jaccard(minhashes[candidate_idx]) # Jaccard similarity between two singnatures

            if similarity >= similarity_threshold:
                duplicate_indices.add(candidate_idx)
                logger.debug(
                    f"Near-duplicate found (similarity={similarity:.2f}): "
                    f"review {idx} and review {candidate_idx}"
                )

    df.loc[list(duplicate_indices), "is_near_duplicate"] = True

    dupe_count = len(duplicate_indices)
    if dupe_count > 0:
        logger.info(f"Found {dupe_count} near-duplicate reviews (threshold={similarity_threshold}).")
    else:
        logger.info("No near-duplicate reviews found.")

    return df


def clean_pipeline(raw_reviews: list[dict], app_id: str) -> pd.DataFrame:
    """
    Wrapper to run the full cleaning pipeline: extract → clean → deduplicate.
    """
    df = extract_review_fields(raw_reviews, app_id)
    df = clean_reviews(df)
    df = detect_near_duplicates(df)
    return df