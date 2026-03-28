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