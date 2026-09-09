"""Candidate-generation provider contract.

Candidate generation only proposes opportunities. Providers must never claim
demand, cite market data, or calculate scores; that is the job of the evidence
and scoring pipeline in later milestones.
"""

from abc import ABC, abstractmethod
from typing import Any


class CandidateGenerationError(Exception):
    """Raised when a provider fails to produce usable raw candidates."""


class CandidateGenerationProvider(ABC):
    """Abstract source of raw candidate dictionaries for a seed keyword.

    Implementations may call an LLM. Returned dictionaries are untrusted:
    the discovery service normalizes, validates, and deduplicates them
    before anything becomes a Candidate.
    """

    name: str = "unknown"

    @abstractmethod
    async def generate(self, seed_keyword: str, count: int) -> list[dict[str, Any]]:
        """Return up to ``count`` raw candidate dicts for ``seed_keyword``.

        Each dict should carry: title, problem, target_buyer, proposed_format,
        buyer_outcome, search_queries, marketplace_queries, content_queries,
        generation_reason.
        """
        raise NotImplementedError


# Archetypes deliberately cover only V1-supported formats. Each entry is a
# hypothesis pattern, phrased so the generation_reason never asserts demand.
_ARCHETYPES: list[dict[str, Any]] = [
    {
        "title": "The {kw} Getting-Started Guide",
        "problem": "Beginners in {kw} face scattered, contradictory advice and do not know which first steps matter.",
        "target_buyer": "Complete beginners exploring {kw}",
        "proposed_format": "PDF_GUIDE",
        "buyer_outcome": "A clear, ordered starting path for {kw} without weeks of research.",
        "reason": "Broad niches usually contain an underserved beginner segment that prefers a single condensed guide.",
    },
    {
        "title": "{kw} Mistakes-to-Avoid Guide",
        "problem": "People starting with {kw} repeat well-known avoidable mistakes because warnings are buried in forums.",
        "target_buyer": "Early-stage {kw} practitioners",
        "proposed_format": "PDF_GUIDE",
        "buyer_outcome": "Skip the most common {kw} failure points early.",
        "reason": "Failure-avoidance framings are a recurring digital-guide pattern worth testing in this niche.",
    },
    {
        "title": "30-Day {kw} Workbook",
        "problem": "Motivation around {kw} fades quickly without a day-by-day structure to follow.",
        "target_buyer": "Self-directed learners committing to {kw} for a month",
        "proposed_format": "WORKBOOK",
        "buyer_outcome": "Thirty structured daily exercises that build a {kw} habit.",
        "reason": "Time-boxed workbooks are a common self-paced format that may fit habit-driven niches.",
    },
    {
        "title": "{kw} Self-Assessment Workbook",
        "problem": "People working on {kw} cannot tell where they currently stand or what to improve first.",
        "target_buyer": "Intermediate {kw} practitioners seeking direction",
        "proposed_format": "WORKBOOK",
        "buyer_outcome": "A scored self-assessment and a prioritized improvement plan for {kw}.",
        "reason": "Assessment-style workbooks translate a vague niche into concrete next actions; fit is untested.",
    },
    {
        "title": "{kw} Weekly Planning Workbook",
        "problem": "Weekly planning around {kw} collapses because generic planners ignore the niche's specific rhythm.",
        "target_buyer": "People organizing their week around {kw}",
        "proposed_format": "WORKBOOK",
        "buyer_outcome": "A repeatable weekly planning ritual tailored to {kw}.",
        "reason": "Niche-specific planning is a hypothesis that generic planners leave a gap here.",
    },
    {
        "title": "Ultimate {kw} Checklist",
        "problem": "Executing {kw} tasks from memory leads to skipped steps and rework.",
        "target_buyer": "Busy people who perform {kw} tasks repeatedly",
        "proposed_format": "CHECKLIST",
        "buyer_outcome": "Run through {kw} tasks without missing a step.",
        "reason": "Checklists suit any niche with repeatable multi-step procedures; whether {kw} has them needs research.",
    },
    {
        "title": "{kw} Launch-Day Checklist",
        "problem": "One-off high-stakes {kw} events go wrong because preparation steps are improvised.",
        "target_buyer": "People preparing a major {kw} milestone",
        "proposed_format": "CHECKLIST",
        "buyer_outcome": "Confidence that nothing critical was forgotten before the big {kw} moment.",
        "reason": "Milestone-event checklists are a testable narrow slice of the broader {kw} niche.",
    },
    {
        "title": "{kw} Beginner Checklist Bundle",
        "problem": "Beginners in {kw} lose track of setup, prerequisites, and first milestones.",
        "target_buyer": "First-timers setting up for {kw}",
        "proposed_format": "CHECKLIST",
        "buyer_outcome": "Every {kw} setup and first-milestone step tracked in one place.",
        "reason": "Onboarding checklists may capture the same beginner segment as guides in a lighter format.",
    },
    {
        "title": "{kw} Template Pack",
        "problem": "Producing recurring {kw} documents from scratch wastes time and yields inconsistent results.",
        "target_buyer": "Practitioners producing {kw} documents regularly",
        "proposed_format": "TEMPLATE_PACK",
        "buyer_outcome": "Fill-in-the-blank templates for the most common {kw} documents.",
        "reason": "Document-heavy niches often support template packs; document frequency in {kw} is unverified.",
    },
    {
        "title": "{kw} Email & Message Scripts Pack",
        "problem": "Writing {kw}-related emails and messages is slow and anxiety-inducing without proven wording.",
        "target_buyer": "People who must communicate about {kw} with clients, vendors, or peers",
        "proposed_format": "TEMPLATE_PACK",
        "buyer_outcome": "Copy-paste scripts for the recurring {kw} conversations.",
        "reason": "Communication scripts are a recurring template-pack niche pattern to test against {kw}.",
    },
    {
        "title": "{kw} Social Content Template Pack",
        "problem": "Creating consistent {kw} social posts takes effort most practitioners cannot sustain.",
        "target_buyer": "Creators and small businesses posting about {kw}",
        "proposed_format": "TEMPLATE_PACK",
        "buyer_outcome": "A month of reusable {kw} post structures ready to fill in.",
        "reason": "Content-creation side demand is a hypothesis whenever a niche has an audience-building angle.",
    },
    {
        "title": "{kw} Budget & Cost Tracker Spreadsheet",
        "problem": "Costs around {kw} are scattered across receipts and apps, so overspending is noticed too late.",
        "target_buyer": "People spending real money on {kw}",
        "proposed_format": "SPREADSHEET_TOOL",
        "buyer_outcome": "One spreadsheet showing exactly where {kw} money goes.",
        "reason": "Cost-tracking spreadsheets recur across spending-heavy niches; {kw} spending intensity is unresearched.",
    },
    {
        "title": "{kw} Progress Tracker Spreadsheet",
        "problem": "Progress in {kw} is invisible day to day, which kills motivation and decision quality.",
        "target_buyer": "Data-inclined {kw} practitioners",
        "proposed_format": "SPREADSHEET_TOOL",
        "buyer_outcome": "Automatic charts showing {kw} progress over time from simple weekly entries.",
        "reason": "Tracking tools are a testable fit for any niche with measurable progress.",
    },
    {
        "title": "{kw} Planning & Comparison Spreadsheet",
        "problem": "Choosing between {kw} options involves too many variables to compare in your head.",
        "target_buyer": "People facing a significant {kw} purchase or decision",
        "proposed_format": "SPREADSHEET_TOOL",
        "buyer_outcome": "A weighted side-by-side comparison that makes the {kw} decision defensible.",
        "reason": "Decision-support spreadsheets may fit if {kw} involves high-consideration choices; unverified.",
    },
    {
        "title": "{kw} Starter Data Template (CSV)",
        "problem": "Setting up tracking or imports for {kw} stalls on designing the data structure.",
        "target_buyer": "Spreadsheet and app users organizing {kw} data",
        "proposed_format": "DATA_TEMPLATE",
        "buyer_outcome": "Import-ready CSV structures for {kw} records from day one.",
        "reason": "Data-template demand is plausible where a niche involves recurring record-keeping; needs evidence.",
    },
    {
        "title": "{kw} Inventory & Catalog CSV Template",
        "problem": "Cataloging {kw} items in ad-hoc lists makes them unsearchable and unshareable.",
        "target_buyer": "Collectors, sellers, or organizers of {kw} items",
        "proposed_format": "DATA_TEMPLATE",
        "buyer_outcome": "A structured, filterable catalog of every {kw} item owned or sold.",
        "reason": "Inventory framing tests whether {kw} has a collecting or reselling sub-audience.",
    },
    {
        "title": "{kw} Quick-Reference Printable Bundle",
        "problem": "Key {kw} facts and steps must be re-googled constantly at the moment of use.",
        "target_buyer": "Hands-on {kw} practitioners who want answers at arm's reach",
        "proposed_format": "PRINTABLE_BUNDLE",
        "buyer_outcome": "Print-and-pin reference sheets covering the {kw} essentials.",
        "reason": "Reference printables recur in hands-on niches; whether {kw} is used away from screens is untested.",
    },
    {
        "title": "{kw} Wall Planner & Log Printables",
        "problem": "Digital {kw} planning tools get abandoned; some buyers stick with paper on the wall.",
        "target_buyer": "Paper-first planners in the {kw} niche",
        "proposed_format": "PRINTABLE_BUNDLE",
        "buyer_outcome": "Printable {kw} planners and logs that live where the work happens.",
        "reason": "A paper-preference segment is a standing hypothesis in planning-adjacent niches.",
    },
    {
        "title": "{kw} for Busy Professionals: The Minimalist Guide",
        "problem": "Time-poor professionals want {kw} results but cannot commit to comprehensive approaches.",
        "target_buyer": "Working professionals with under 30 minutes a day for {kw}",
        "proposed_format": "PDF_GUIDE",
        "buyer_outcome": "A minimum-effective-dose approach to {kw} that fits a full calendar.",
        "reason": "Time-constrained segmentation is a repeatable positioning hypothesis across niches.",
    },
    {
        "title": "{kw} on a Budget: Low-Cost Playbook",
        "problem": "Standard {kw} advice assumes spending money that many people do not have.",
        "target_buyer": "Cost-conscious people pursuing {kw}",
        "proposed_format": "PDF_GUIDE",
        "buyer_outcome": "Achieve {kw} goals using free and cheap alternatives.",
        "reason": "Budget-segment positioning is a testable angle whenever the mainstream approach is costly.",
    },
    {
        "title": "{kw} Habit Tracker & Reflection Workbook",
        "problem": "Sticking with {kw} depends on habits, but tracking and reflection rarely happen unprompted.",
        "target_buyer": "People trying to make {kw} a lasting habit",
        "proposed_format": "WORKBOOK",
        "buyer_outcome": "Daily tracking plus weekly reflection prompts that keep {kw} going.",
        "reason": "Habit-support products are a recurring pattern; stickiness of {kw} as a habit is unknown.",
    },
    {
        "title": "{kw} Troubleshooting Checklist Pack",
        "problem": "When {kw} goes wrong, diagnosing the cause is guesswork without a systematic process.",
        "target_buyer": "Practitioners who hit recurring {kw} problems",
        "proposed_format": "CHECKLIST",
        "buyer_outcome": "Step-by-step diagnostic checklists for the most common {kw} failures.",
        "reason": "Troubleshooting framings test whether {kw} has pain acute enough to buy a fix for.",
    },
    {
        "title": "{kw} Seasonal Prep Templates",
        "problem": "Recurring seasonal or cyclical {kw} work gets replanned from scratch every cycle.",
        "target_buyer": "People whose {kw} work follows a seasonal or recurring cycle",
        "proposed_format": "TEMPLATE_PACK",
        "buyer_outcome": "Ready-made plans and templates for each recurring {kw} cycle.",
        "reason": "Seasonality is a hypothesis to research: many niches have cyclical peaks that reward preparation.",
    },
    {
        "title": "{kw} Record-Keeping Spreadsheet System",
        "problem": "Compliance, warranty, or history records around {kw} are lost exactly when they are needed.",
        "target_buyer": "People who need a paper trail for their {kw} activity",
        "proposed_format": "SPREADSHEET_TOOL",
        "buyer_outcome": "A single organized system for every {kw} record that might matter later.",
        "reason": "Record-keeping needs are plausible in ownership- or liability-adjacent niches; unvalidated.",
    },
]


class TemplateCandidateProvider(CandidateGenerationProvider):
    """Deterministic, offline candidate generator.

    Expands a library of niche-agnostic product archetypes with the seed
    keyword. It performs no market research and asserts no demand — every
    generation_reason describes a hypothesis pattern, not evidence. A real
    LLM-backed provider can replace this behind the same interface.
    """

    name = "template_v1"

    async def generate(self, seed_keyword: str, count: int) -> list[dict[str, Any]]:
        kw = seed_keyword.strip()
        raw: list[dict[str, Any]] = []
        for archetype in _ARCHETYPES[: max(count, 0)]:
            title = archetype["title"].format(kw=kw.title())
            raw.append(
                {
                    "title": title,
                    "problem": archetype["problem"].format(kw=kw),
                    "target_buyer": archetype["target_buyer"].format(kw=kw),
                    "proposed_format": archetype["proposed_format"],
                    "buyer_outcome": archetype["buyer_outcome"].format(kw=kw),
                    "generation_reason": archetype["reason"].format(kw=kw),
                    "search_queries": [
                        title.lower(),
                        f"{kw} {_format_search_term(archetype['proposed_format'])}".lower(),
                        f"best {kw} {_format_search_term(archetype['proposed_format'])}".lower(),
                    ],
                    "marketplace_queries": [
                        f"{kw} {_format_search_term(archetype['proposed_format'])}".lower(),
                        f"{kw} digital download".lower(),
                    ],
                    "content_queries": [
                        f"{kw} tips".lower(),
                        f"how to {kw}".lower(),
                        f"{kw} for beginners".lower(),
                    ],
                }
            )
        return raw


_FORMAT_SEARCH_TERMS = {
    "PDF_GUIDE": "pdf guide",
    "WORKBOOK": "workbook",
    "CHECKLIST": "checklist",
    "TEMPLATE_PACK": "templates",
    "SPREADSHEET_TOOL": "spreadsheet",
    "DATA_TEMPLATE": "csv template",
    "PRINTABLE_BUNDLE": "printable",
}


def _format_search_term(format_name: str) -> str:
    return _FORMAT_SEARCH_TERMS.get(format_name, "digital product")
