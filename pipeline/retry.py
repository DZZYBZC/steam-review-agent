"""
Shared retry-with-backoff logic for HTTP requests.
"""

import time
import logging
import requests

logger = logging.getLogger(__name__)


def fetch_with_retries(
    url: str,
    params: dict,
    max_retries: int = 3,
    timeout: int = 30,
    context: str = "request",
) -> requests.Response:
    """
    Make an HTTP GET request with exponential backoff on transient failures.

    Retries on: HTTP 429, 5xx, ConnectionError, Timeout.
    Raises on: other HTTP errors, or all retries exhausted.

    Parameters:
        url: The URL to fetch.
        params: Query parameters.
        max_retries: How many attempts before giving up.
        timeout: Seconds to wait per request.
        context: Label for log messages (e.g., "reviews for app 123").

    Returns:
        The successful Response object.
    """
    for attempt in range(max_retries):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else None

            if status_code == 429 or (status_code and status_code >= 500):
                wait_time = 2 ** attempt
                logger.warning(
                    f"HTTP {status_code} on attempt {attempt + 1}/{max_retries} "
                    f"({context}). Retrying in {wait_time}s..."
                )
                time.sleep(wait_time)
            else:
                logger.error(f"HTTP error {status_code}: {e}")
                raise

        except requests.exceptions.ConnectionError:
            wait_time = 2 ** attempt
            logger.warning(
                f"Connection error on attempt {attempt + 1}/{max_retries} "
                f"({context}). Retrying in {wait_time}s..."
            )
            time.sleep(wait_time)

        except requests.exceptions.Timeout:
            wait_time = 2 ** attempt
            logger.warning(
                f"Timeout on attempt {attempt + 1}/{max_retries} "
                f"({context}). Retrying in {wait_time}s..."
            )
            time.sleep(wait_time)

    logger.error(f"All {max_retries} attempts failed ({context}).")
    raise Exception(f"Failed to fetch {context} after {max_retries} retries")
