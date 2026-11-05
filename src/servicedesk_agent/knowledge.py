"""Versioned policy documents retrieved with Haystack's native BM25 component."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from servicedesk_agent.domain import BusinessError, Evidence, fingerprint, utc_now


class PolicyVersion(BaseModel):
    policy_id: str
    version: str
    source_uri: str
    scope: str
    valid_from: AwareDatetime
    valid_to: AwareDatetime | None = None
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_interval(self):
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("A policy interval must end after it begins.")
        return self

    def active_at(self, now: datetime) -> bool:
        return self.valid_from <= now and (self.valid_to is None or now < self.valid_to)

    @property
    def key(self) -> str:
        return fingerprint(self.model_dump(mode="json"))

    def evidence(self, now: datetime, *, limit: int | None = None) -> Evidence:
        return Evidence(
            source_uri=self.source_uri,
            source_kind="policy",
            entity_scope=self.scope,
            source_version=f"{self.version}:{self.key}",
            observed_at=now,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
            status="observed" if self.active_at(now) else "historical",
            content=self.text if limit is None else self.text[:limit],
        )


class PolicyCatalog:
    def __init__(self, source: Path | list[PolicyVersion], scope: str):
        self.source = source
        self.scope = scope
        self._digest = None
        self._documents: list[PolicyVersion] = []
        self._retriever = None

    def _load(self) -> None:
        if isinstance(self.source, Path):
            raw = json.loads(self.source.read_text(encoding="utf-8"))
            digest = fingerprint(raw)
            documents = [PolicyVersion.model_validate(item) for item in raw]
        else:
            documents = self.source
            digest = fingerprint([item.model_dump(mode="json") for item in documents])
        if self._digest == digest:
            return
        seen = set()
        for item in documents:
            identity = (item.scope, item.policy_id, item.version)
            if identity in seen:
                raise ValueError("Policy versions must have unique scope, ID and version.")
            seen.add(identity)
        for index, first in enumerate(documents):
            for second in documents[index + 1 :]:
                if (first.scope, first.policy_id) != (second.scope, second.policy_id):
                    continue
                if (first.valid_to is None or second.valid_from < first.valid_to) and (
                    second.valid_to is None or first.valid_from < second.valid_to
                ):
                    raise ValueError("Published versions of the same policy overlap.")

        os.environ.setdefault("HAYSTACK_TELEMETRY_ENABLED", "false")
        from haystack import Document
        from haystack.components.retrievers.in_memory import InMemoryBM25Retriever
        from haystack.document_stores.in_memory import InMemoryDocumentStore

        store = InMemoryDocumentStore()
        store.write_documents(
            [
                Document(
                    id=item.key,
                    content=item.text,
                    meta={
                        "scope": item.scope,
                        "policy_id": item.policy_id,
                        "version": item.version,
                    },
                )
                for item in documents
            ]
        )
        self._retriever = InMemoryBM25Retriever(document_store=store, top_k=3)
        self._documents = list(documents)
        self._digest = digest

    def search(self, query: str, *, top_k: int = 3, now: datetime | None = None) -> list[dict]:
        if not query.strip() or not 1 <= top_k <= 5:
            raise BusinessError("invalid_query", "Use a nonempty query and top_k from 1 to 5.")
        now = now or utc_now()
        self._load()
        current = {
            item.key: item
            for item in self._documents
            if item.scope == self.scope and item.active_at(now)
        }
        if not current:
            return []
        result = self._retriever.run(
            query=query,
            top_k=top_k,
            filters={
                "operator": "AND",
                "conditions": [
                    {"field": "meta.scope", "operator": "==", "value": self.scope},
                    {"field": "id", "operator": "in", "value": list(current)},
                ],
            },
        )
        return [
            {
                "policy_id": current[item.id].policy_id,
                "version": current[item.id].version,
                "evidence": current[item.id].evidence(now, limit=700).model_dump(mode="json"),
            }
            for item in result["documents"]
        ]

    def read(self, policy_id: str, version: str = "", *, now: datetime | None = None) -> dict:
        now = now or utc_now()
        self._load()
        matches = [
            item
            for item in self._documents
            if item.scope == self.scope
            and item.policy_id == policy_id
            and (item.version == version if version else item.active_at(now))
        ]
        if len(matches) != 1:
            raise BusinessError("policy_unavailable", "Specify an available policy and version.")
        return matches[0].evidence(now).model_dump(mode="json")
