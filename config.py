"""
Configuration — models, temperatures, thresholds, retrieval params, env vars.
"""

import os
from dotenv import load_dotenv

load_dotenv()

CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
HF_TOKEN = os.getenv("HF_TOKEN")

STEAM_API_KEY = os.getenv("STEAM_API_KEY")
STEAM_API_URL = "https://store.steampowered.com/appreviews/{app_id}?json=1"

DB_PATH = "reviews.db"

CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
CLASSIFIER_TEMPERATURE = 0.0
CLASSIFIER_MAX_TOKENS = 500

TONE_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
TONE_CLASSIFIER_TEMPERATURE = 0.0
TONE_CLASSIFIER_MAX_TOKENS = 50
CONFIDENCE_THRESHOLD = 0.7

REVIEW_MIN_CHARS = 5
REVIEW_MIN_WORDS = 3
NEAR_DUPLICATE_THRESHOLD = 0.85

PROPOSED_ACTIONS = ("no_action", "monitor", "investigate", "escalate")

REVIEW_CATEGORIES = [
    "technical_issues",
    "performance_optimization",
    "gameplay_mechanics",
    "balance_difficulty",
    "ui_controls",
    "content_progression",
    "multiplayer_network",
    "story_presentation",
    "monetization_value",
    "other"
]

CLUSTER_TIME_WINDOW_DAYS = 30
CLUSTER_MIN_REVIEWS = 3
PRIORITY_WEIGHTS = {
    "volume": 0.3,
    "velocity": 0.3,
    "sentiment": 0.2,
    "rating_impact": 0.2,
}
CLUSTER_SUMMARY_MODEL = "claude-haiku-4-5-20251001"
CLUSTER_SUMMARY_TEMPERATURE = 0.2
CLUSTER_SUMMARY_MAX_TOKENS = 600

CLUSTER_NOTE_STALENESS_DAYS = 90
CLUSTER_NOTE_DEDUP_WINDOW_HOURS = 24
CLUSTER_NOTE_AUTO_MIN_SOURCES = 2
CLUSTER_NOTE_STATUSES = ["active", "resolved"]

AGENT_MAX_ITERATIONS = 3
CHECKPOINT_BACKEND = "sqlite"
CHECKPOINT_DB_PATH = "checkpoints.db"

STEAM_NEWS_API_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid={app_id}"
PATCH_NOTE_MAX_ITEMS = 100

CHUNK_MAX_LENGTH = 500
CHUNK_MIN_TEXT_LENGTH = 10

EMBEDDING_BATCH_SIZE = 100

CHROMA_PERSIST_DIR = "chroma_db"

PARENT_CHUNK_MAX_LENGTH = 2000          # Budget for bullet-aware parent context (display only, not embedded)
PARENT_CONTEXT_SIBLING_BULLETS = 3      # Max sibling bullets before/after matched child in context window
PARENT_METADATA_WARN_CHARS = 10_000     # Log warning when parent metadata exceeds this per child
PARENT_CONTEXT_INVESTIGATOR = True      # Flip first in staged rollout
PARENT_CONTEXT_RESPONDER = True         # Flip second after investigator context is measured
PARENT_DEDUP_ENABLED = False            # Reverted — dedup dropped concept-carrying chunks, regressed retrieval metrics
PARENT_DEDUP_MAX_PER_PARENT = 2         # When dedup enabled: max children kept per parent (not hard-1)
RERANKER_USE_FP16 = True                # Use fp16 for Gemma reranker; set False on hardware without fp16 support
RERANKER_HYDE_AUGMENT = False             # Augment reranker query with HyDE hypothetical doc; reverted — displaced load-bearing chunks
RERANKER_HYDE_MAX_CHARS = 150             # Truncate hypothetical doc for reranker query (query dominance guard)
RERANKER_HYDE_AUGMENT_DIAGNOSTIC = False  # Log rank-shift + membership diff (2x reranker cost); disable in production

# HyDE (Hypothetical Document Embedding) retrieval
HYDE_ENABLED = True                           # Feature flag — staged rollout
HYDE_MODEL = "claude-haiku-4-5-20251001"      # Narrow generation task; Haiku is sufficient
HYDE_TEMPERATURE = 0.3                        # Low creativity — paraphrastic bridging, not invention
HYDE_MAX_TOKENS = 200                         # A single patch note bullet is ~50-100 tokens
HYDE_TOP_K = 5                                # Focused contribution — avoids flooding RRF pool
HYDE_MAX_PER_PARENT = 2                       # Pre-RRF diversity cap on HyDE results

# BM25-based HyDE gate — skip HyDE when BM25 is clearly rich enough
# Corpus-tuned gate heuristics — revisit immediately after first eval replay.
# AND logic makes the gate conservative; these numbers are calibration starters,
# not principled thresholds.
HYDE_GATE_ENABLED = False                     # Disabled — let HyDE run unconditionally; gate saves pennies but masks impact
HYDE_GATE_MIN_NONZERO_HITS = 6                # ~rich BM25 set out of top-8 (BM25_TOP_K); adjust if BM25_TOP_K changes
HYDE_GATE_MIN_TOP_SCORE = 8.0                 # AND BM25 top score >= this; calibrate after first gate-audit replay

EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"
SIMILARITY_THRESHOLD = 0.3
VECTOR_TOP_K = 8
BM25_TOP_K = 8
RRF_K = 60
RERANKER_MODEL = "BAAI/bge-reranker-v2-gemma"
RERANKER_TOP_N = 5

INVESTIGATOR_MODEL = "claude-sonnet-4-6"
INVESTIGATOR_TEMPERATURE = 0.1
INVESTIGATOR_MAX_TOKENS = 1500

RESPONDER_MODEL = "claude-sonnet-4-6"
RESPONDER_TEMPERATURE = 0.4
RESPONDER_MAX_TOKENS = 1000

RESPONDER_USE_FEEDBACK_EXAMPLES = True     # Feature flag — disable to roll back
RESPONDER_FEEDBACK_EXAMPLES = 3            # Max approved examples loaded from audit_log
RESPONDER_FEEDBACK_MAX_REVIEW_CHARS = 300  # Truncation limit for review_text in examples
RESPONDER_FEEDBACK_MAX_RESPONSE_CHARS = 500  # Truncation limit for drafted_response in examples
RESPONDER_FEEDBACK_MAX_SUMMARY_CHARS = 300   # Truncation limit for evidence_summary in examples
RESPONDER_FEEDBACK_SELECTION_POOL = 25       # Candidate pool size for diversity-aware selection

CRITIC_MODEL = "claude-haiku-4-5-20251001"
CRITIC_TEMPERATURE = 0.1
CRITIC_MAX_TOKENS = 1000

# Judge scorers (shared across all five eval judges: grounding, action,
# pairwise, pool-sufficiency, draft-grounding). Narrow single-question
# classifiers; Haiku is fine. If rulings look unreliable on the two-sided
# acceptance gate, upgrade in a follow-up iteration.
JUDGE_MODEL = "claude-haiku-4-5-20251001"
JUDGE_TEMPERATURE = 0.0
JUDGE_MAX_TOKENS = 300
JUDGE_WORKERS = 10  # Per-judge thread pool size for parallel LLM calls during eval scoring

RETRIEVAL_CATEGORIES = [
    "technical_issues",
    "performance_optimization",
    "gameplay_mechanics",
    "balance_difficulty",
    "ui_controls",
    "content_progression",
    "multiplayer_network",
    "story_presentation",
    "monetization_value",
]

INVESTIGATOR_MAX_TOOL_CALLS = 4  # Hard cap on PRIMARY retrieve_patches calls per investigator invocation
INVESTIGATOR_SECONDARY_PROBE_BUDGET = 1  # Extra call budget reserved for secondary-aspect probes on multipart reviews. Not consumed by primary calls.

CLASSIFIER_WORKERS = 10     # Thread pool size for parallel classification
CLASSIFICATION_LIMIT = 200  # Default number of reviews to classify per run

TEST_APP_ID = "2246340"  # Monster Hunter Wilds — used by test scripts