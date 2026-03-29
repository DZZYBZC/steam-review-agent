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