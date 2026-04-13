"""
Section-aware chunking for Steam game news. One chunk per fix/change line,
with the patch version header prepended so each chunk is self-contained.
Section headers and source metadata (patch ID, URL, date, news_type) travel
with every chunk; BBCode/HTML/image markup is stripped first (image URLs
preserved as metadata).
"""

import re
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from config import CHUNK_MAX_LENGTH, CHUNK_MIN_TEXT_LENGTH, PARENT_CHUNK_MAX_LENGTH

logger = logging.getLogger(__name__)


# Markup stripping

_BBCODE_IMG = re.compile(r"\[img\](.*?)\[/img\]", re.IGNORECASE)
_HTML_IMG = re.compile(r'<img\s+[^>]*src=["\']([^"\']+)["\'][^>]*/?\s*>', re.IGNORECASE)

_BBCODE_TAGS = re.compile(
    r"\[/?"
    r"(?:b|i|u|s|p|h[1-6]|list|"
    r"olist|\*|url|quote|code|"
    r"spoiler|strike|noparse|"
    r"table|tr|td|th|hr|"
    r"previewyoutube|img|"
    r"dynamiclink|localtime)"
    r"(?:\s[^\]]*|=[^\]]*)?]",
    re.IGNORECASE,
)

_HYBRID_IMG = re.compile(
    r"\[img\s+[^\]]*\]",
    re.IGNORECASE,
)

_HTML_TAGS = re.compile(r"<[^>]+>")
_MULTI_WHITESPACE = re.compile(r"[ \t]+")
_MULTI_NEWLINES = re.compile(r"\n{3,}")
_STEAM_CLAN_IMAGE = re.compile(r"\{STEAM_CLAN_IMAGE\}/\S+", re.IGNORECASE)


def extract_image_urls(text: str) -> list[str]:
    """
    Pull image URLs from BBCode [img] tags, HTML <img> tags, and hybrid [img src=] tags.
    """
    _HYBRID_IMG_URL = re.compile(
        r'\[img\s+src=["\']?([^"\'>\]\s]+)["\']?[^\]]*\]',
        re.IGNORECASE,
    )
    urls = []
    urls.extend(_BBCODE_IMG.findall(text))
    urls.extend(_HTML_IMG.findall(text))
    urls.extend(_HYBRID_IMG_URL.findall(text))
    seen = set()
    unique = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def strip_markup(text: str) -> str:
    """
    Remove BBCode, HTML, and Steam-specific markup from patch note content.
    """
    result = _BBCODE_IMG.sub("", text)
    result = _HTML_IMG.sub("", result)
    result = _HYBRID_IMG.sub("", result)
    result = _STEAM_CLAN_IMAGE.sub("", result)

    # Before stripping all tags, convert BBCode list items ([*]) and HTML list items (<li>) into plain dashes (-)
    result = re.sub(r"\[\*\]\s*", "- ", result)
    result = re.sub(r"<li[^>]*>\s*", "- ", result, flags=re.IGNORECASE)
    result = re.sub(r"</li>", "", result, flags=re.IGNORECASE)

    result = _BBCODE_TAGS.sub("", result)
    result = _HTML_TAGS.sub("", result)

    result = result.replace("&amp;", "&")
    result = result.replace("&lt;", "<")
    result = result.replace("&gt;", ">")
    result = result.replace("&nbsp;", " ")
    result = result.replace("&quot;", '"')

    result = re.sub(
        r"\\?\[\s*([A-Z][A-Z '/&\-]{1,57}?)\s*\\?\]",
        r"\n\1\n",
        result,
    )

    lines = result.split("\n")
    lines = [_MULTI_WHITESPACE.sub(" ", line).strip() for line in lines]
    result = "\n".join(lines)
    result = _MULTI_NEWLINES.sub("\n\n", result)

    return result.strip()


# Section header detection

_SECTION_HEADER_PATTERN = re.compile(
    r"^(?![-•*])"
    r"[A-Z]"
    r"[A-Za-z '/&\-]{2,58}"
    r"$"
)

_BRACKET_HEADER_PATTERN = re.compile(
    r"^\\?\[\s*([A-Za-z][A-Za-z '/&\-]{1,57}?)\s*\\?\]$"
)

_BULLET_PATTERN = re.compile(r"^\s*[-•*]|\s*\d+[.)]\s")

@dataclass
class PatchChunk:
    """
    A single searchable unit from a patch note, gets embedded and stored in the vector store.

    Attributes:
        chunk_id: Unique identifier (patch_gid + chunk index).
        text: The full chunk text, including the prepended version header.
        patch_version: The patch version string (e.g. "Patch 1.3.2 — Hotfix").
        patch_date: When this patch was released (ISO format string).
        section: The section header this chunk falls under (e.g. "Bug Fixes").
        news_type: "patch" or "content_update" — carried from the fetcher.
        source_gid: The Steam news item ID for traceability.
        source_url: Link back to the original patch note.
        app_id: The Steam game ID.
        image_urls: Any image URLs found in the source item (for dashboard display).
    """
    chunk_id: str
    text: str
    patch_version: str
    patch_date: str
    section: str
    news_type: str
    source_gid: str
    source_url: str
    app_id: str
    image_urls: list[str] = field(default_factory=list)
    parent_id: str = ""
    bullet_index: int = -1  # Position within parent's bullet list (0-based). -1 = unset.

    def to_dict(self) -> dict:
        """Convert to a plain dict for storage/serialization."""
        return asdict(self)


@dataclass
class ParentChunk:
    """
    A section-level aggregate of child chunks, used for context augmentation.

    Not embedded or stored in the vector index — carried as metadata on
    each child chunk so the investigator can see surrounding context.
    """
    parent_id: str
    text: str
    bullet_texts: list[str] = field(default_factory=list)
    patch_version: str = ""
    patch_date: str = ""
    section: str = ""
    news_type: str = ""
    source_gid: str = ""
    source_url: str = ""
    app_id: str = ""


@dataclass
class ChunkResult:
    """Container for chunking output: child chunks (for embedding) and parent chunks (for context)."""
    children: list[PatchChunk] = field(default_factory=list)
    parents: list[ParentChunk] = field(default_factory=list)


def _extract_version_header(title: str, contents: str) -> str:
    """
    Extract a clean version header from the patch note title or first line.

    Returns:
        A string like "Patch 1.3.2 — Hotfix (March 15, 2024)"
    """
    header = title.strip().rstrip("#").strip()

    if not header:
        for line in contents.split("\n"):
            line = line.strip()
            if line:
                header = line
                break

    return header if header else "Unknown Version"


def _is_section_header(line: str) -> str | None:
    """
    Check if a line is a section header and return the clean name.

    Matches plain headers ("Bug Fixes", "Performance") and bracket-style
    headers ("[ SOUND ]", "[GAMEPLAY]", "\\[ ENGINE ]"). Returns the clean
    section name with brackets/backslashes stripped, or None if not a header.
    """
    stripped = line.strip()

    if not stripped or len(stripped) < 3:
        return None

    if _BULLET_PATTERN.match(stripped):
        return None

    bracket_match = _BRACKET_HEADER_PATTERN.match(stripped)
    if bracket_match:
        return bracket_match.group(1).strip()

    if stripped.endswith((".", "!", "?")) and len(stripped) > 30:
        return None

    if _SECTION_HEADER_PATTERN.match(stripped):
        return stripped

    return None


def _clean_bullet_text(line: str) -> str:
    """
    Remove the leading bullet character and normalize whitespace.

    '- Fixed a crash...' → 'Fixed a crash...'
    '  • Improved FPS...' → 'Improved FPS...'
    """
    stripped = line.strip()
    cleaned = re.sub(r"^[-•*]\s*", "", stripped)
    cleaned = re.sub(r"^\d+[.)]\s*", "", cleaned)
    return cleaned.strip()


_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_BULLET_BOUNDARY = re.compile(r"(?<=\.)-\s*")
_DASH_BOUNDARY = re.compile(r"(?<=[a-z0-9)])-\s+")


def _split_with_pattern(body: str, prefix: str, max_length: int, pattern: re.Pattern) -> list[str]:
    """
    Split body at the given regex boundary, accumulating parts into segments
    that fit within max_length (including prefix). Returns segments if the
    split produced multiple pieces, or an empty list if it couldn't split.
    """
    parts = pattern.split(body)
    if len(parts) <= 1:
        return []

    segments: list[str] = []
    current = parts[0]

    for part in parts[1:]:
        candidate = f"{current} {part}"
        if len(f"{prefix}{candidate}") <= max_length:
            current = candidate
        else:
            segments.append(current)
            current = part

    segments.append(current)
    return segments if len(segments) > 1 else []


def _split_long_text(
    body: str, prefix: str, max_length: int = CHUNK_MAX_LENGTH
) -> list[str]:
    """
    Split body text at sentence or bullet boundaries if prefix + body exceeds
    max_length.

    Applies boundary types progressively: split at sentence boundaries first,
    then re-split any oversized segments at bullet boundaries (".- ").

    The prefix (version header + section) is prepended to every segment so
    each sub-chunk is self-contained. If no boundary is found, the text is
    kept as one chunk rather than splitting mid-word.

    Returns a list of one or more complete chunk texts.
    """
    full = f"{prefix}{body}"
    if len(full) <= max_length:
        return [full]

    # First pass: sentence boundaries
    bodies = _split_with_pattern(body, prefix, max_length, _SENTENCE_BOUNDARY)
    if not bodies:
        bodies = [body]

    # Subsequent passes: re-split any oversized segments at finer boundaries
    for pattern in [_BULLET_BOUNDARY, _DASH_BOUNDARY]:
        next_pass: list[str] = []
        for b in bodies:
            if len(f"{prefix}{b}") <= max_length:
                next_pass.append(b)
            else:
                sub = _split_with_pattern(b, prefix, max_length, pattern)
                if sub:
                    next_pass.extend(sub)
                else:
                    next_pass.append(b)
        bodies = next_pass

    return [f"{prefix}{b}" for b in bodies]


def chunk_patch_note(item: dict) -> ChunkResult:
    """
    Split a single news item into child chunks and parent section chunks.

    Each bullet point becomes its own child chunk (embedded and searched).
    Each section becomes a parent chunk (stored as metadata on children
    for context augmentation, never embedded).

    Markup (BBCode, HTML, image tags) is stripped before chunking.
    Image URLs are extracted and stored as metadata.

    Parameters:
        item: A news item dict from the Steam News API (or sample data).
              Expected keys: gid, title, contents, date, url, appid
              Optional keys: news_type (from fetcher classification)

    Returns:
        A ChunkResult with children (for embedding) and parents (for context).
    """
    title = item.get("title", "")
    raw_contents = item.get("contents", "")
    gid = str(item.get("gid", "unknown"))
    url = item.get("url", "")
    app_id = str(item.get("appid", ""))
    news_type = item.get("news_type", "patch")
    raw_date = item.get("date", 0)
    patch_date = datetime.fromtimestamp(raw_date, tz=timezone.utc).strftime("%Y-%m-%d")

    image_urls = extract_image_urls(raw_contents)
    contents = strip_markup(raw_contents)
    clean_title = strip_markup(title)
    version_header = _extract_version_header(clean_title, contents)

    chunks: list[PatchChunk] = []
    parents: list[ParentChunk] = []
    current_section = "General"
    chunk_index = 0
    section_index = 0
    bullet_index_in_section = 0
    section_bullet_texts: list[str] = []
    section_children: list[PatchChunk] = []

    def _finalize_section() -> None:
        """Build a ParentChunk from the accumulated section and link children."""
        nonlocal section_index
        if not section_bullet_texts:
            return

        parent_id = f"{gid}-s{section_index}"
        prefix = f"{version_header} | {current_section}:\n"
        parent_text = prefix + "\n".join(section_bullet_texts)
        if len(parent_text) > PARENT_CHUNK_MAX_LENGTH:
            # Truncate at last complete bullet boundary within budget
            truncated = prefix
            for bt in section_bullet_texts:
                candidate = truncated + bt + "\n"
                if len(candidate) > PARENT_CHUNK_MAX_LENGTH:
                    break
                truncated = candidate
            parent_text = truncated.rstrip("\n") if len(truncated) > len(prefix) else parent_text[:PARENT_CHUNK_MAX_LENGTH]

        parent = ParentChunk(
            parent_id=parent_id,
            text=parent_text,
            bullet_texts=list(section_bullet_texts),
            patch_version=version_header,
            patch_date=patch_date,
            section=current_section,
            news_type=news_type,
            source_gid=gid,
            source_url=url,
            app_id=app_id,
        )
        parents.append(parent)

        for child in section_children:
            child.parent_id = parent_id

    lines = contents.split("\n")

    for line in lines:
        stripped = line.strip()

        if not stripped:
            continue

        if stripped.startswith(version_header) or version_header.startswith(stripped):
            continue

        section_name = _is_section_header(stripped)
        if section_name is not None:
            _finalize_section()
            current_section = section_name
            section_index += 1
            bullet_index_in_section = 0
            section_bullet_texts = []
            section_children = []
            continue

        if stripped.startswith(("http://", "https://")) and " " not in stripped:
            continue

        if _BULLET_PATTERN.match(stripped):
            clean_text = _clean_bullet_text(stripped)

            if len(clean_text) < CHUNK_MIN_TEXT_LENGTH:
                continue

            body = clean_text
        elif len(stripped) > 40:
            body = stripped
        else:
            continue

        prefix = f"{version_header} | {current_section}: "

        for segment in _split_long_text(body, prefix):
            child = PatchChunk(
                chunk_id=f"{gid}-{chunk_index}",
                text=segment,
                patch_version=version_header,
                patch_date=patch_date,
                section=current_section,
                news_type=news_type,
                source_gid=gid,
                source_url=url,
                app_id=app_id,
                image_urls=image_urls,
                bullet_index=bullet_index_in_section,
            )
            chunks.append(child)
            section_children.append(child)
            chunk_index += 1

        section_bullet_texts.append(body)
        bullet_index_in_section += 1

    # Finalize the last section
    _finalize_section()

    logger.debug(f"Chunked '{clean_title}' into {len(chunks)} chunks, {len(parents)} parent sections.")
    return ChunkResult(children=chunks, parents=parents)


def chunk_all_patch_notes(items: list[dict]) -> ChunkResult:
    """
    Chunk a list of news items into individual searchable units.

    Parameters:
        items: List of news item dicts from the Steam News API.

    Returns:
        A ChunkResult with all children and parents across all items.
    """
    all_children: list[PatchChunk] = []
    all_parents: list[ParentChunk] = []
    for item in items:
        result = chunk_patch_note(item)
        all_children.extend(result.children)
        all_parents.extend(result.parents)

    logger.info(
        f"Chunked {len(items)} news items into {len(all_children)} child chunks, "
        f"{len(all_parents)} parent sections."
    )
    return ChunkResult(children=all_children, parents=all_parents)