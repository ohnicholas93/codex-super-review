from __future__ import annotations

import json

from .constants import ORACLE_STATUSES

PROMPT_REVIEW_CHANGES = """Review the current code changes (staged, unstaged, and untracked files) and provide prioritized findings. Infer the intended change scope from the diff and surrounding context, and use it to keep findings focused on issues reasonably connected to the change. Avoid drifting into highly unrelated pre-existing issues, broad refactors, or excessive  speculative hardening. Try to be comprehensive and precise so as to not require unnecessarily frequent back-and-forths with the developer.

If you find no actionable issues, your final response must be exactly:

NO_FINDINGS

If you do find issues, do not include the string NO_FINDINGS anywhere in your response."""

PROMPT_REVIEW_BRANCH = """Review the currently checked out branch against the pinned base commit {base_commit} from {base_branch} and provide prioritized findings. Use the merge-base comparison from {merge_base} to the current working tree. This includes the committed branch diff equivalent to `git diff {base_commit}...HEAD`, plus staged, unstaged, and untracked repair edits created during this run. Infer the intended change scope from the diff and surrounding context, and use it to keep findings focused on issues reasonably connected to the branch. Avoid drifting into highly unrelated pre-existing issues, broad refactors, or excessive speculative hardening.

Do not change HEAD during this review. Do not create commits, amend commits, rebase, merge, reset HEAD, or check out another branch.

If you find no actionable issues, your final response must be exactly:

NO_FINDINGS

If you do find issues, do not include the string NO_FINDINGS anywhere in your response."""

PROMPT_VALIDATE_FIX_COMMENTS = """Another developer wrote these comments about the currently uncommitted changes in git. Please verify and check the correctness and applicability of their findings. If their concerns are valid and appropriate, please fix and address them.

Reject review comments that are out of scope for the current change, or would cause unrelated scope creep. Do not implement broad refactors, product changes, speculative hardening, or unrelated cleanup merely because a reviewer suggested them. If a review comment is invalid or out of scope, say so clearly and leave the code unchanged for that comment.

If some command fails due to permission issues, retry it with escalation. If harmless, an oracle will approve it."""

PROMPT_VALIDATE_BRANCH_FIX_COMMENTS = """Another developer wrote these comments regarding your currently checked out branch compared against the pinned base commit {base_commit} from {base_branch}. The intended review scope is the merge-base comparison from {merge_base} to the current working tree. This includes the committed branch diff equivalent to `git diff {base_commit}...HEAD`, plus staged, unstaged, and untracked repair edits created during this run. Please verify and check the correctness and applicability of their findings. If their concerns are valid and appropriate, please fix and address them.

Do not change HEAD while fixing these comments. Do not create commits, amend commits, rebase, merge, reset HEAD, or check out another branch. Apply fixes as staged, unstaged, or untracked working-tree edits only.

Reject review comments that are out of scope for the branch diff against {base_branch}, or would cause unrelated scope creep. Do not implement broad refactors, product changes, speculative hardening, or unrelated cleanup merely because a reviewer suggested them. If a review comment is invalid or out of scope, say so clearly and leave the code unchanged for that comment.

If some command fails due to permission issues, retry it with escalation. If harmless, an oracle will approve it."""

PROMPT_REVERIFY_FIXES = """Can you recheck now if all concerns have been successfully fixed, and if any other issues persist or cropped up since? Keep the intended change scope in mind and avoid drifting into unrelated pre-existing issues, broad refactors, or speculative hardening.

If all concerns are resolved and you find no actionable issues, your final response must be exactly:

NO_FINDINGS

If you do find issues, do not include the string NO_FINDINGS anywhere in your response.

If remaining issues are not solvable by implementer, it would count as NO_FINDINGS."""

PROMPT_VALIDATE_FOLLOWUP_COMMENTS = """The reviewing developer returned with the following notes. Please verify and check the correctness and applicability of their findings. If their concerns are valid and appropriate, please fix and address them.

Reject review comments that are out of scope for the current change, or would cause unrelated scope creep. Do not implement broad refactors, product changes, speculative hardening, or unrelated cleanup merely because a reviewer suggested them. If a review comment is invalid or out of scope, say so clearly and leave the code unchanged for that comment.

If some command fails due to permission issues, retry it with escalation. If harmless, an oracle will approve it."""

PROMPT_VALIDATE_BRANCH_FOLLOWUP_COMMENTS = """The reviewing developer returned with the following notes about your currently checked out branch compared against the pinned base commit {base_commit} from {base_branch}. The intended review scope is the merge-base comparison from {merge_base} to the current working tree. This includes the committed branch diff equivalent to `git diff {base_commit}...HEAD`, plus staged, unstaged, and untracked repair edits created during this run. Please verify and check the correctness and applicability of their findings. If their concerns are valid and appropriate, please fix and address them.

Do not change HEAD while fixing these comments. Do not create commits, amend commits, rebase, merge, reset HEAD, or check out another branch. Apply fixes as staged, unstaged, or untracked working-tree edits only.

Reject review comments that are out of scope for the branch diff against {base_branch}, or would cause unrelated scope creep. Do not implement broad refactors, product changes, speculative hardening, or unrelated cleanup merely because a reviewer suggested them. If a review comment is invalid or out of scope, say so clearly and leave the code unchanged for that comment.

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


def build_branch_reverify_prompt(
    developer_response: str,
    *,
    base_branch: str,
    base_commit: str,
    merge_base: str,
) -> str:
    review_scope = (
        f"Reverify against the pinned base commit {base_commit} from {base_branch}. "
        f"Use the merge-base comparison from {merge_base} to the current "
        "working tree. This includes the committed branch diff equivalent to "
        f"`git diff {base_commit}...HEAD`, plus staged, unstaged, and untracked "
        "repair edits created during this run. Do not change HEAD during "
        "reverification."
    )
    return (
        "The developer's response to your comments can be found below.\n\n"
        "<developer_response>\n"
        f"{developer_response}\n"
        "</developer_response>\n\n"
        f"{review_scope}\n\n"
        f"{PROMPT_REVERIFY_FIXES}"
    )


def format_prompt(
    template: str,
    *,
    branch_base: str | None = None,
    branch_base_commit: str | None = None,
    merge_base: str | None = None,
) -> str:
    if (
        "{base_branch}" not in template
        and "{base_commit}" not in template
        and "{merge_base}" not in template
    ):
        return template
    if branch_base is None:
        raise ValueError("branch_base is required for branch review prompt")
    if branch_base_commit is None:
        raise ValueError("branch_base_commit is required for branch review prompt")
    if merge_base is None:
        raise ValueError("merge_base is required for branch review prompt")
    return template.format(
        base_branch=branch_base,
        base_commit=branch_base_commit,
        merge_base=merge_base,
    )


def build_reverify_retry_prompt(
    reviewer_comments: str,
    developer_response: str,
    *,
    branch_base: str | None = None,
    branch_base_commit: str | None = None,
    merge_base: str | None = None,
) -> str:
    if branch_base is not None and branch_base_commit is None:
        raise ValueError("branch_base_commit is required for branch review prompt")
    if branch_base is not None and merge_base is None:
        raise ValueError("merge_base is required for branch review prompt")
    review_scope = (
        f"the pinned base commit {branch_base_commit} from {branch_base}, using the merge-base comparison from {merge_base} to the current working tree, including the committed branch diff equivalent to `git diff {branch_base_commit}...HEAD` plus staged, unstaged, and untracked repair edits created during this run. Do not change HEAD"
        if branch_base is not None
        else "the current code changes"
    )
    return (
        "A previous reviewer stream hit a transient tool or router failure during reverification. "
        f"You are a fresh reviewer. Re-evaluate {review_scope}. Use the reviewer comments "
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
