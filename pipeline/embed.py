"""
Embeds patch note chunks into ChromaDB for similarity search.

Uses sentence-transformers for embeddings and ChromaDB with persistent
storage. The embedding model is loaded lazily and cached at module level.
"""

import re
import logging
import chromadb
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer
from config import (
    CHROMA_PERSIST_DIR,
    EMBEDDING_MODEL,
    SIMILARITY_THRESHOLD,
    RRF_K,
)
from pipeline.chunk import PatchChunk

logger = logging.getLogger(__name__)

_model: SentenceTransformer | None = None

def _get_model() -> SentenceTransformer:
    """Load the embedding model lazily and cache it."""
    global _model
    if _model is None:
        logger.info(f"Loading embedding model: {EMBEDDING_MODEL}")
        _model = SentenceTransformer(EMBEDDING_MODEL)
    return _model


def _get_client():
    """Create a persistent ChromaDB client."""
    return chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)


def _get_or_create_collection(
    client, app_id: str
) -> chromadb.Collection:
    """Get or create a ChromaDB collection for an app, using cosine distance."""
    collection_name = f"patches_{app_id}"
    return client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )


def embed_chunks(
    chunks: list[PatchChunk], app_id: str
) -> chromadb.Collection:
    """
    Embed a list of PatchChunk objects into ChromaDB.

    Uses upsert for idempotency — re-running with the same chunk_ids
    will update existing entries rather than creating duplicates.
    Batches upserts in groups of 100.

    Returns:
        A ChromaDB collection for downstream queries.
    """
    model = _get_model()
    client = _get_client()
    collection = _get_or_create_collection(client, app_id)

    batch_size = 100
    total = len(chunks)

    for start in range(0, total, batch_size):
        batch = chunks[start : start + batch_size]

        ids = [c.chunk_id for c in batch]
        documents = [c.text for c in batch]
        embeddings = model.encode(documents).tolist()
        metadatas: list[dict[str, str | int | float | bool]] = [
            {
                "patch_version": c.patch_version,
                "patch_date": c.patch_date,
                "section": c.section,
                "news_type": c.news_type,
                "source_gid": c.source_gid,
                "source_url": c.source_url,
                "app_id": c.app_id,
            }
            for c in batch
        ]

        collection.upsert(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas, # type: ignore[arg-type]
        )

    logger.info(f"Embedded {total} chunks into collection patches_{app_id}.")
    return collection


def query_similar(
    collection: chromadb.Collection,
    query_text: str,
    n_results: int = 8,
) -> list[dict]:
    """
    Query ChromaDB for the most similar chunks to query_text.

    Returns:
        A list of dicts with: chunk_id, text, distance, metadata.
    """
    model = _get_model()
    query_embedding = model.encode([query_text]).tolist()

    results = collection.query(
        query_embeddings=query_embedding,
        n_results=n_results,
        include=["documents", "metadatas", "distances"],
    )

    max_distance = 1.0 - SIMILARITY_THRESHOLD

    ids = results["ids"]
    documents = results["documents"]
    metadatas = results["metadatas"]
    distances = results["distances"]

    if ids is None or documents is None or metadatas is None or distances is None:
        return []

    output = []
    for i in range(len(ids[0])):
        distance = distances[0][i]
        if distance > max_distance:
            continue

        output.append({
            "chunk_id": ids[0][i],
            "text": documents[0][i],
            "distance": distance,
            "metadata": metadatas[0][i],
            "retriever": "vector",
            "rank": len(output),
        })

    return output


def _tokenize(text: str) -> list[str]:
    """Tokenize text for BM25. Preserves version strings, hyphenated terms, and dotted identifiers."""
    return re.findall(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", text.lower())


def build_bm25_index(
    chunks: list[PatchChunk],
) -> tuple[BM25Okapi, list[PatchChunk]]:
    """
    Build a BM25 index from a list of PatchChunk objects.

    Returns:
        A tuple of (BM25Okapi index, original chunk list). The chunk list
        is needed to look up results since BM25 returns positional indices.
    """
    corpus = [_tokenize(c.text) for c in chunks]
    index = BM25Okapi(corpus)
    logger.info(f"Built BM25 index over {len(chunks)} chunks.")
    return index, chunks


def query_bm25(
    index: BM25Okapi,
    chunks: list[PatchChunk],
    query_text: str,
    n_results: int = 8,
) -> list[dict]:
    """
    Query a BM25 index for the most relevant chunks.

    Tokenizes the query with the same normalization used for the corpus, including
    lowercasing, punctuation removal, and preservation of dotted/hyphenated terms.
    Returns top n_results sorted by score descending, excluding zero-score results.

    Returns:
        A list of dicts with: chunk_id, text, score, metadata.
    """
    tokenized_query = _tokenize(query_text)
    scores = index.get_scores(tokenized_query)

    top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:n_results]

    output = []
    for idx in top_indices:
        score = float(scores[idx])
        if score <= 0:
            break

        chunk = chunks[idx]
        output.append({
            "chunk_id": chunk.chunk_id,
            "text": chunk.text,
            "score": score,
            "retriever": "bm25",
            "rank": len(output),
            "metadata": {
                "patch_version": chunk.patch_version,
                "patch_date": chunk.patch_date,
                "section": chunk.section,
                "news_type": chunk.news_type,
                "source_gid": chunk.source_gid,
                "source_url": chunk.source_url,
                "app_id": chunk.app_id,
            },
        })

    return output


def reciprocal_rank_fusion(
    vector_results: list[dict],
    bm25_results: list[dict],
    k: int = RRF_K,
) -> list[dict]:
    """
    Merge vector and BM25 results using Reciprocal Rank Fusion.

    Parameters:
        vector_results: Output from query_similar().
        bm25_results: Output from query_bm25().
        k: RRF constant (default 60). Higher values flatten rank differences.

    Returns:
        A list of dicts sorted by rrf_score descending, each with:
        chunk_id, text, metadata, rrf_score, retrievers.
    """
    scores: dict[str, float] = {}
    docs: dict[str, dict] = {}
    retrievers: dict[str, list[str]] = {}

    for r in vector_results:
        cid = r["chunk_id"]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + r["rank"])
        if cid not in docs:
            docs[cid] = r
        retrievers.setdefault(cid, []).append("vector")

    for r in bm25_results:
        cid = r["chunk_id"]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + r["rank"])
        if cid not in docs:
            docs[cid] = r
        retrievers.setdefault(cid, []).append("bm25")

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    output = []
    for cid, rrf_score in ranked:
        doc = docs[cid]
        output.append({
            "chunk_id": cid,
            "text": doc["text"],
            "metadata": doc["metadata"],
            "rrf_score": rrf_score,
            "retrievers": retrievers[cid],
        })

    return output
