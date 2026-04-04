"""
Loads environment variables from .env file and retrieves them.
"""

import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

STEAM_API_KEY = os.getenv("STEAM_API_KEY")
STEAM_API_URL = "https://store.steampowered.com/appreviews/{app_id}?json=1"

DB_PATH = "reviews.db"

CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
CLASSIFIER_TEMPERATURE = 0.0
CLASSIFIER_MAX_TOKENS = 500
CONFIDENCE_THRESHOLD = 0.7

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

AGENT_MAX_ITERATIONS = 3
CHECKPOINT_BACKEND = "memory"
CHECKPOINT_DB_PATH = "checkpoints.db"

STEAM_NEWS_API_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid={app_id}"
PATCH_NOTE_MAX_ITEMS = 50
PATCH_NOTE_EXTRA_FEEDS: list[str] = []

CHUNK_MAX_LENGTH = 500