from __future__ import annotations

import json

from .constants import ORACLE_STATUSES

PROMPT_REVIEW_CHANGES = """Review the current code changes (staged, unstaged, and untracked files) and provide prioritized findings. Try to be comprehensive and precise so as to not require unnecessarily frequent back-and-forths with the developer.

If you find no actionable issues, your final response must be exactly:

NO_FINDINGS

If you do find issues, do not include the string NO_FINDINGS anywhere in your response."""

PROMPT_VALIDATE_FIX_COMMENTS = """Another developer wrote these comments regarding your changes (currently uncommitted in git). Please verify and check the correctness and applicability of their findings. If their concerns are valid and appropriate, please fix and address them.

Reject review comments that are out of scope for the current change, or would cause unrelated scope creep. Do not implement broad refactors, product changes, speculative hardening, or unrelated cleanup merely because a reviewer suggested them. If a review comment is invalid or out of scope, say so clearly and leave the code unchanged for that comment.

If some command fails due to permission issues, retry it with escalation. If harmless, an oracle will approve it."""

PROMPT_REVERIFY_FIXES = """Can you recheck now if all concerns have been successfully fixed, and if any other issues persist or cropped up since?

If all concerns are resolved and you find no actionable issues, your final response must be exactly:

NO_FINDINGS

If you do find issues, do not include the string NO_FINDINGS anywhere in your response.

If remaining issues are not solvable by implementer, it would count as NO_FINDINGS."""

PROMPT_VALIDATE_FOLLOWUP_COMMENTS = """The reviewing developer returned with the following notes. Please verify and check the correctness and applicability of their findings. If their concerns are valid and appropriate, please fix and address them.

Reject review comments that are out of scope for the current change, or would cause unrelated scope creep. Do not implement broad refactors, product changes, speculative hardening, or unrelated cleanup merely because a reviewer suggested them. If a review comment is invalid or out of scope, say so clearly and leave the code unchanged for that comment.

If some command fails due to permission issues, retry it with escalation. If harmless, an oracle will approve it."""

PROMPT_ORACLE_CLASSIFY_REJECTED_FINDINGS = """You are an oracle classifier for an automated code-review workflow.

You are checking whether a fresh reviewer's current findings repeat findings already explicitly rejected by the developer in this persistent oracle conversation.

The latest developer response may or may not contain an explicit rejection. If the latest response and prior oracle conversation do not show an explicit rejection covering the current findings, classify as NO_REJECTED_FINDINGS.

Do not judge whether the developer was correct. Trust the developer. Only classify whether current reviewer findings are clearly covered by an explicit rejection in the latest developer response or prior oracle conversation.

The persistent oracle conversation receives developer responses in chronological order, one response per classification turn. A later response supersedes earlier responses for the same finding. If the developer later accepts, fixes, or otherwise withdraws an earlier rejection for the same issue, do not treat that earlier rejection as covering the current finding.

Return JSON only, with this exact shape:

{
  "explanation": "...",
  "status": "ONLY_REJECTED_FINDINGS | HAS_REJECTED_AND_NEW_FINDINGS | NO_REJECTED_FINDINGS"
}

Rules:
- Use ONLY_REJECTED_FINDINGS when every current finding is clearly covered by explicit developer rejection.
- Use HAS_REJECTED_AND_NEW_FINDINGS when at least one current finding is clearly covered by explicit developer rejection and at least one current finding is not.
- Use NO_REJECTED_FINDINGS when no current finding is clearly covered by explicit developer rejection, or when the latest developer response and prior oracle conversation contain no explicit rejection.
- When responses conflict for the same finding, classify using the latest applicable developer response.
- explanation must always exist and must come before status in the JSON object.
- For ONLY_REJECTED_FINDINGS and HAS_REJECTED_AND_NEW_FINDINGS, explanation must identify which current findings were previously rejected and why.
- For NO_REJECTED_FINDINGS, explanation must briefly explain why no current finding is clearly covered by prior explicit rejection."""

PROMPT_REWRITE_WITHOUT_REJECTED_FINDINGS = """An oracle detected that part of your review repeats findings the developer already explicitly rejected.

Please rewrite the review comments below nearly identically, but remove the rejected finding(s) described below, as if they had never been part of your review. Keep every remaining finding, severity, file reference, and rationale unchanged except for small wording adjustments needed after removal.

The rewritten comments will be sent to the developer.

Do not add new findings.
Do not re-evaluate the code.
Only remove the rejected finding(s).
If removing the rejected finding(s) leaves no remaining actionable findings, your final response must be exactly:

NO_FINDINGS

The rejected finding description and review comments are provided as JSON:

{rewrite_payload_json}"""


def build_reverify_prompt(developer_response: str) -> str:
    return (
        "The developer's response to your comments can be found below.\n\n"
        "<developer_response>\n"
        f"{developer_response}\n"
        "</developer_response>\n\n"
        f"{PROMPT_REVERIFY_FIXES}"
    )


def build_reverify_retry_prompt(reviewer_comments: str, developer_response: str) -> str:
    return (
        "A previous reviewer stream hit a transient tool or router failure during reverification. "
        "You are a fresh reviewer. Re-evaluate the current code changes using the reviewer comments "
        "and the developer response below. Determine whether those concerns are fully resolved and whether "
        "any other actionable issues remain.\n\n"
        "<reviewer_comments>\n"
        f"{reviewer_comments}\n"
        "</reviewer_comments>\n\n"
        "<developer_response>\n"
        f"{developer_response}\n"
        "</developer_response>\n\n"
        f"{PROMPT_REVERIFY_FIXES}"
    )


def build_oracle_prompt(latest_developer_response: str, current_findings: str) -> str:
    payload = json.dumps(
        {
            "latest_developer_response": latest_developer_response,
            "current_reviewer_findings": current_findings,
        },
        ensure_ascii=False,
        indent=2,
    )
    return (
        f"{PROMPT_ORACLE_CLASSIFY_REJECTED_FINDINGS}\n\n"
        "Classify the review findings using this JSON payload:\n\n"
        f"{payload}"
    )


def build_rewrite_without_rejected_prompt(
    review_comments: str,
    rejected_findings_explanation: str,
) -> str:
    payload = json.dumps(
        {
            "rejected_findings": rejected_findings_explanation,
            "review_comments": review_comments,
        },
        ensure_ascii=False,
        indent=2,
    )
    return PROMPT_REWRITE_WITHOUT_REJECTED_FINDINGS.format(
        rewrite_payload_json=payload,
    )
