from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChildThreadId:
    parent_thread_id: str
    agent_slug: str
    suffix: str


def safe_subagent_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-") or "subagent"


def make_child_thread_id(parent_thread_id: str, agent_slug: str, tool_call_id: str) -> str:
    suffix = hashlib.sha256(
        f"{parent_thread_id}:{agent_slug}:{tool_call_id}".encode("utf-8")
    ).hexdigest()[:8]
    return f"{parent_thread_id}_sub_{safe_subagent_slug(agent_slug)}_{suffix}"


def parse_child_thread_id(thread_id: str) -> ChildThreadId | None:
    parent_thread_id, separator, child_suffix = str(thread_id or "").rpartition("_sub_")
    if not separator or not parent_thread_id:
        return None

    agent_slug, separator, suffix = child_suffix.rpartition("_")
    if not separator or not agent_slug or len(suffix) != 8:
        return None
    if any(char not in "0123456789abcdef" for char in suffix.lower()):
        return None
    return ChildThreadId(parent_thread_id=parent_thread_id, agent_slug=agent_slug, suffix=suffix)
