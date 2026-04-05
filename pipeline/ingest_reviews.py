"""
Fetches Steam game reviews from the Steam Store API.
"""

import time
import logging
import requests
from config import STEAM_API_URL, STEAM_API_KEY

logger = logging.getLogger(__name__)

def fetch_reviews_page(
    app_id: str,
    cursor: str = "*",
    num_per_page: int = 100,
    max_retries: int = 3,
    timeout: int = 30,
) -> dict:
    """
    Fetch a single page of reviews from the Steam API.

    Parameters:
        app_id: The Steam game ID
        cursor: Pagination bookmark.
        num_per_page: How many reviews per page (max 100).
        max_retries: How many times to retry on transient failures before giving up (exponential backoff).
        timeout: Seconds to wait for the server to respond before giving up.

    Returns:
        The full JSON response from the API as a Python dictionary.
    """
    url = STEAM_API_URL.format(app_id=app_id)

    params = {
        "filter": "recent",
        "language": "english",
        "review_type": "all",
        "purchase_type": "steam",
        "num_per_page": num_per_page,
        "cursor": cursor,
    }

    if STEAM_API_KEY:
        params["key"] = STEAM_API_KEY

    for attempt in range(max_retries):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else None

            if status_code == 429 or (status_code and status_code >= 500):
                wait_time = 2 ** attempt # Exponential backoff time
                logger.warning(
                    f"HTTP {status_code} on attempt {attempt + 1}/{max_retries}. "
                    f"Retrying in {wait_time}s..."
                )
                time.sleep(wait_time)
            else:
                logger.error(f"HTTP error {status_code}: {e}")
                raise

        except requests.exceptions.ConnectionError:
            wait_time = 2 ** attempt
            logger.warning(
                f"Connection error on attempt {attempt + 1}/{max_retries}. "
                f"Retrying in {wait_time}s..."
            )
            time.sleep(wait_time)

        except requests.exceptions.Timeout:
            wait_time = 2 ** attempt
            logger.warning(
                f"Timeout on attempt {attempt + 1}/{max_retries}. "
                f"Retrying in {wait_time}s..."
            )
            time.sleep(wait_time)

    logger.error(f"All {max_retries} attempts failed for app {app_id}.")
    raise Exception(f"Failed to fetch reviews after {max_retries} retries")


def fetch_all_reviews(
    app_id: str,
    cursor: str = "*",
    max_reviews: int = 500,
    max_retries: int = 3,
    timeout: int = 30,
) -> list[dict]:
    """
    Fetch multiple pages of reviews, following the cursor until we have enough.

    Parameters:
        app_id: The Steam game ID.
        max_reviews: Stop after collecting this many reviews.

    Returns:
        A list of review dictionaries
    """
    all_reviews = []
    pages_fetched = 0

    logger.info(f"Starting to fetch reviews for app {app_id} (max {max_reviews})...")

    while len(all_reviews) < max_reviews:
        data = fetch_reviews_page(app_id, cursor, max_retries=max_retries, timeout=timeout)

        if not data.get("success"):
            logger.error("Steam API returned success=0. Response may be invalid.")
            break

        reviews = data.get("reviews", [])

        if not reviews:
            logger.info("No more reviews returned. Reached the end.")
            break

        all_reviews.extend(reviews)
        pages_fetched += 1
        new_cursor = data.get("cursor")

        if new_cursor is None or new_cursor == cursor:
            logger.info("No more pages available.")
            break

        cursor = new_cursor
        
        logger.info(
            f"Page {pages_fetched}: got {len(reviews)} reviews "
            f"(total so far: {len(all_reviews)})"
        )

        time.sleep(0.5)

    all_reviews = all_reviews[:max_reviews]
    logger.info(f"Done. Fetched {len(all_reviews)} reviews total.")

    return all_reviews