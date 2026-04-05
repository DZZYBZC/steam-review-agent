"""
Manual inspection script for the full retrieval pipeline.

Fetches real patch notes for Monster Hunter Wilds, chunks and embeds them,
then runs test queries through vector search, BM25, RRF fusion, and
cross-encoder reranking.
"""

from pipeline.ingest_patch_notes import fetch_news
from pipeline.chunk import chunk_all_patch_notes
from pipeline.retrieve import embed_chunks, query_similar, build_bm25_index, query_bm25, reciprocal_rank_fusion, rerank
from config import VECTOR_TOP_K, BM25_TOP_K, RERANKER_TOP_N

APP_ID = "2246340"

TEST_QUERIES = [
    "crashing on startup after latest update",
    "frame rate drops during large monster fights",
    "multiplayer disconnects when joining friends",
    "textures not loading properly looks blurry",
    "game is too expensive not worth the price",
]


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

        # Summary of retriever overlap
        vec_ids = {r["chunk_id"] for r in vec_results}
        bm25_ids = {r["chunk_id"] for r in bm25_results}
        overlap = vec_ids & bm25_ids
        print(f"\n  Retrievers: vector={len(vec_results)}  bm25={len(bm25_results)}  overlap={len(overlap)}")

        rrf_results = reciprocal_rank_fusion(vec_results, bm25_results)

        print(f"\n  --- RRF FUSED RESULTS ({len(rrf_results)} candidates) ---")
        if not rrf_results:
            print("  (no results)")
        else:
            for j, r in enumerate(rrf_results):
                meta = r["metadata"]
                text_preview = r["text"][:120]
                print(f"  [{j}] rrf={r['rrf_score']:.6f}  retrievers={r['retrievers']}  version={meta['patch_version']}")
                print(f"      section={meta['section']}  type={meta['news_type']}")
                print(f"      {text_preview}...")

        reranked = rerank(query, rrf_results)

        print(f"\n  --- RERANKED RESULTS (top {RERANKER_TOP_N}) ---")
        if not reranked:
            print("  (no results)")
        else:
            for j, r in enumerate(reranked):
                meta = r["metadata"]
                text_preview = r["text"][:120]
                print(f"  [{j}] relevance={r['relevance_score']:.4f}  rrf={r['rrf_score']:.6f}  retrievers={r['retrievers']}  version={meta['patch_version']}")
                print(f"      section={meta['section']}  type={meta['news_type']}")
                print(f"      {text_preview}...")
        print()


if __name__ == "__main__":
    main()
