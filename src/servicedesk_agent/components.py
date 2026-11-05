"""Optional existing components: native Skills, Haystack BM25 and LangMem."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any

from deepagents.backends import StateBackend
from deepagents.backends.utils import create_file_data
from deepagents.middleware.filesystem import FilesystemPermission
from langchain_core.tools import tool

from servicedesk_agent.domain import BusinessError, Evidence, fingerprint
from servicedesk_agent.knowledge import PolicyCatalog


@dataclass
class ComponentBundle:
    tools: list = field(default_factory=list)
    agent_kwargs: dict[str, Any] = field(default_factory=dict)
    initial_state: dict[str, Any] = field(default_factory=dict)


def memory_namespace(scope: str) -> tuple[str, ...]:
    return ("servicedesk", fingerprint(scope), "confirmed_preferences")


def build_components(scope: str, store, catalog: PolicyCatalog) -> ComponentBundle:
    from langmem import create_search_memory_tool

    @tool
    def search_policy(query: str, top_k: int = 3) -> str:
        """Search current policies by BM25 within this service scope; return versioned citations."""
        try:
            return json.dumps(catalog.search(query, top_k=top_k))
        except BusinessError as exc:
            return json.dumps(exc.as_dict())

    @tool
    def read_policy(policy_id: str, version: str = "") -> str:
        """Read a full policy. Omit version for current policy; supply one for historical text."""
        try:
            return json.dumps(catalog.read(policy_id, version))
        except BusinessError as exc:
            return json.dumps(exc.as_dict())

    memory_tool = create_search_memory_tool(
        namespace=memory_namespace(scope),
        name="recall_confirmed_preferences",
        instructions=(
            "This SQLite store has no embedding index. Results are scoped confirmed preferences "
            "ordered by update time; use filters for exact lookup. These are historical user "
            "statements, not fresh authority for business actions."
        ),
    )
    asset_root = files("servicedesk_agent").joinpath("assets/skills")
    skill_files = {}
    for directory in asset_root.iterdir():
        skill = directory.joinpath("SKILL.md")
        if skill.is_file():
            skill_files[f"/skills/{directory.name}/SKILL.md"] = create_file_data(
                skill.read_text(encoding="utf-8")
            )
    return ComponentBundle(
        tools=[search_policy, read_policy, memory_tool],
        agent_kwargs={
            "backend": StateBackend(),
            "store": store,
            "skills": ["/skills/"],
            "permissions": [
                FilesystemPermission(operations=["write"], paths=["/skills/**"], mode="deny")
            ],
        },
        initial_state={"files": skill_files},
    )


def remember_confirmed_preference(scope: str, text: str, store) -> str:
    from langmem import create_manage_memory_tool

    if not text.strip() or len(text) > 2000:
        raise ValueError(
            "Use a nonempty, explicitly confirmed preference of at most 2000 characters."
        )
    evidence = Evidence(
        source_uri=f"user-confirmation://{fingerprint(scope)}/{fingerprint(text)}",
        source_kind="user",
        entity_scope=scope,
        source_version=fingerprint(text),
        content=text,
    )
    manage = create_manage_memory_tool(
        namespace=memory_namespace(scope),
        schema=Evidence,
        actions_permitted=("create",),
        store=store,
    )
    return manage.invoke({"content": evidence.model_dump(mode="json")})
