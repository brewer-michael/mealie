"""
The provider answers one task got, by request (docs/ai/PHASE2.md §3.9). A backup restore can stop a card's reading
part way: the image reader answered, then the next step met the restore's dropped tables. Rather than pay for that
answer again, the task's answers are kept with its result (`results`), and the card's next task replays each request
it makes again with the same inputs (the same prompt, message, response schema, attachments and provider choice)
instead of sending it. The handlers' AI service records and replays them (`tasks._KeptAnswersService`).
"""

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel


class KeptAnswers:
    """Provider answers by request (`request_key`): each the response schema's name, the answer, and its usage"""

    def __init__(self, entries: dict[str, dict[str, Any]] | None = None) -> None:
        self.entries: dict[str, dict[str, Any]] = dict(entries or {})
        self.replayed = 0
        """How many requests this task answered from them instead of asking a provider"""

    def __bool__(self) -> bool:
        return bool(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    @staticmethod
    def request_key(
        prompt: str,
        message: str,
        response_schema: type,
        attachments: Iterable[Any] | None,
        provider: BaseModel | None,
        slot: Any,
    ) -> str:
        """A request's identity: everything the answer depends on, an image by its content"""
        images = []
        for attachment in attachments or []:
            image_url = getattr(attachment, "get_image_url", None)
            data = image_url() if callable(image_url) else attachment.model_dump_json()
            images.append(hashlib.sha256(str(data).encode()).hexdigest())
        request = {
            "prompt": prompt,
            "message": message,
            "schema": f"{response_schema.__module__}.{response_schema.__qualname__}",
            "attachments": images,
            "provider": str(getattr(provider, "id", None)) if provider is not None else None,
            "slot": str(getattr(slot, "value", slot)) if slot is not None else None,
        }
        return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        return self.entries.get(key)

    def put(self, key: str, entry: dict[str, Any]) -> None:
        self.entries[key] = entry
