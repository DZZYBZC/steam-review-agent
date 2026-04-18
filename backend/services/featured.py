"""
Hand-curated list of replay cases for the demo.

Each entry was picked by inspection from backend.scripts.pick_featured output
and represents one distinct story along the agent's behavioral surface.

A `demo_override` field is available for presenting the run with a different
terminal decision than what the DB recorded. This is used for the rejection
case: the agent was auto-approved during eval, but under real human review
the rubric-wrong action would have been rejected — the override represents
that counterfactual. DB data is unchanged; only replay presentation differs.
"""

from typing import Literal, NotRequired, TypedDict


StartPanel = Literal["timeline", "why_action", "draft", "cited_evidence", "outcome"]


class DemoOverride(TypedDict):
    human_decision: Literal["approved", "rejected"]
    human_feedback: NotRequired[str]
    rationale: NotRequired[str]


class FeaturedCase(TypedDict):
    run_id: str
    label: str
    story: str
    start_panel: StartPanel
    talk_track: str
    demo_override: NotRequired[DemoOverride]


class RecommendedLivePick(TypedDict):
    review_id: str
    app_id: str
    label: str
    story: str
    talk_track: str


FEATURED_CASES: list[FeaturedCase] = [
    {
        "run_id": "3ba7476e-eaca-4223-8f67-22e33249b89e",
        "label": "Clean Approval",
        "story": "Critic approved the first draft; rubric-correct investigate call on a broken-feature report.",
        "start_panel": "outcome",
        "talk_track": (
            "Review (PAYDAY 3): 'It should have an offline mode that works properly, not one that "
            "tries to connect to the internet.' Investigator pulled the October 2 Vision Stream "
            "recap (ev.conf=0.9) — the studio's public statement that offline mode 'proved not a "
            "feasible route.' Responder correctly called investigate, not no_action: the studio's "
            "decision explains WHY offline doesn't work but doesn't fix the shipped-broken feature "
            "the reviewer is hitting. Draft acknowledges the tradeoff honestly without pretending "
            "the issue is resolved. Critic approved on iter 0. Golden set agrees (case_id "
            "payday3_tech_002, ideal_action=investigate). This is the baseline happy path — full "
            "loop in one pass with a nuanced rubric call the responder got right."
        ),
    },
    {
        "run_id": "f34fb28d-9be1-438c-9e69-2526fa7899aa",
        "label": "Rejection → Revision",
        "story": "Drafting rejection on iter 0, action rejection on iter 1, approved on iter 2.",
        "start_panel": "why_action",
        "talk_track": (
            "Review: endgame armor feels too weak against elemental damage. Iter 0 draft failed the "
            "overconfidence check — the evidence covered offensive elemental changes (Ver.1.040) but "
            "not defensive armor tuning, and the draft over-claimed. Iter 1 fixed the hedging but the "
            "critic still disagreed on the action label. Iter 2 landed. Show the reason_type progression "
            "(drafting → action → approved) in the timeline — this is what revision-loop observability "
            "looks like."
        ),
    },
    {
        "run_id": "15fe232b-9756-4441-bd72-bd4db209f05a",
        "label": "Action Override / Freeze",
        "story": "Critic over-applied monitor; action-freeze bypass preserved the responder's correct no_action call.",
        "start_panel": "outcome",
        "talk_track": (
            "Review: three words — 'Tedious, bad maps. convoluted.' Responder correctly chose "
            "no_action: evidence shows Firaxis is already reworking maps (Voronoi types, Update "
            "1.1.1, 1.3.1, edge-panning restoration) so no new follow-up is warranted — the "
            "'already-addressed by shipped patches' path in the rubric. The critic kept rejecting "
            "in favor of monitor — a known critic over-application failure mode. By iter 2 only "
            "the action label was in dispute, so action-freeze kicked in: coordinator stopped the "
            "loop and routed straight to human gate. Human approved the responder's call. "
            "Golden set agrees (case_id civ7_vague_001, ideal_action=no_action). This shows the "
            "system handling critic over-correction gracefully instead of infinite re-drafting."
        ),
    },
    {
        "run_id": "d045613b-e1e7-4400-b15e-5f59cd34d5f4",
        "label": "Escalate — Hard Blocker",
        "story": "Hundreds of crashes in 40h; responder correctly escalates with concrete next step.",
        "start_panel": "why_action",
        "talk_track": (
            "Review: 'I have 40 hours in and literally hundreds of crashes in that time. Game is "
            "literally broken.' Classic hard-blocker per the rubric — explicit persistence + "
            "concrete scenario. Investigator found strong evidence (ev.conf=0.72): four separate "
            "stability patches (1.0.1, Feb 2025, 1.2.0 P1, 1.2.1). Responder credits the shipped "
            "work, flags that the reviewer's frequency exceeds what those fixes should leave "
            "behind, and gives a concrete next step — file a support ticket with crash logs. "
            "Critic approved on iter 0. Golden set agrees (case_id civ7_tech_001, "
            "ideal_action=escalate). This is what a good escalate response looks like: not just "
            "'we hear you,' but a specific path forward for a persistent single-user blocker."
        ),
    },
    {
        "run_id": "fc3d2340-df46-4d86-a496-e95c6c291b13",
        "label": "Insufficient Evidence",
        "story": "Subjective narrative complaint with no patch evidence; responder hedges, critic approves.",
        "start_panel": "cited_evidence",
        "talk_track": (
            "Review: 'The opposite of show, don't tell — everything interesting happened before the "
            "game.' Pure narrative design critique, no bug to fix. Investigator returned no direct "
            "evidence (confidence=0.0). Responder deliberately did not fabricate a fix — acknowledged "
            "the criticism, named it as a design choice, landed at no_action. Critic approved on "
            "iter 0. This is the graceful-failure path: the system refuses to hallucinate a response "
            "when there's nothing to respond with."
        ),
    },
    {
        "run_id": "63f0648b-fbb2-442b-8366-ccb7999c2168",
        "label": "Human Rejection — Under-Escalated Miss",
        "story": "Responder under-called to monitor; action-freeze hid the miss; under real human review this would be rejected.",
        "start_panel": "why_action",
        "talk_track": (
            "MH Wilds performance review: '900 hours invested... February patch completely wrecked "
            "the game... stuttery, unplayable mess... over a month no more patches.' Matches golden "
            "case mhw_perf_001 (ideal_action=escalate) via rubric path (b): concrete hard-blocker + "
            "explicit persistence + post-patch regression. Watch the chain of misses: (1) responder "
            "under-called to monitor, (2) critic correctly flagged BOTH overconfidence and action "
            "mismatch on iter 0, (3) action-freeze bypass fired because reason_type='action' was "
            "the primary concern — skipping the drafting fix entirely, (4) frozen monitor routed "
            "straight to human gate. In the DB this was auto-approved during eval. Under real "
            "human review this is a clear reject: 'rubric path (b), should be escalate.' That's "
            "what this replay presents — the counterfactual. The point: the gate is a real arbiter "
            "AND the eval framework (golden case mhw_perf_001) catches misses even when the gate "
            "is automated. Both safety nets matter, and neither is a rubber stamp."
        ),
        "demo_override": {
            "human_decision": "rejected",
            "human_feedback": (
                "Disagreeing on the action here. 900 hours in and the game's been unplayable "
                "since the Feb patch, with no fix in over a month — that's a hard blocker for "
                "an engaged player, not something to just monitor. Escalate is the right call. "
                "Please re-draft."
            ),
            "rationale": (
                "Golden case mhw_perf_001 labels this escalate. Historical run auto-approved the "
                "responder's monitor call; this override demonstrates what a real human review "
                "would do at the gate."
            ),
        },
    },
]


# Curated reviews that are always pinned to the top of the Try Live sidebar.
# These are the demo-critical picks: the interviewer can click any of them
# with confidence they'll produce the intended story. The algorithmic
# candidate list follows, and interviewers can still roam freely from there.
RECOMMENDED_LIVE_PICKS: list[RecommendedLivePick] = [
    {
        "review_id": "222054931",
        "app_id": "2246340",
        "label": "Rejection demo — MHW Feb-perf regression",
        "story": "Agent will likely under-call to monitor. Reject at the gate → revision loop → re-draft toward escalate.",
        "talk_track": (
            "MH Wilds, 900+ hours: 'February performance patch completely wrecked the game... "
            "stuttery, unplayable mess... over a month no more patches.' Matches golden case "
            "mhw_perf_001 (ideal_action=escalate) via rubric path (b). Historically the agent "
            "under-calls to monitor + action-freeze hides the miss. Use the gate's Reject "
            "button with feedback like: 'Disagreeing on action — 900h player, post-patch "
            "regression, persistence → should be escalate.' Watch the revision loop re-draft "
            "to escalate. This is the star moment: the gate is a real arbiter."
        ),
    },
    {
        "review_id": "221602720",
        "app_id": "1295660",
        "label": "Escalate demo — Civ 7 'hundreds of crashes'",
        "story": "Clean hard-blocker. Agent should route to escalate on iter 0. Approve at the gate.",
        "talk_track": (
            "Civ 7, 40-hour player: 'literally hundreds of crashes.' Textbook escalate per rubric "
            "path (b): concrete hard-blocker + explicit persistence. Evidence retrieval finds "
            "the stability patches (1.0.1, Feb 2025, 1.2.0 P1, 1.2.1). Responder credits the "
            "shipped work + gives a concrete next step (support ticket with crash logs). Use "
            "this as the happy-path live demo before driving the rejection path above."
        ),
    },
    {
        "review_id": "221766197",
        "app_id": "1716740",
        "label": "Graceful failure — Starfield 'show don't tell'",
        "story": "Pure narrative design critique, no patch evidence. Agent hedges at no_action.",
        "talk_track": (
            "Starfield: 'Everything that was interesting takes place before the events of the "
            "game.' Pure narrative design critique with no bug to fix. Investigator returns "
            "low/zero confidence. Responder refuses to fabricate a fix — acknowledges the "
            "criticism, names it as a design choice, lands at no_action. Use this live when "
            "someone asks 'what does the agent do when there's nothing to respond with?'"
        ),
    },
]

