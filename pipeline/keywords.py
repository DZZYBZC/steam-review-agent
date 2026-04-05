"""
Shared keyword extraction logic used by both cluster.py and stats.py.
"""

from collections import Counter
import pandas as pd

STOP_WORDS = {
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
    "know", "nice", "bad", "here", "think", "enjoy", "why", "win",
    "though", "masterpiece", "try", "addictive", "addicting", "second",
    "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth",
    "tenth", "since", "years", "year", "day", "days", "month", "months",
    "own", "need", "something", "worth", "his", "hers", "theirs",
    "ours", "hate", "minute", "minutes", "seconds", "beat", "pew", "end",
    "players", "player", "people", "die", "dead", "find", "start", "lose",
    "things", "give", "gave", "main", "far", "near", "thing", "going", "used",
    "uses", "use", "few", "around", "work", "works", "worked", "overall",
    "once", "doing", "add", "enjoyed", "looking", "plenty", "take", "takes",
    "taken", "took"
}


def extract_keywords(texts: pd.Series, n: int = 20) -> list[tuple[str, int]]:
    """
    Extract the most common words from a series of texts, excluding stop words.

    Parameters:
        texts: A pandas Series of review text strings.
        n: How many top keywords to return.

    Returns:
        A list of (word, count) tuples, sorted by count descending.
    """
    counts: Counter = Counter()

    for text in texts.dropna():
        for word in text.lower().split():
            cleaned = word.strip(".,!?;:\"'()[]{}#@*&^%$~`<>/\\|+=-_")
            if len(cleaned) > 1 and cleaned not in STOP_WORDS:
                counts[cleaned] += 1

    return counts.most_common(n)
