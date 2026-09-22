"""Execution context: the pieces of evidence agents read before deciding.

Each item is tagged `trusted` or not. Untrusted context (scraped attachments,
tool output, third-party text) is exactly the surface prompt injection uses —
modeling that explicitly is what lets the rest of the system reason about it.
"""
from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class ContextItem:
    id: str
    source: str          # e.g. "user_input", "ticket_attachment", "tool_result"
    trusted: bool
    content: Any

    def as_text(self) -> str:
        return str(self.content)


class ContextStore:
    def __init__(self):
        self._items: Dict[str, ContextItem] = {}

    def add(self, item: ContextItem) -> ContextItem:
        self._items[item.id] = item
        return item

    def get(self, item_id: str) -> ContextItem:
        return self._items[item_id]

    def snapshot(self, item_ids) -> Dict[str, ContextItem]:
        return {i: self._items[i] for i in item_ids if i in self._items}

    def all_ids(self):
        return list(self._items.keys())
