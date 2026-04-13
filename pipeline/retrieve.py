"""
Embeds patch note chunks into ChromaDB for similarity search.

Uses sentence-transformers for embeddings and ChromaDB with persistent
storage. The embedding model is loaded lazily and cached at module level.
"""

import json
import re
import logging
import chromadb
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer
from sentence_transformers import CrossEncoder
from config import (
    CHROMA_PERSIST_DIR,
    CHUNK_MAX_LENGTH,
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_MODEL,
    PARENT_CHUNK_MAX_LENGTH,
    PARENT_CONTEXT_SIBLING_BULLETS,
    PARENT_DEDUP_ENABLED,
    PARENT_DEDUP_MAX_PER_PARENT,
    PARENT_METADATA_WARN_CHARS,
    RERANKER_MODEL,
    RERANKER_TOP_N,
    SIMILARITY_THRESHOLD,
    VECTOR_TOP_K,
    BM25_TOP_K,
    RRF_K,
)
from pipeline.chunk import ChunkResult, PatchChunk

logger = logging.getLogger(__name__)

_model: SentenceTransformer | None = None
_reranker: CrossEncoder | None = None
_chroma_client: chromadb.ClientAPI | None = None
_bm25_cache: dict[str, tuple[BM25Okapi, list[dict]]] = {}

def _get_model() -> SentenceTransformer:
    """Load the embedding model lazily and cache it."""
    global _model
    if _model is None:
        logger.info(f"Loading embedding model: {EMBEDDING_MODEL}")
        _model = SentenceTransformer(EMBEDDING_MODEL)
    return _model


def _get_reranker() -> CrossEncoder:
    """Load the cross-encoder reranker lazily and cache it."""
    global _reranker
    if _reranker is None:
        logger.info(f"Loading reranker model: {RERANKER_MODEL}")
        _reranker = CrossEncoder(RERANKER_MODEL)
    return _reranker


def _get_client() -> chromadb.ClientAPI:
    """Get or create a persistent ChromaDB client (cached)."""
    global _chroma_client
    if _chroma_client is None:
        _chroma_client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
    return _chroma_client


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
    chunk_result: ChunkResult, app_id: str
) -> chromadb.Collection:
    """
    Embed child chunks into ChromaDB with parent context metadata.

    Uses upsert for idempotency — re-running with the same chunk_ids
    will update existing entries rather than creating duplicates.
    Batches upserts in groups of 100.

    Parent chunks are not embedded — their text and bullet lists are
    stored as metadata on each child for context augmentation at
    retrieval time.

    Returns:
        A ChromaDB collection for downstream queries.
    """
    model = _get_model()
    client = _get_client()
    collection = _get_or_create_collection(client, app_id)

    chunks = chunk_result.children

    parent_text_lookup: dict[str, str] = {}
    parent_bullets_lookup: dict[str, list[str]] = {}
    for p in chunk_result.parents:
        parent_text_lookup[p.parent_id] = p.text
        parent_bullets_lookup[p.parent_id] = p.bullet_texts

    batch_size = EMBEDDING_BATCH_SIZE
    total = len(chunks)

    for start in range(0, total, batch_size):
        batch = chunks[start : start + batch_size]

        ids = [c.chunk_id for c in batch]
        documents = [c.text for c in batch]
        embeddings = model.encode(documents).tolist()

        metadatas: list[dict[str, str | int | float | bool]] = []
        for c in batch:
            parent_text = parent_text_lookup.get(c.parent_id, "")
            all_bullets = parent_bullets_lookup.get(c.parent_id, [])

            # Pre-window: store only ±PARENT_CONTEXT_SIBLING_BULLETS around
            # this child's bullet_index. Avoids 50KB+ metadata on children in
            # huge headerless sections while preserving everything
            # _attach_parent_context() needs at retrieval time.
            # Individual bullets are also capped to CHUNK_MAX_LENGTH — some
            # patch notes have multi-paragraph bodies under one bullet that
            # _split_long_text() fans into multiple children, but the parent
            # context only needs enough to show what each sibling is about.
            if c.bullet_index >= 0 and len(all_bullets) > 2 * PARENT_CONTEXT_SIBLING_BULLETS + 1:
                lo = max(0, c.bullet_index - PARENT_CONTEXT_SIBLING_BULLETS)
                hi = min(len(all_bullets), c.bullet_index + PARENT_CONTEXT_SIBLING_BULLETS + 1)
                windowed_bullets = [b[:CHUNK_MAX_LENGTH] for b in all_bullets[lo:hi]]
                windowed_bullet_index = c.bullet_index - lo
            else:
                windowed_bullets = [b[:CHUNK_MAX_LENGTH] for b in all_bullets]
                windowed_bullet_index = c.bullet_index

            parent_bullets_json = json.dumps(windowed_bullets)

            if len(parent_text) + len(parent_bullets_json) > PARENT_METADATA_WARN_CHARS:
                logger.warning(
                    f"Large parent metadata for chunk {c.chunk_id}: "
                    f"{len(parent_text) + len(parent_bullets_json)} chars "
                    f"(parent_id={c.parent_id}, section={c.section})"
                )

            metadatas.append({
                "patch_version": c.patch_version,
                "patch_date": c.patch_date,
                "section": c.section,
                "news_type": c.news_type,
                "source_gid": c.source_gid,
                "source_url": c.source_url,
                "app_id": c.app_id,
                "parent_id": c.parent_id,
                "parent_text": parent_text,
                "parent_bullets": parent_bullets_json,
                "bullet_index": windowed_bullet_index,
            })

        collection.upsert(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas, # type: ignore[arg-type]
        )

    # Remove stale chunks no longer in the current batch
    current_ids = {c.chunk_id for c in chunks}
    existing_ids = set(collection.get(include=[])["ids"])
    stale_ids = list(existing_ids - current_ids)
    if stale_ids:
        collection.delete(ids=stale_ids)
        logger.info(f"Removed {len(stale_ids)} stale chunks from collection patches_{app_id}.")

    # Invalidate BM25 cache so it rebuilds from the updated collection
    _bm25_cache.pop(app_id, None)

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
    n_results: int = 12,
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

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:n_results]

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


def rerank(
    query_text: str,
    rrf_results: list[dict],
    top_n: int = RERANKER_TOP_N,
) -> list[dict]:
    """
    Re-score RRF candidates using a cross-encoder for fine-grained relevance.

    Scores each (query, chunk_text) pair with the cross-encoder, then returns
    the top_n results sorted by relevance score descending.

    Parameters:
        query_text: The player complaint or search query.
        rrf_results: Output from reciprocal_rank_fusion().
        top_n: Number of top results to return (default 5).

    Returns:
        A list of dicts sorted by relevance_score descending, each with:
        chunk_id, text, metadata, relevance_score, rrf_score, retrievers.
    """
    if not rrf_results:
        return []

    reranker = _get_reranker()

    pairs = [[query_text, r["text"]] for r in rrf_results]
    scores = reranker.predict(pairs)

    scored = []
    for i, r in enumerate(rrf_results):
        scored.append({
            "chunk_id": r["chunk_id"],
            "text": r["text"],
            "metadata": r["metadata"],
            "relevance_score": float(scores[i]),
            "rrf_score": r["rrf_score"],
            "retrievers": r["retrievers"],
        })

    scored.sort(key=lambda x: x["relevance_score"], reverse=True)
    return scored[:top_n]


def _get_bm25_index(app_id: str) -> tuple[BM25Okapi, list[dict]]:
    """
    Build or retrieve a cached BM25 index for an app.

    On first call, pulls all documents and metadata from ChromaDB
    and builds the index. Subsequent calls return the cached version.

    Returns:
        A tuple of (BM25Okapi index, list of doc dicts with chunk_id/text/metadata).
    """
    if app_id in _bm25_cache:
        return _bm25_cache[app_id]

    client = _get_client()
    collection = _get_or_create_collection(client, app_id)
    all_data = collection.get(include=["documents", "metadatas"])

    ids = all_data["ids"]
    documents = all_data["documents"]
    metadatas = all_data["metadatas"]

    if not ids or not documents or not metadatas:
        logger.warning(f"No documents found in collection patches_{app_id}.")
        empty_index = BM25Okapi([[]])
        _bm25_cache[app_id] = (empty_index, [])
        return empty_index, []

    docs = []
    corpus = []
    for i in range(len(ids)):
        docs.append({
            "chunk_id": ids[i],
            "text": documents[i],
            "metadata": metadatas[i],
        })
        corpus.append(_tokenize(documents[i]))

    index = BM25Okapi(corpus)
    logger.info(f"Built and cached BM25 index for app {app_id} ({len(docs)} documents).")
    _bm25_cache[app_id] = (index, docs)
    return index, docs


def _query_bm25_from_cache(
    app_id: str,
    query_text: str,
    n_results: int = BM25_TOP_K,
) -> list[dict]:
    """
    Query the cached BM25 index for an app.

    Returns:
        A list of dicts with: chunk_id, text, score, retriever, rank, metadata.
    """
    index, docs = _get_bm25_index(app_id)

    if not docs:
        return []

    tokenized_query = _tokenize(query_text)
    scores = index.get_scores(tokenized_query)

    top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:n_results]

    output = []
    for idx in top_indices:
        score = float(scores[idx])
        if score <= 0:
            break

        doc = docs[idx]
        output.append({
            "chunk_id": doc["chunk_id"],
            "text": doc["text"],
            "score": score,
            "retriever": "bm25",
            "rank": len(output),
            "metadata": doc["metadata"],
        })

    return output


def _attach_parent_context(reranked_results: list[dict]) -> list[dict]:
    """
    Attach parent context to each reranked result using bullet-aware truncation.

    Always runs as plumbing — downstream formatting decides whether to display
    parent context based on feature flags.

    For each result with parent metadata, locates the matched child via
    bullet_index (deterministic, no text matching), then takes up to
    PARENT_CONTEXT_SIBLING_BULLETS siblings before/after. Trims outer
    bullets first if the total exceeds PARENT_CHUNK_MAX_LENGTH.

    Adds two fields to each result dict:
        child_text: the original child text (always set)
        parent_context: the truncated parent section text (set when parent metadata exists)
    """
    output = []
    for r in reranked_results:
        r = dict(r)  # shallow copy to avoid mutating upstream
        r["child_text"] = r["text"]

        meta = r.get("metadata", {})
        parent_bullets_raw = meta.get("parent_bullets", "")
        bullet_index = meta.get("bullet_index", -1)

        if not parent_bullets_raw or bullet_index == -1:
            output.append(r)
            continue

        try:
            bullets = json.loads(parent_bullets_raw)
        except (json.JSONDecodeError, TypeError):
            output.append(r)
            continue

        if not isinstance(bullets, list) or bullet_index < 0 or bullet_index >= len(bullets):
            logger.warning(
                f"bullet_index {bullet_index} out of range for {len(bullets)} bullets "
                f"(chunk_id={r.get('chunk_id', '?')}). Falling back to section start."
            )
            # Fallback: use full parent_text with head truncation
            parent_text = meta.get("parent_text", "")
            if parent_text:
                r["parent_context"] = parent_text[:PARENT_CHUNK_MAX_LENGTH]
            output.append(r)
            continue

        # Build context window centered on matched bullet
        lo = max(0, bullet_index - PARENT_CONTEXT_SIBLING_BULLETS)
        hi = min(len(bullets), bullet_index + PARENT_CONTEXT_SIBLING_BULLETS + 1)
        window = bullets[lo:hi]

        version = meta.get("patch_version", "")
        section = meta.get("section", "")
        prefix = f"{version} | {section}:\n" if version and section else ""
        context = prefix + "\n".join(window)

        # Trim outer bullets if over budget
        while len(context) > PARENT_CHUNK_MAX_LENGTH and (lo < bullet_index or hi > bullet_index + 1):
            # Remove the bullet farthest from the match
            dist_lo = bullet_index - lo
            dist_hi = hi - 1 - bullet_index
            if dist_lo >= dist_hi and lo < bullet_index:
                lo += 1
            elif hi > bullet_index + 1:
                hi -= 1
            else:
                lo += 1
            window = bullets[lo:hi]
            context = prefix + "\n".join(window)

        r["parent_context"] = context
        output.append(r)

    return output


def _dedup_by_parent(
    results: list[dict], max_per_parent: int = PARENT_DEDUP_MAX_PER_PARENT
) -> list[dict]:
    """
    Limit results to max_per_parent children per parent section.

    Keeps the top children by relevance_score for each parent_id group.
    Results without a parent_id pass through unchanged.
    """
    parent_counts: dict[str, int] = {}
    output = []

    for r in results:
        parent_id = r.get("metadata", {}).get("parent_id", "")
        if not parent_id:
            output.append(r)
            continue

        count = parent_counts.get(parent_id, 0)
        if count < max_per_parent:
            parent_counts[parent_id] = count + 1
            output.append(r)

    return output


def retrieve(
    query_text: str,
    app_id: str,
) -> list[dict]:
    """
    Run the full hybrid retrieval pipeline: vector search + BM25 → RRF fusion → rerank.

    Both indexes are read from persistent storage (ChromaDB for vector,
    cached BM25 built from ChromaDB documents). No chunk list needed.

    Parameters:
        query_text: The search query (review text or reformulated query).
        app_id: Steam app ID, used to select the ChromaDB collection.

    Returns:
        Top reranked results from rerank(), or an empty list if no
        results survive the pipeline.
    """
    # Vector search
    client = _get_client()
    collection = _get_or_create_collection(client, app_id)
    vector_results = query_similar(collection, query_text, n_results=VECTOR_TOP_K)

    # BM25 search (from cached index)
    bm25_results = _query_bm25_from_cache(app_id, query_text, n_results=BM25_TOP_K)

    # Fuse and rerank
    rrf_results = reciprocal_rank_fusion(vector_results, bm25_results)
    reranked = rerank(query_text, rrf_results)

    # Parent context augmentation (always: plumbing)
    reranked = _attach_parent_context(reranked)

    # Optional same-parent dedup (off by default)
    if PARENT_DEDUP_ENABLED:
        reranked = _dedup_by_parent(reranked, PARENT_DEDUP_MAX_PER_PARENT)

    return reranked
