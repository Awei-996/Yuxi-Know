from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from yuxi.agents.state import BaseState


class SubAgentRunState(TypedDict, total=False):
    id: str
    subagent_type: str
    subagent_name: str
    child_thread_id: str
    description: str
    status: Literal["completed", "failed"]
    created_at: str
    completed_at: str
    result_preview: str
    error: str | None
    artifacts: list[str]


def merge_subagent_runs(
    existing: list[SubAgentRunState] | None,
    new: list[SubAgentRunState] | None,
) -> list[SubAgentRunState]:
    if existing is None:
        return list(new or [])
    if new is None:
        return existing

    merged = [dict(item) for item in existing]
    index = {item.get("id"): position for position, item in enumerate(merged) if item.get("id")}
    for item in new:
        run = dict(item)
        run_id = run.get("id")
        if run_id and run_id in index:
            merged[index[run_id]] = {**merged[index[run_id]], **run}
        else:
            if run_id:
                index[run_id] = len(merged)
            merged.append(run)
    return merged


class ChatBotState(BaseState):
    subagent_runs: Annotated[list[SubAgentRunState], merge_subagent_runs]
