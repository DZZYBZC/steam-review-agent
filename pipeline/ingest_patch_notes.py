"""
Fetches game news from the Steam News API.
"""

import re
import time
import logging
import requests
from config import STEAM_NEWS_API_URL, PATCH_NOTE_MAX_ITEMS

logger = logging.getLogger(__name__)

_DEFAULT_FEED = "steam_community_announcements"

_TITLE_NEWSLETTER_SIGNALS = re.compile(
    r"newsletter|neowsletter|digest|community\s+hub|director'?s?\s+letter",
    re.IGNORECASE,
)

_TITLE_EVENT_SIGNALS = re.compile(
    r"watch\b|livestream|live\s+on\b|free\s+weekend|twitch",
    re.IGNORECASE,
)

_TITLE_PATCH_SIGNALS = re.compile(
    r"patch\b|hotfix|changelog|update\s+summary|"
    r"patch\s+notes?|release\s+notes?|bug\s*fix|fixed\s+\w+|"
    r"update\s+\d+\.\d+|"
    r"\w+\s+update\s*$",
    re.IGNORECASE,
)

_CONTENT_PATCH_SIGNALS = re.compile(
    r"bug\s*fix|balance\s*change|patch\s+notes?|"
    r"fixed\s+(?:a|an|the|crash|issue|bug|error|problem)|"
    r"fix\s+for\b|fixes\s+(?:a|an|the|for|to|crash|issue|bug)|"
    r"crash\s+fix|"
    r"resolved\s+(?:a|an|the|issue|bug|crash)|"
    r"addressed\s+(?:a|an|the|issue|bug)|"
    r"corrected\s+(?:a|an|the)",
    re.IGNORECASE,
)

_CONTENT_SIGNALS = re.compile(
    r"new\s+content|expansion|new\s+map|new\s+weapons?|"
    r"new\s+area|new\s+quest|new\s+dungeon|new\s+feature|"
    r"major\s+update|content\s+update|added\s+new|now\s+available|"
    r"introducing|DLC",
    re.IGNORECASE,
)

_EVENT_SIGNALS = re.compile(
    r"double\s+xp|sale|discount|\d+%\s+off|free\s+weekend|"
    r"tournament|community\s+event|giveaway|limited\s+time|"
    r"weekend\s+event|celebration|anniversary\s+event|"
    r"contest|sweepstakes|live\s+stream|"
    r"league|competition|compete|sign\s*ups?\b|1v1|2v2|"
    r"esports?|championship|showmatch|invitational",
    re.IGNORECASE,
)


def classify_news_type(item: dict) -> str:
    """
    Classify a Steam news item as "patch", "content_update", or "event".
    """
    title = item.get("title", "")
    contents_preview = item.get("contents", "")[:1000]

    if _TITLE_NEWSLETTER_SIGNALS.search(title):
        return "content_update"

    if _TITLE_EVENT_SIGNALS.search(title):
        return "event"

    if _TITLE_PATCH_SIGNALS.search(title):
        return "patch"

    if _CONTENT_PATCH_SIGNALS.search(contents_preview):
        return "patch"

    combined = f"{title} {contents_preview}"
    has_content = bool(_CONTENT_SIGNALS.search(combined))
    has_event = bool(_EVENT_SIGNALS.search(combined))

    if has_content and not has_event:
        return "content_update"
    elif has_event:
        return "event"
    elif has_content:
        return "event"
    else:
        return "content_update"


def _deduplicate_by_title(items: list[dict]) -> list[dict]:
    """
    Remove duplicate news items that appear across multiple feeds.
    """
    seen_titles = {}
    unique = []

    sorted_items = sorted(items, key=lambda x: x.get("date", 0), reverse=True)

    for item in sorted_items:
        title = item.get("title", "").strip().lower()
        if title not in seen_titles:
            seen_titles[title] = True
            unique.append(item)
        else:
            logger.debug(f"Dedup: dropping duplicate '{item.get('title', '')[:50]}' from feed '{item.get('feedname', '')}'")

    removed = len(items) - len(unique)
    if removed > 0:
        logger.info(f"Deduplication removed {removed} duplicate items.")

    return unique


def fetch_news(
    app_id: str,
    max_items: int | None = None,
    extra_feeds: list[str] | None = None,
    max_retries: int = 3,
    timeout: int = 30,
) -> list[dict]:
    """
    Fetch and classify game news from the Steam News API.

    Deduplicates by title and classifies each item as "patch",
    "content_update", or "event". Event items are dropped.
    External press is excluded at the API level via the feeds parameter.

    Parameters:
        app_id: The Steam game ID.
        max_items: Maximum number of raw news items to request from the API.
        extra_feeds: Additional feed names to request beyond the default
                     (steam_community_announcements). Game-specific blogs
                     like "tf2_blog" go here. Configure in config.py.
        max_retries: Retry count for transient failures (429, 5xx, timeouts).
        timeout: Seconds to wait for the server to respond.

    Returns:
        A list of news item dicts, each with an added `news_type` field.
    """
    if max_items is None:
        max_items = PATCH_NOTE_MAX_ITEMS

    feeds = [_DEFAULT_FEED]
    if extra_feeds:
        feeds.extend(extra_feeds)
    feeds_param = ",".join(feeds)

    url = STEAM_NEWS_API_URL.format(app_id=app_id)

    params = {
        "count": max_items,
        "maxlength": 0,  # 0 = return full content
        "feeds": feeds_param,
    }

    for attempt in range(max_retries):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            break

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else None

            if status_code == 429 or (status_code and status_code >= 500):
                wait_time = 2 ** attempt
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
    else:
        logger.error(f"All {max_retries} attempts failed for app {app_id}.")
        raise Exception(f"Failed to fetch news after {max_retries} retries")

    app_news = data.get("appnews", {})
    all_items = app_news.get("newsitems", [])
    logger.info(f"Steam News API returned {len(all_items)} total items for app {app_id}.")

    dev_items = _deduplicate_by_title(all_items)

    counts = {"patch": 0, "content_update": 0, "event": 0}
    kept_items = []

    for item in dev_items:
        news_type = classify_news_type(item)
        counts[news_type] += 1

        if news_type == "event":
            continue

        item["news_type"] = news_type
        kept_items.append(item)

    logger.info(
        f"Classified {len(dev_items)} dev items: "
        f"{counts['patch']} patch, {counts['content_update']} content_update, "
        f"{counts['event']} event (dropped). Keeping {len(kept_items)}."
    )

    return kept_items