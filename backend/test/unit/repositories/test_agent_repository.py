from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuxi.repositories.agent_repository import AgentRepository, DEFAULT_AGENT_DESCRIPTION, DEFAULT_SHARE_CONFIG


class FakeDb:
    def __init__(self):
        self.added = None
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    def add(self, item):
        self.added = item


@pytest.mark.asyncio
async def test_ensure_default_agent_creates_description(monkeypatch):
    db = FakeDb()
    repo = AgentRepository(db)

    async def get_by_slug(_slug):
        return None

    monkeypatch.setattr(repo, "get_by_slug", get_by_slug)

    agent = await repo.ensure_default_agent()

    assert agent.description == DEFAULT_AGENT_DESCRIPTION
    assert db.added is agent
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once_with(agent)


@pytest.mark.asyncio
async def test_ensure_default_agent_backfills_missing_description(monkeypatch):
    db = FakeDb()
    repo = AgentRepository(db)
    agent = SimpleNamespace(
        share_config=DEFAULT_SHARE_CONFIG.copy(),
        is_default=True,
        description=None,
        updated_by=None,
        updated_at=None,
    )

    async def get_by_slug(_slug):
        return agent

    monkeypatch.setattr(repo, "get_by_slug", get_by_slug)

    result = await repo.ensure_default_agent(created_by="admin")

    assert result is agent
    assert agent.description == DEFAULT_AGENT_DESCRIPTION
    assert agent.updated_by == "admin"
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once_with(agent)
