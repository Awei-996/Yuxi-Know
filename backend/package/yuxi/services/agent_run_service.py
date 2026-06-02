"""Agent run service (run creation, polling stream, cancel)."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.agents.buildin import agent_manager
from yuxi.repositories.agent_repository import AgentRepository
from yuxi.repositories.agent_run_repository import TERMINAL_RUN_STATUSES, AgentRunRepository
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.services.run_queue_service import (
    build_run_event_envelope,
    get_arq_pool,
    get_last_run_stream_seq,
    list_run_stream_events,
    normalize_after_seq,
    publish_cancel_signal,
)
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Message, User
from yuxi.utils.datetime_utils import utc_now_naive
from yuxi.utils.logging_config import logger

SSE_HEARTBEAT_SECONDS = int(os.getenv("RUN_SSE_HEARTBEAT_SECONDS", "15"))
SSE_MAX_CONNECTION_MINUTES = int(os.getenv("RUN_SSE_MAX_CONNECTION_MINUTES", "30"))
SSE_POLL_INTERVAL_SECONDS = float(os.getenv("RUN_SSE_POLL_INTERVAL_SECONDS", "1.0"))


def _build_run_response(run) -> dict:
    return {
        "run_id": run.id,
        "thread_id": run.thread_id,
        "status": run.status,
        "request_id": run.request_id,
        "stream_url": f"/api/agent/runs/{run.id}/events",
    }


def _format_sse(data: dict, event: str, event_id: str | None = None) -> str:
    lines = [f"event: {event}", f"data: {json.dumps(data, ensure_ascii=False)}"]
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _format_heartbeat() -> str:
    return ": heartbeat\n\n"


async def create_agent_run_view(
    *,
    query: str | None,
    agent_id: str,
    thread_id: str,
    meta: dict,
    image_content: str | None,
    current_uid: str,
    db: AsyncSession,
    resume: object | None = None,
    parent_run_id: str | None = None,
    resume_request_id: str | None = None,
) -> dict:
    if not query and resume is None:
        raise HTTPException(status_code=422, detail="query 或 resume 不能为空")

    if not thread_id:
        raise HTTPException(status_code=422, detail="thread_id 不能为空")

    conv_repo = ConversationRepository(db)
    conversation = await conv_repo.get_conversation_by_thread_id(thread_id)
    if not conversation or conversation.uid != str(current_uid) or conversation.status == "deleted":
        raise HTTPException(status_code=404, detail="对话线程不存在")
    if conversation.agent_id != agent_id:
        raise HTTPException(status_code=409, detail="已有线程已绑定智能体，不能切换")

    user_result = await db.execute(select(User).where(User.uid == str(current_uid)))
    current_user = user_result.scalar_one_or_none()
    if not current_user:
        raise HTTPException(status_code=404, detail="用户不存在")

    agent_repo = AgentRepository(db)
    agent_item = await agent_repo.get_visible_by_slug(slug=agent_id, user=current_user)
    if not agent_item:
        raise HTTPException(status_code=404, detail="智能体不存在")
    if not agent_manager.get_agent(agent_item.backend_id):
        raise HTTPException(status_code=404, detail=f"智能体后端 {agent_item.backend_id} 不存在")

    run_type = "resume" if resume is not None else "chat"
    request_id = str(resume_request_id or (meta or {}).get("request_id") or uuid.uuid4())
    config = {"thread_id": thread_id, "agent_id": agent_id}
    run_repo = AgentRunRepository(db)
    if run_type == "resume":
        if not parent_run_id:
            raise HTTPException(status_code=422, detail="parent_run_id 不能为空")
        parent_run = await run_repo.get_run_for_user(parent_run_id, str(current_uid))
        if not parent_run or parent_run.thread_id != thread_id:
            raise HTTPException(status_code=404, detail="被恢复的运行任务不存在")
        if parent_run.status != "interrupted":
            raise HTTPException(status_code=409, detail="只有 interrupted run 可以恢复")
        if resume_request_id:
            existing_resume = await run_repo.get_resume_run(parent_run_id, resume_request_id)
            if existing_resume and existing_resume.uid == str(current_uid):
                return _build_run_response(existing_resume)
    existing = await run_repo.get_run_by_request_id(request_id)
    if existing and existing.uid == str(current_uid):
        return _build_run_response(existing)
    if existing and existing.uid != str(current_uid):
        raise HTTPException(status_code=409, detail="request_id 冲突")

    run_id = str(uuid.uuid4())
    input_payload = {
        "query": query or "",
        "resume": resume,
        "parent_run_id": parent_run_id,
        "resume_request_id": resume_request_id,
        "run_type": run_type,
        "config": config or {},
        "image_content": image_content,
        "agent_id": agent_id,
        "backend_id": agent_item.backend_id,
        "thread_id": thread_id,
        "uid": str(current_uid),
        "request_id": request_id,
        "attachment_file_ids": (meta or {}).get("attachment_file_ids") or [],
        "created_at": utc_now_naive().isoformat(),
    }
    try:
        run = await run_repo.create_run(
            run_id=run_id,
            thread_id=thread_id,
            agent_id=agent_id,
            uid=str(current_uid),
            request_id=request_id,
            input_payload=input_payload,
            conversation_id=conversation.id,
            parent_run_id=parent_run_id,
            run_type=run_type,
            resume_request_id=resume_request_id,
            checkpoint_thread_id=thread_id,
        )
        input_content = query or json.dumps(resume, ensure_ascii=False)
        input_metadata = {
            "request_id": request_id,
            "run_id": run_id,
            "run_type": run_type,
            "parent_run_id": parent_run_id,
            "resume": resume,
            "attachments": [],
        }
        if run_type == "resume":
            input_metadata["source"] = "ask_user_question_resume"

        input_message = Message(
            conversation_id=conversation.id,
            role="user",
            content=input_content,
            message_type="resume" if run_type == "resume" else "text",
            image_content=image_content,
            run_id=run_id,
            request_id=request_id,
            delivery_status="complete",
            extra_metadata=input_metadata,
        )
        db.add(input_message)
        await db.flush()
        await run_repo.set_input_message(run_id, input_message.id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await run_repo.get_run_by_request_id(request_id)
        if existing and existing.uid == str(current_uid):
            return _build_run_response(existing)
        raise HTTPException(status_code=409, detail="request_id 冲突")

    queue = await get_arq_pool()
    await queue.enqueue_job("process_agent_run", run.id, _job_id=f"run:{run.id}")

    return _build_run_response(run)


async def get_agent_run_view(*, run_id: str, current_uid: str, db: AsyncSession) -> dict:
    repo = AgentRunRepository(db)
    run = await repo.get_run_for_user(run_id, str(current_uid))
    if not run:
        raise HTTPException(status_code=404, detail="运行任务不存在")
    return {"run": run.to_dict()}


async def cancel_agent_run_view(*, run_id: str, current_uid: str, db: AsyncSession) -> dict:
    repo = AgentRunRepository(db)
    run = await repo.get_run_for_user(run_id, str(current_uid))
    if not run:
        raise HTTPException(status_code=404, detail="运行任务不存在")

    run = await repo.request_cancel(run_id)
    await publish_cancel_signal(run_id)
    return {"run": run.to_dict() if run else None}


async def stream_agent_run_events(
    *,
    run_id: str,
    after_seq: str,
    current_uid: str,
) -> AsyncIterator[str]:
    started_at = utc_now_naive()
    last_heartbeat_ts = started_at

    last_seq = normalize_after_seq(after_seq)

    try:
        while True:
            try:
                async with pg_manager.get_async_session_context() as db:
                    repo = AgentRunRepository(db)
                    run = await repo.get_run_for_user(run_id, str(current_uid))
                    if not run:
                        yield _format_sse({"run_id": run_id, "message": "运行任务不存在"}, event="error")
                        return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Run SSE DB error for run {run_id}: {e}")
                yield _format_sse(
                    {
                        "run_id": run_id,
                        "message": "运行事件流暂时不可用，请重连",
                        "reason": "db_error",
                    },
                    event="error",
                )
                return

            try:
                events = await list_run_stream_events(run_id, after_seq=last_seq, limit=200)
            except Exception as e:
                logger.warning(f"Run SSE redis error for run {run_id}: {e}")
                yield _format_sse(
                    {
                        "run_id": run_id,
                        "message": "运行事件流暂时不可用，请重连",
                        "reason": "redis_error",
                    },
                    event="error",
                )
                return

            emitted_terminal = False
            for event in events:
                seq = str(event.get("seq") or "0-0")
                last_seq = seq
                event_type = event.get("event_type") or "message"
                envelope = event.get("payload") or {}
                yield _format_sse(envelope, event=event_type, event_id=seq)
                if event_type == "end":
                    emitted_terminal = True

            if emitted_terminal:
                return

            if run.status in TERMINAL_RUN_STATUSES and not events:
                terminal_seq = last_seq
                if terminal_seq in {"", "0-0"}:
                    terminal_seq = await get_last_run_stream_seq(run_id)
                if terminal_seq in {"", "0-0"}:
                    terminal_seq = None
                yield _format_sse(
                    build_run_event_envelope(
                        run_id=run_id,
                        thread_id=run.thread_id,
                        event_type="end",
                        payload={"status": run.status},
                        created_at=utc_now_naive().isoformat(),
                    ),
                    event="end",
                    event_id=terminal_seq,
                )
                return

            now = utc_now_naive()
            elapsed_seconds = (now - started_at).total_seconds()
            heartbeat_elapsed = (now - last_heartbeat_ts).total_seconds()
            if heartbeat_elapsed >= SSE_HEARTBEAT_SECONDS:
                yield _format_heartbeat()
                last_heartbeat_ts = now

            if elapsed_seconds >= SSE_MAX_CONNECTION_MINUTES * 60:
                return

            await asyncio.sleep(SSE_POLL_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        return


async def get_active_run_by_thread(*, thread_id: str, current_uid: str, db: AsyncSession) -> dict:
    from sqlalchemy import select
    from yuxi.storage.postgres.models_business import AgentRun

    active_result = await db.execute(
        select(AgentRun)
        .where(
            AgentRun.thread_id == thread_id,
            AgentRun.uid == str(current_uid),
            AgentRun.run_type.in_(["chat", "resume"]),
            AgentRun.status.in_(["pending", "running", "cancel_requested"]),
        )
        .order_by(AgentRun.created_at.desc())
        .limit(1)
    )
    run = active_result.scalar_one_or_none()
    if not run:
        interrupted_result = await db.execute(
            select(AgentRun)
            .where(
                AgentRun.thread_id == thread_id,
                AgentRun.uid == str(current_uid),
                AgentRun.run_type.in_(["chat", "resume"]),
                AgentRun.status == "interrupted",
            )
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
        run = interrupted_result.scalar_one_or_none()
    return {"run": run.to_dict() if run else None}
