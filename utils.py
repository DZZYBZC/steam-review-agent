"""
utils.py — Shared utilities for the project.
"""

import json
import os
import frontmatter

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_SKILLS_DIR = os.path.join(_PROJECT_ROOT, "skills")


def load_skill(name: str) -> str:
    """
    Load a skill file by name, stripping YAML frontmatter.

    The skill must live at: skills/{name}/SKILL.md

    Parameters:
        name: The skill directory name (e.g. "classify-review").

    Returns:
        The markdown body of the skill (everything after the frontmatter) that will be used as system prompt
    """
    skill_path = os.path.join(_SKILLS_DIR, name, "SKILL.md")

    if not os.path.exists(skill_path):
        raise FileNotFoundError(
            f"Skill not found: {skill_path}. "
            f"Expected a SKILL.md file in skills/{name}/"
        )

    post = frontmatter.load(skill_path)
    return post.content


def strip_code_fence(text: str) -> str:
    """Strip markdown code fences (```json ... ```) from LLM output."""
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]).strip()
    return text


def parse_llm_json(text: str) -> dict:
    """
    Parse JSON from an LLM response, stripping code fences if present.

    Raises json.JSONDecodeError if the text is not valid JSON.
    """
    text = strip_code_fence(text)
    return json.loads(text)