"""
Cluster Detection & Priority Ranking for classified reviews.
Groups reviews by complaint category, computes metrics, and ranks by priority.
"""

import logging
import pandas as pd
import anthropic
from datetime import timedelta, timezone
from pydantic import BaseModel, Field
from collections import Counter
from skills import load_skill
from config import (
    CLUSTER_TIME_WINDOW_DAYS,
    CLUSTER_MIN_REVIEWS,
    PRIORITY_WEIGHTS,
    CLUSTER_SUMMARY_MODEL,
    CLUSTER_SUMMARY_TEMPERATURE,
    CLUSTER_SUMMARY_MAX_TOKENS,
)

logger = logging.getLogger(__name__)

class ClusterSummary(BaseModel):
    """
    A cluster of reviews sharing the same complaint category with computed metrics and an optional LLM-generated summary.
    """
    category: str
    total_reviews: int
    recent_reviews: int
    prior_reviews: int
    velocity_ratio: float = Field(
        description="Ratio of recent to prior review volume. >1.0 means growing."
    )
    negative_pct: float = Field(
        description="Percentage of reviews in this cluster that are negative (0-100)."
    )
    avg_playtime_hours: float
    top_keywords: list[str] = Field(default_factory=list)
    sample_reviews: list[str] = Field(default_factory=list)
    priority_score: float = 0.0
    summary: str = ""  # Filled later by LLM


def build_clusters(df: pd.DataFrame) -> list[ClusterSummary]:
    """
    Group classified reviews by primary_category and compute per-cluster metrics.

    Parameters:
        df: DataFrame from load_classified_reviews() — must have columns:
            primary_category, voted_up, timestamp, playtime_hours, review_text

    Returns:
        A list of ClusterSummary objects, one per category that meets the
        minimum review threshold.
    """
    if len(df) == 0:
        logger.warning("No reviews to cluster.")
        return []

    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")

    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    recent_cutoff = now - timedelta(days=CLUSTER_TIME_WINDOW_DAYS)
    prior_cutoff = recent_cutoff - timedelta(days=CLUSTER_TIME_WINDOW_DAYS)

    clusters = []

    for category, group in df.groupby("primary_category"):
        total = len(group)

        if total < CLUSTER_MIN_REVIEWS:
            logger.debug(
                f"Skipping '{category}': only {total} reviews (need {CLUSTER_MIN_REVIEWS})."
            )
            continue

        recent = group[group["timestamp"] >= recent_cutoff]
        prior = group[(group["timestamp"] >= prior_cutoff) & (group["timestamp"] < recent_cutoff)]

        recent_count = len(recent)
        prior_count = len(prior)

        if prior_count == 0 and recent_count > 0:
            velocity_ratio = float(recent_count)
        elif prior_count == 0 and recent_count == 0:
            velocity_ratio = 0.0
        else:
            velocity_ratio = round(recent_count / prior_count, 1)

        negative_count = (group["voted_up"] == 0).sum()
        negative_pct = round(negative_count / total * 100, 1)

        avg_playtime = round(group["playtime_hours"].mean(), 1)

        top_keywords = _extract_top_keywords(group["review_text"], n=10)

        samples = (
            group.sort_values("weighted_vote_score", ascending=False)
            .head(5)["review_text"]
            .tolist()
        )
        samples = [s[:300] for s in samples]

        cluster = ClusterSummary(
            category=str(category),
            total_reviews=total,
            recent_reviews=recent_count,
            prior_reviews=prior_count,
            velocity_ratio=velocity_ratio,
            negative_pct=negative_pct,
            avg_playtime_hours=avg_playtime,
            top_keywords=top_keywords,
            sample_reviews=samples,
        )

        clusters.append(cluster)
        logger.info(
            f"Cluster '{category}': {total} reviews (recent={recent_count}, prior={prior_count}), "
        )
        logger.info(
            f"velocity={velocity_ratio}, negative_pct={negative_pct}%, "
        )
        logger.info(
            f"avg_playtime={avg_playtime}h, keywords={top_keywords}"
        )

    logger.info(f"Built {len(clusters)} clusters from {len(df)} reviews.")
    return clusters


def _extract_top_keywords(texts: pd.Series, n: int = 5) -> list[str]:
    """
    Quick keyword extraction for a cluster's reviews.
    A simpler version of stats.py's version.
    """
    stop_words = {
        "the", "a", "an", "is", "it", "in", "to", "and", "of", "for",
        "on", "with", "this", "that", "was", "are", "you", "but", "not",
        "have", "has", "had", "be", "been", "can", "will", "would",
        "just", "so", "its", "my", "me", "i", "do", "if", "at", "by",
        "or", "no", "from", "they", "we", "there", "all", "your",
        "what", "when", "up", "out", "about", "how", "one", "their",
        "very", "really", "more", "some", "than", "get", "got", "like",
        "don", "as", "im", "ive", "also", "even", "much", "too",
        "don't", "dont", "can't", "cant", "won't", "wont", "isn't", "isnt",
        "didn't", "didnt", "doesn't", "doesnt", "it's", "i'm", "i've",
        "you're", "youre", "that's", "thats", "there's", "theres",
        "am", "did", "does", "should", "could", "may", "might", "must",
        "he", "she", "him", "her", "them", "us", "who", "which", "because",
        "game", "games", "played", "playing", "being", "time", "hours", "play",
        "best", "ever", "every", "great", "love", "good", "amazing", "first",
        "after", "fun", "then", "only", "go", "again", "most", "see", "still",
        "where", "feel", "other", "made", "into", "feels", "way", "many",
        "better", "any", "want", "lot", "make", "over", "well", "run", "runs",
        "experience", "while", "back", "each", "new", "different", "bit", 
        "say", "never", "makes", "everything", "now", "recommend", "full",
        "it's", "know", "nice", "bad", "here", "think", "enjoy", "why", "win",
        "though", "masterpiece", "try", "addictive", "addicting", "second",
        "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth",
        "tenth", "since", "years", "year", "day", "days", "month", "months",
        "own", "need", "something", "worth", "it’s", "his", "hers", "theirs",
        "ours", "hate", "minute", "minutes", "seconds", "beat", "pew", "end",
        "players", "player", "people", "die", "dead", "find", "start", "lose",
        "things", "give", "gave", "main", "far", "near", "thing", "going", "used",
        "uses", "use", "few", "around", "work", "works", "worked", "overall",
        "once", "doing", "add", "enjoyed", "looking", "plenty", "take", "takes",
        "taken", "took"
    }

    counts: Counter = Counter()

    for text in texts.dropna():
        for word in text.lower().split():
            cleaned = word.strip(".,!?;:\"'()[]{}#@*&^%$~`<>/\\|+=-_")
            if len(cleaned) > 1 and cleaned not in stop_words:
                counts[cleaned] += 1

    return [word for word, _ in counts.most_common(n)]

def rank_clusters(clusters: list[ClusterSummary]) -> list[ClusterSummary]:
    """
    Compute a priority score (0-100) for each cluster and sort by highest priority.

    The score combines four factors:
        - volume:       How many total reviews are in this cluster
        - velocity:     Is the cluster growing recently vs. the prior window
        - sentiment:    What percentage of reviews are negative
        - rating_impact: How many upvotes the cluster's reviews have (community signal)

    Each factor is normalized to 0-1, then combined using PRIORITY_WEIGHTS from config.
    """
    if not clusters:
        return []

    volumes = [c.total_reviews for c in clusters]
    velocities = [c.velocity_ratio for c in clusters]

    max_volume = max(volumes) if volumes else 1
    max_velocity = max(velocities) if velocities else 1

    for cluster in clusters:
        norm_volume = cluster.total_reviews / max_volume if max_volume > 0 else 0
        norm_velocity = cluster.velocity_ratio / max_velocity if max_velocity > 0 else 0
        norm_sentiment = cluster.negative_pct / 100
        norm_rating = norm_sentiment * norm_volume

        score = (
            PRIORITY_WEIGHTS["volume"] * norm_volume
            + PRIORITY_WEIGHTS["velocity"] * norm_velocity
            + PRIORITY_WEIGHTS["sentiment"] * norm_sentiment
            + PRIORITY_WEIGHTS["rating_impact"] * norm_rating
        )

        cluster.priority_score = round(score * 100, 1)

    clusters.sort(key=lambda c: c.priority_score, reverse=True)

    for i, cluster in enumerate(clusters, start=1):
        logger.info(
            f"Priority #{i}: '{cluster.category}': "
            f"score={cluster.priority_score}, volume={cluster.total_reviews}, "
            f"velocity={cluster.velocity_ratio}, negative={cluster.negative_pct}%"
        )

    return clusters

client = anthropic.Anthropic()
CLUSTER_SYSTEM_PROMPT = load_skill("analyze_cluster")


def summarize_cluster(cluster: ClusterSummary) -> str:
    """
    Send a cluster's metrics and sample reviews to the LLM and get back a concise summary for the dev team.

    Returns:
        The summary text extracted from the LLM response.
    """
    user_message = f"""<cluster_data>
category: {cluster.category}
total_reviews: {cluster.total_reviews}
recent_reviews: {cluster.recent_reviews}
prior_reviews: {cluster.prior_reviews}
velocity_ratio: {cluster.velocity_ratio}
negative_pct: {cluster.negative_pct}
avg_playtime_hours: {cluster.avg_playtime_hours}
top_keywords: {cluster.top_keywords}
sample_reviews:
{_format_samples(cluster.sample_reviews)}
priority_score: {cluster.priority_score}
</cluster_data>"""

    try:
        response = client.messages.create(
            model=CLUSTER_SUMMARY_MODEL,
            max_tokens=CLUSTER_SUMMARY_MAX_TOKENS,
            temperature=CLUSTER_SUMMARY_TEMPERATURE,
            system=CLUSTER_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user_message}
            ],
        )
    except anthropic.APIError as e:
        logger.error(f"Cluster summary API call failed: {e}")
        return f"[Summary unavailable — API error: {e}]"

    if response.stop_reason == "max_tokens":
        logger.warning(
            "Cluster summary was cut off (stop_reason='max_tokens'). "
            "Consider increasing CLUSTER_SUMMARY_MAX_TOKENS in config."
        )

    if response.stop_reason == "refusal":
        logger.warning(
            f"Model refused to summarize cluster '{cluster.category}' to safety concerns."
        )
        return "[Summary unavailable — model refused due to safety concerns]"

    logger.debug(
        f"Cluster summary API call for '{cluster.category}': {response.usage.input_tokens} input + {response.usage.output_tokens} output tokens"
    )

    content_block = response.content[0]
    if not hasattr(content_block, "text"):
        raise ValueError(f"Expected a text response, got {type(content_block).__name__}")
    raw_text = content_block.text.strip()  # type: ignore[union-attr]

    summary = _extract_summary(raw_text)

    return summary


def _format_samples(samples: list[str]) -> str:
    """
    Format sample reviews as a bulleted list for the prompt.
    """
    return "\n".join(f'- "{s}"' for s in samples)


def _extract_summary(text: str) -> str:
    """
    Pull out the content between <summary> tags.
    Falls back to the full text if tags are missing.
    """
    start_tag = "<summary>"
    end_tag = "</summary>"

    start = text.find(start_tag)
    end = text.find(end_tag)

    if start == -1 or end == -1:
        logger.warning(
            "Could not find <summary> tags in LLM response. Using full response as summary."
        )
        return text.strip()

    return text[start + len(start_tag):end].strip()