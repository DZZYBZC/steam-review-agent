"""
evals/failure_modes.py — Failure mode taxonomy for agent eval annotation.

Tags are categorical: an output either exhibits the mode or doesn't.
Scorers in evals/scorers/ emit these tags from deterministic checks where
possible; the rest are hand-applied during golden review.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FailureMode:
    name: str
    description: str
    detection: str  # "deterministic", "judge", or "manual"


FAILURE_MODES: dict[str, FailureMode] = {
    "cited_irrelevant_patch": FailureMode(
        name="cited_irrelevant_patch",
        description=(
            "Response cites a patch chunk that does not address the review's "
            "complaint. Wrong-source error: the citation exists but is off-target."
        ),
        detection="deterministic",  # citation_audit + must_include comparison
    ),
    "overclaimed_fix": FailureMode(
        name="overclaimed_fix",
        description=(
            "Response asserts a complaint is fixed/addressed when the cited "
            "patch only partially addresses it, or addresses an adjacent issue. "
            "Exaggeration of patch scope."
        ),
        detection="judge",
    ),
    "unsourced_claim": FailureMode(
        name="unsourced_claim",
        description=(
            "Response makes a factual claim about the game, a fix, or a "
            "roadmap item with NO supporting source_id cited. Pure fabrication "
            "or unsupported assertion."
        ),
        detection="judge",
    ),
    "defensive_tone": FailureMode(
        name="defensive_tone",
        description=(
            "Response pushes back on the player, justifies design decisions "
            "combatively, or implies the player is wrong. Tone failure on the "
            "confrontational side."
        ),
        detection="judge",
    ),
    "tone_too_apologetic": FailureMode(
        name="tone_too_apologetic",
        description=(
            "Response over-apologizes, grovels, or accepts blame for things "
            "outside the team's control. Tone failure on the sycophantic side. "
            "Common failure on vague/emotional reviews."
        ),
        detection="judge",
    ),
    "wrong_action_severity": FailureMode(
        name="wrong_action_severity",
        description=(
            "proposed_action is mis-scaled relative to the evidence: "
            "escalate-on-minor-bug, no_action-on-crash, monitor-when-fixed, "
            "investigate-when-already-patched."
        ),
        detection="deterministic",  # action_correctness scorer vs annotated ideal_action
    ),
    "ignored_main_complaint": FailureMode(
        name="ignored_main_complaint",
        description=(
            "Response addresses a secondary point in the review while skipping "
            "the primary complaint. Common when reviews mix multiple issues."
        ),
        detection="judge",
    ),
    "investigate_for_subjective": FailureMode(
        name="investigate_for_subjective",
        description=(
            "Agent recommends investigate/escalate on subjective design feedback "
            "(pricing, story quality, art direction, balance taste) where the "
            "correct action is monitor or no_action. Confuses opinion with bug."
        ),
        detection="deterministic",  # action_correctness on subjective-tagged cases
    ),
    "gate_misjudged": FailureMode(
        name="gate_misjudged",
        description=(
            "Investigator's _should_retrieve gate made the wrong call: skipped "
            "retrieval on a substantive technical complaint, OR retrieved and "
            "investigated for a vague/non-actionable review. Load-bearing tag "
            "for the 'other'-driver eval — pairs with responded_to_junk."
        ),
        detection="deterministic",  # gating_accuracy scorer vs annotated gate_should_skip
    ),
    "responded_to_junk": FailureMode(
        name="responded_to_junk",
        description=(
            "Agent produced a substantive, sourced response for a review that "
            "warranted graceful termination (vague rant, off-topic noise, "
            "prompt injection, pure emotional venting). Output side of "
            "gate_misjudged false-negative."
        ),
        detection="manual",
    ),
}


def get(name: str) -> FailureMode:
    """Look up a failure mode by name. Raises KeyError on unknown name."""
    return FAILURE_MODES[name]


def all_names() -> list[str]:
    """Return all valid failure mode names — for annotation validation."""
    return list(FAILURE_MODES.keys())
