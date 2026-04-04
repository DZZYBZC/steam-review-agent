"""
Manual inspection script for patch note fetching and chunking.

Fetches real Steam news for a game specified in APP_ID, shows feed distribution,
news_type classifications, and sample chunks for quality review.
"""

from collections import Counter
from pipeline.ingest_patch_notes import fetch_news
from pipeline.chunk import chunk_all_patch_notes

APP_ID = "892970"


def main():
    print(f"Fetching news for app {APP_ID}...")
    items = fetch_news(APP_ID)
    print(f"\nTotal items returned: {len(items)}\n")

    # --- Feed distribution ---
    feedname_counts = Counter(item.get("feedname", "<missing>") for item in items)
    feedlabel_counts = Counter(item.get("feedlabel", "<missing>") for item in items)

    print("=== Unique feednames ===")
    for name, count in feedname_counts.most_common():
        print(f"  {name}: {count}")

    print("\n=== Unique feedlabels ===")
    for label, count in feedlabel_counts.most_common():
        print(f"  {label}: {count}")

    # --- News type classification ---
    print("\n=== News type per item ===")
    for i, item in enumerate(items):
        title = item.get("title", "<no title>")[:80]
        news_type = item.get("news_type", "<unclassified>")
        feed = item.get("feedname", "?")
        print(f"  [{i}] ({news_type}) [{feed}] {title}")

    # --- Chunking ---
    print("\n=== Chunking ===")
    chunks = chunk_all_patch_notes(items)
    print(f"Total chunks: {len(chunks)}\n")

    print("=== First 5 chunks (full text) ===")
    for i, chunk in enumerate(chunks[:5]):
        print(f"--- Chunk {i} ---")
        print(f"  chunk_id:      {chunk.chunk_id}")
        print(f"  section:       {chunk.section}")
        print(f"  news_type:     {chunk.news_type}")
        print(f"  patch_version: {chunk.patch_version}")
        print(f"  patch_date:    {chunk.patch_date}")
        print(f"  text:\n    {chunk.text}")
        print()


if __name__ == "__main__":
    main()
