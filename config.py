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

PARENT_CHUNK_MAX_LENGTH = 2000          # Budget for matched-centered parent context (display only, not embedded)
PARENT_CONTEXT_INVESTIGATOR = True      # Investigator sees [matched] + [context] format
PARENT_CONTEXT_RESPONDER = True         # Responder/Critic see parent context in evidence_sources
PARENT_DEDUP_ENABLED = False            # Same-parent dedup in retrieve() (off initially — test context gain first)
PARENT_DEDUP_MAX_PER_PARENT = 2         # When dedup enabled: max children kept per parent (not hard-1)
EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"
SIMILARITY_THRESHOLD = 0.3
VECTOR_TOP_K = 8
BM25_TOP_K = 8
RRF_K = 60
RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
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

INVESTIGATOR_MAX_TOOL_CALLS = 4  # Hard cap on retrieve_patches tool calls per investigator invocation

CLASSIFICATION_LIMIT = 200  # Default number of reviews to classify per run

TEST_APP_ID = "2246340"  # Monster Hunter Wilds — used by test scripts