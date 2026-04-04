"""
Manual inspection script for vector search and BM25 retrieval quality.

Fetches real patch notes for Monster Hunter Wilds, chunks and embeds them,
then runs test queries through both retrieval methods side by side.
"""

from pipeline.ingest_patch_notes import fetch_news
from pipeline.chunk import chunk_all_patch_notes
from pipeline.embed import embed_chunks, query_similar, build_bm25_index, query_bm25, reciprocal_rank_fusion
from config import VECTOR_TOP_K, BM25_TOP_K

APP_ID = "2246340"

TEST_QUERIES = [
    "crashing on startup after latest update",
    "frame rate drops during large monster fights",
    "multiplayer disconnects when joining friends",
    "textures not loading properly looks blurry",
    "game is too expensive not worth the price",
]


def print_vector_results(results):
    if not results:
        print("  (no results above similarity threshold)")
        return
    for j, r in enumerate(results):
        meta = r["metadata"]
        text_preview = r["text"][:120]
        print(f"  [{j}] dist={r['distance']:.4f}  rank={r['rank']}  retriever={r['retriever']}  version={meta['patch_version']}")
        print(f"      section={meta['section']}  type={meta['news_type']}")
        print(f"      {text_preview}...")


def print_bm25_results(results):
    if not results:
        print("  (no results with score > 0)")
        return
    for j, r in enumerate(results):
        meta = r["metadata"]
        text_preview = r["text"][:120]
        print(f"  [{j}] score={r['score']:.4f}  rank={r['rank']}  retriever={r['retriever']}  version={meta['patch_version']}")
        print(f"      section={meta['section']}  type={meta['news_type']}")
        print(f"      {text_preview}...")


def main():
    print(f"Fetching patch notes for app {APP_ID}...")
    items = fetch_news(APP_ID)
    print(f"Fetched {len(items)} items.")

    chunks = chunk_all_patch_notes(items)
    print(f"Created {len(chunks)} chunks.")

    print(f"\nEmbedding chunks into ChromaDB...")
    collection = embed_chunks(chunks, APP_ID)
    print(f"Embedding complete.")

    print(f"Building BM25 index...")
    bm25_index, bm25_chunks = build_bm25_index(chunks)
    print(f"BM25 index built.\n")

    for query in TEST_QUERIES:
        print(f"{'=' * 80}")
        print(f"QUERY: \"{query}\"")
        print(f"{'=' * 80}")

        vec_results = query_similar(collection, query, n_results=VECTOR_TOP_K)
        bm25_results = query_bm25(bm25_index, bm25_chunks, query, n_results=BM25_TOP_K)

        print(f"\n  --- VECTOR RESULTS (top {VECTOR_TOP_K}, cosine) ---")
        print_vector_results(vec_results)

        print(f"\n  --- BM25 RESULTS (top {BM25_TOP_K}, keyword) ---")
        print_bm25_results(bm25_results)

        vec_ids = {r["chunk_id"] for r in vec_results}
        bm25_ids = {r["chunk_id"] for r in bm25_results}
        overlap = vec_ids & bm25_ids
        vec_only = vec_ids - bm25_ids
        bm25_only = bm25_ids - vec_ids

        print(f"\n  --- COMPARISON ---")
        print(f"  overlap={len(overlap)}  vector_only={len(vec_only)}  bm25_only={len(bm25_only)}")

        rrf_results = reciprocal_rank_fusion(vec_results, bm25_results)

        print(f"\n  --- RRF FUSED RESULTS ({len(rrf_results)} unique) ---")
        if not rrf_results:
            print("  (no results)")
        else:
            for j, r in enumerate(rrf_results):
                meta = r["metadata"]
                text_preview = r["text"][:120]
                print(f"  [{j}] rrf={r['rrf_score']:.6f}  retrievers={r['retrievers']}  version={meta['patch_version']}")
                print(f"      section={meta['section']}  type={meta['news_type']}")
                print(f"      {text_preview}...")
        print()


if __name__ == "__main__":
    main()
