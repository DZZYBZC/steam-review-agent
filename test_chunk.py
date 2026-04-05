"""
test_chunk.py — Verify the chunker handles real-world Steam formatting.

Tests:
1. BBCode markup is stripped from chunk text
2. HTML markup is stripped from chunk text
3. Image URLs are extracted and stored as metadata
4. news_type classification heuristics

Usage: python test_chunk.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline.chunk import (
    strip_markup,
    extract_image_urls,
)
from pipeline.ingest_patch_notes import classify_news_type


def test_markup_stripping():
    """Verify BBCode and HTML are removed from chunk text."""
    print("--- Markup stripping ---")

    # BBCode test
    bbcode_text = "[b]Bug Fixes[/b]\n[list]\n[*] Fixed a crash\n[/list]"
    cleaned = strip_markup(bbcode_text)
    assert "[b]" not in cleaned, f"BBCode [b] tag not stripped: {cleaned}"
    assert "[list]" not in cleaned, f"BBCode [list] tag not stripped: {cleaned}"
    assert "[*]" not in cleaned, f"BBCode [*] tag not stripped: {cleaned}"
    assert "Bug Fixes" in cleaned, f"Text content was lost: {cleaned}"
    assert "Fixed a crash" in cleaned, f"Text content was lost: {cleaned}"
    print(f"  BBCode stripped: '{cleaned}'")

    # HTML test
    html_text = '<b>Bug Fixes</b><br>\n<li>Fixed a crash</li>'
    cleaned = strip_markup(html_text)
    assert "<b>" not in cleaned, f"HTML <b> tag not stripped: {cleaned}"
    assert "<li>" not in cleaned, f"HTML <li> tag not stripped: {cleaned}"
    assert "<br>" not in cleaned, f"HTML <br> tag not stripped: {cleaned}"
    assert "Bug Fixes" in cleaned, f"Text content was lost: {cleaned}"
    print(f"  HTML stripped: '{cleaned}'")

    # Steam clan image test
    steam_text = "[img]{STEAM_CLAN_IMAGE}/99999/banner.jpg[/img]\nSome content"
    cleaned = strip_markup(steam_text)
    assert "STEAM_CLAN_IMAGE" not in cleaned, f"Steam clan image not stripped: {cleaned}"
    assert "Some content" in cleaned, f"Text content was lost: {cleaned}"
    print(f"  Steam image stripped: '{cleaned}'")

    # URL tag test (content preserved, tag removed)
    url_text = "Check the [url=https://example.com]patch notes[/url] here"
    cleaned = strip_markup(url_text)
    assert "[url" not in cleaned, f"URL tag not stripped: {cleaned}"
    assert "patch notes" in cleaned, f"URL text content was lost: {cleaned}"
    print(f"  URL tag stripped: '{cleaned}'")

    print("  ✓ All markup stripping tests passed")


def test_image_extraction():
    """Verify image URLs are captured before stripping."""
    print("\n--- Image URL extraction ---")

    # BBCode images
    text_bbcode = "[img]https://cdn.example/a.png[/img] some text [img]https://cdn.example/b.jpg[/img]"
    urls = extract_image_urls(text_bbcode)
    assert len(urls) == 2, f"Expected 2 BBCode image URLs, got {len(urls)}"
    assert "https://cdn.example/a.png" in urls
    print(f"  BBCode images: {urls}")

    # HTML images
    text_html = '<img src="https://cdn.example/c.png"> text <img src="https://cdn.example/d.jpg"/>'
    urls = extract_image_urls(text_html)
    assert len(urls) == 2, f"Expected 2 HTML image URLs, got {len(urls)}"
    print(f"  HTML images: {urls}")

    # Deduplication
    text_dupe = "[img]https://cdn.example/same.png[/img]\n[img]https://cdn.example/same.png[/img]"
    urls = extract_image_urls(text_dupe)
    assert len(urls) == 1, f"Expected 1 deduplicated URL, got {len(urls)}"
    print(f"  Deduplicated: {urls}")

    print("  ✓ All image extraction tests passed")


def test_news_type_classification():
    """Verify fetcher classifies news items correctly."""
    print("\n--- News type classification ---")

    cases = [
        ({"title": "Patch 1.3.2 — Hotfix", "contents": ""}, "patch", "Hotfix title"),
        ({"title": "Patch 1.3.0 — The Frosthollow Update", "contents": ""}, "patch", "Patch in title"),
        ({"title": "Stability Update", "contents": ""}, "patch", "Game Update title"),
        ({"title": "Update 1.12.30", "contents": ""}, "patch", "Update + version"),
        ({"title": "Release Notes for March", "contents": ""}, "patch", "Release notes title"),
        ({"title": "Fixed Matchmaking Issues", "contents": ""}, "patch", "Fixed in title"),
        ({"title": "Some News Post", "contents": "We fixed a crash that affected players."}, "patch", "Fix in body"),
        ({"title": "Weekend Event: Double XP!", "contents": "All XP gains are doubled!"}, "event", "Double XP event"),
        ({"title": "Watch the Developer Livestream", "contents": "Join us on Twitch."}, "event", "Livestream title"),
        ({"title": "Season 2: Now Available!", "contents": "New content and new map."}, "content_update", "Content update"),
        ({"title": "The Neowsletter #5", "contents": "We fixed a bug and resolved an issue."}, "content_update", "Newsletter"),
        ({"title": "Hades II v1.0 Is Now Available!", "contents": "Now available for all."}, "content_update", "Launch announcement"),
    ]

    for item, expected, label in cases:
        result = classify_news_type(item)
        status = "✓" if result == expected else "✗"
        print(f"  {status} '{item['title'][:50]}' → {result} (expected {expected}) [{label}]")
        assert result == expected, f"FAILED: {label} — got '{result}', expected '{expected}'"

    print("  ✓ All classification tests passed")


def main():
    print("=" * 60)
    print("UNIT TESTS — Markup + Images + Classification")
    print("=" * 60)

    test_markup_stripping()
    test_image_extraction()
    test_news_type_classification()

    print(f"\n{'=' * 60}")
    print("✓ All tests passed.")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
