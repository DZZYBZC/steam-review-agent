"""
Loads environment variables from .env file and retrieves them.
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
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
SIMILARITY_THRESHOLD = 0.3
VECTOR_TOP_K = 8
BM25_TOP_K = 8
RRF_K = 60
RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
RERANKER_TOP_N = 5

INVESTIGATOR_MODEL = "claude-haiku-4-5-20251001"
INVESTIGATOR_TEMPERATURE = 0.1
INVESTIGATOR_MAX_TOKENS = 1500  # Bumped from 400 to fit tool-use turns (text reasoning + tool_use blocks + final JSON)

RESPONDER_MODEL = "claude-sonnet-4-6"
RESPONDER_TEMPERATURE = 0.4
RESPONDER_MAX_TOKENS = 1000

CRITIC_MODEL = "claude-haiku-4-5-20251001"
CRITIC_TEMPERATURE = 0.1
CRITIC_MAX_TOKENS = 1000

# Judge scorer (evals/scorers/judge_grounding.py).
# Narrow single-question classifier; Haiku is fine. If rulings look unreliable
# on the two-sided acceptance gate, upgrade in a follow-up iteration.
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

INVESTIGATOR_MAX_TOOL_CALLS = 3  # Hard cap on retrieve_patches tool calls per investigator invocation

CLASSIFICATION_LIMIT = 200  # Default number of reviews to classify per run

TEST_APP_ID = "2246340"  # Monster Hunter Wilds — used by test scripts