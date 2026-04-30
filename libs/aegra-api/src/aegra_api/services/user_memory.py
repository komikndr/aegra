"""Cross-agent user memory storage and prompt formatting."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.orm import UserMemory, UserMemorySnapshot

MAX_FACTS = 40
MAX_FACT_CHARS = 280
MAX_SNAPSHOT_CHARS = 8000
IMPLICIT_PROMOTION_THRESHOLD = 3

_MEMORY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("identity", r"\bmy name is\s+([^\n.!?]{1,80})"),
    ("identity", r"\bcall me\s+([^\n.!?]{1,80})"),
    ("preference", r"\bi prefer\s+([^\n]{1,180})"),
    ("preference", r"\bi like\s+([^\n]{1,180})"),
    ("work", r"\bi work (?:as|at|for|in)\s+([^\n.!?]{1,120})"),
    ("work", r"\bmy role is\s+([^\n.!?]{1,120})"),
    ("instruction", r"\bremember that\s+([^\n]{1,220})"),
    ("instruction", r"\balways\s+([^\n]{1,180})"),
    ("instruction", r"\bdon't\s+([^\n]{1,180})"),
    ("instruction", r"\bdo not\s+([^\n]{1,180})"),
)

_IMPLICIT_SIGNALS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "implicit_preference",
        "User likely prefers concise, direct answers",
        ("short answer", "keep it short", "be concise", "tl;dr", "summarize"),
    ),
    (
        "implicit_preference",
        "User likely prefers implementation-first technical guidance",
        ("implement", "patch", "fix", "run test", "docker", "build"),
    ),
    (
        "implicit_context",
        "User frequently works with Docker-based local stacks",
        ("docker", "compose", "container", "image", "rebuild"),
    ),
    (
        "implicit_context",
        "User frequently works with Keycloak authentication and roles",
        ("keycloak", "kc", "realm", "role", "oidc"),
    ),
    (
        "implicit_context",
        "User frequently works with PostgreSQL or database operations",
        ("postgres", "postgresql", "psql", "database", "migration", "alembic"),
    ),
    (
        "implicit_language",
        "User may be comfortable mixing English and Indonesian",
        ("gimana", "bisa", "dong", "nggak", "ga ", "aja", "sama"),
    ),
)


async def get_user_memory_context(session: AsyncSession, user_id: str) -> str:
    snapshot = await session.scalar(select(UserMemorySnapshot).where(UserMemorySnapshot.user_id == user_id))
    if not snapshot or not snapshot.content.strip():
        return ""
    return snapshot.content.strip()


async def update_user_memory_from_run(
    session: AsyncSession,
    *,
    user_id: str,
    thread_id: str,
    run_id: str,
    input_data: dict[str, Any] | None,
    output_data: Any,
) -> None:
    user_text = _latest_human_text(input_data)
    if not user_text:
        return

    explicit_candidates = _extract_memory_candidates(user_text)
    implicit_candidates = _extract_implicit_candidates(user_text)
    if not explicit_candidates and not implicit_candidates:
        return

    memory_changed = False
    snapshot_changed = False
    for kind, content in explicit_candidates:
        inserted = await _upsert_memory(
            session,
            user_id=user_id,
            kind=kind,
            content=content,
            thread_id=thread_id,
            run_id=run_id,
            metadata={"extractor": "deterministic-v1", "confidence": "high"},
        )
        memory_changed = inserted or memory_changed
        snapshot_changed = inserted or snapshot_changed

    for kind, content in implicit_candidates:
        result = await _upsert_implicit_memory(
            session,
            user_id=user_id,
            kind=kind,
            content=content,
            thread_id=thread_id,
            run_id=run_id,
        )
        memory_changed = result[0] or memory_changed
        snapshot_changed = result[1] or snapshot_changed

    if not memory_changed:
        return

    if snapshot_changed:
        await _rebuild_snapshot(session, user_id, output_data)
    await session.commit()


async def _rebuild_snapshot(session: AsyncSession, user_id: str, output_data: Any) -> None:
    result = await session.scalars(
        select(UserMemory)
        .where(UserMemory.user_id == user_id)
        .order_by(UserMemory.created_at.desc())
        .limit(MAX_FACTS * 3)
    )
    facts = [memory for memory in reversed(result.all()) if _memory_is_snapshot_ready(memory)][-MAX_FACTS:]
    lines = ["Known user facts and preferences:"]
    for memory in facts:
        lines.append(f"- [{_memory_label(memory)}] {memory.content}")

    assistant_hint = _latest_assistant_text(output_data)
    if assistant_hint:
        lines.append("")
        lines.append("Recent interaction signal:")
        lines.append(f"- {assistant_hint[:MAX_FACT_CHARS]}")

    content = "\n".join(lines).strip()[:MAX_SNAPSHOT_CHARS]
    memory_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    now = datetime.now(UTC)

    existing = await session.scalar(select(UserMemorySnapshot).where(UserMemorySnapshot.user_id == user_id))
    if existing:
        if existing.memory_hash == memory_hash:
            return
        await session.execute(
            update(UserMemorySnapshot)
            .where(UserMemorySnapshot.user_id == user_id)
            .values(
                content=content,
                version=UserMemorySnapshot.version + 1,
                memory_hash=memory_hash,
                metadata_dict={"builder": "deterministic-v1"},
                updated_at=now,
            )
        )
        return

    session.add(
        UserMemorySnapshot(
            user_id=user_id,
            content=content,
            memory_hash=memory_hash,
            metadata_dict={"builder": "deterministic-v1"},
            created_at=now,
            updated_at=now,
        )
    )


def _extract_memory_candidates(text: str) -> list[tuple[str, str]]:
    normalized_text = " ".join(text.strip().split())
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind, pattern in _MEMORY_PATTERNS:
        for match in re.finditer(pattern, normalized_text, flags=re.IGNORECASE):
            value = _clean_fact(match.group(1))
            if not value:
                continue
            content = _format_fact(kind, value)
            key = content.lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append((kind, content))
    return candidates[:8]


async def _upsert_memory(
    session: AsyncSession,
    *,
    user_id: str,
    kind: str,
    content: str,
    thread_id: str,
    run_id: str,
    metadata: dict[str, Any],
) -> bool:
    existing = await session.scalar(
        select(UserMemory).where(
            UserMemory.user_id == user_id,
            UserMemory.content == content,
        )
    )
    if existing:
        return False
    memory = UserMemory(
        user_id=user_id,
        kind=kind,
        content=content,
        source_thread_id=thread_id,
        source_run_id=run_id,
        metadata_dict=metadata,
    )
    session.add(memory)
    await session.flush()
    return True


async def _upsert_implicit_memory(
    session: AsyncSession,
    *,
    user_id: str,
    kind: str,
    content: str,
    thread_id: str,
    run_id: str,
) -> tuple[bool, bool]:
    existing = await session.scalar(
        select(UserMemory).where(
            UserMemory.user_id == user_id,
            UserMemory.content == content,
        )
    )
    if not existing:
        inserted = await _upsert_memory(
            session,
            user_id=user_id,
            kind=kind,
            content=content,
            thread_id=thread_id,
            run_id=run_id,
            metadata={
                "extractor": "implicit-deterministic-v1",
                "confidence": "low",
                "evidence_count": 1,
                "promotion_threshold": IMPLICIT_PROMOTION_THRESHOLD,
            },
        )
        return inserted, False

    metadata = dict(existing.metadata_dict or {})
    evidence_count = int(metadata.get("evidence_count") or 1) + 1
    was_promoted = _implicit_is_promoted(metadata)
    metadata.update(
        {
            "extractor": metadata.get("extractor") or "implicit-deterministic-v1",
            "confidence": "medium" if evidence_count >= IMPLICIT_PROMOTION_THRESHOLD else "low",
            "evidence_count": evidence_count,
            "promotion_threshold": IMPLICIT_PROMOTION_THRESHOLD,
        }
    )
    existing.metadata_dict = metadata
    existing.source_thread_id = thread_id
    existing.source_run_id = run_id
    existing.updated_at = datetime.now(UTC)
    await session.flush()
    promoted_now = not was_promoted and _implicit_is_promoted(metadata)
    return True, promoted_now


def _extract_implicit_candidates(text: str) -> list[tuple[str, str]]:
    normalized_text = f" {text.lower()} "
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind, content, signals in _IMPLICIT_SIGNALS:
        matches = sum(1 for signal in signals if signal in normalized_text)
        if matches < 2:
            continue
        key = content.lower()
        if key in seen:
            continue
        seen.add(key)
        candidates.append((kind, content))
    return candidates[:4]


def _memory_is_snapshot_ready(memory: UserMemory) -> bool:
    if not memory.kind.startswith("implicit"):
        return True
    return _implicit_is_promoted(memory.metadata_dict or {})


def _implicit_is_promoted(metadata: dict[str, Any]) -> bool:
    return int(metadata.get("evidence_count") or 0) >= int(
        metadata.get("promotion_threshold") or IMPLICIT_PROMOTION_THRESHOLD
    )


def _memory_label(memory: UserMemory) -> str:
    if not memory.kind.startswith("implicit"):
        return memory.kind
    metadata = memory.metadata_dict or {}
    confidence = metadata.get("confidence") or "medium"
    evidence_count = metadata.get("evidence_count") or IMPLICIT_PROMOTION_THRESHOLD
    return f"{memory.kind}, {confidence} confidence, {evidence_count} signals"


def _format_fact(kind: str, value: str) -> str:
    if kind == "identity":
        return f"User identity/name preference: {value}"
    if kind == "preference":
        return f"User preference: {value}"
    if kind == "work":
        return f"User work context: {value}"
    return f"User instruction/preference: {value}"


def _clean_fact(value: str) -> str:
    cleaned = value.strip(" .!?;:\"'")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) < 3:
        return ""
    return cleaned[:MAX_FACT_CHARS]


def _latest_human_text(input_data: dict[str, Any] | None) -> str:
    if not input_data:
        return ""
    messages = input_data.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            text = _message_text(message, human_only=True)
            if text:
                return text
    return _message_text(input_data, human_only=True)


def _latest_assistant_text(output_data: Any) -> str:
    if isinstance(output_data, dict):
        messages = output_data.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                text = _message_text(message, assistant_only=True)
                if text:
                    return text
    return ""


def _message_text(message: Any, *, human_only: bool = False, assistant_only: bool = False) -> str:
    if isinstance(message, HumanMessage):
        return _content_text(message.content)
    if isinstance(message, BaseMessage):
        role = getattr(message, "type", "")
        if human_only and role not in {"human", "user"}:
            return ""
        if assistant_only and role not in {"ai", "assistant"}:
            return ""
        return _content_text(message.content)
    if isinstance(message, dict):
        role = str(message.get("role") or message.get("type") or "").lower()
        if human_only and role not in {"human", "user"}:
            return ""
        if assistant_only and role not in {"ai", "assistant"}:
            return ""
        return _content_text(message.get("content"))
    return ""


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts).strip()
    if isinstance(content, dict):
        value = content.get("text") or content.get("content")
        if isinstance(value, str):
            return value.strip()
    return ""
