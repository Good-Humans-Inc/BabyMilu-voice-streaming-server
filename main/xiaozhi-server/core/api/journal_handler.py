import json
import os

from aiohttp import web

from services.journals.store import soft_delete_journal_entry
from services.logging import setup_logging

TAG = __name__


class JournalHandler:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()

    async def handle_delete(self, request: web.Request) -> web.Response:
        token = os.environ.get("JOURNAL_INTERNAL_API_TOKEN", "").strip()
        if token:
            auth_header = request.headers.get("Authorization", "")
            if auth_header != f"Bearer {token}":
                return self._json({"ok": False, "error": "unauthorized"}, status=401)
        else:
            self.logger.bind(tag=TAG).warning(
                "JOURNAL_INTERNAL_API_TOKEN is not set; /journals/delete allows unauthenticated local/internal calls"
            )

        try:
            data = await request.json()
        except Exception:
            text = await request.text()
            try:
                data = json.loads(text)
            except Exception:
                return self._json({"ok": False, "error": "invalid json"}, status=400)

        user_id = str(data.get("userId") or data.get("user_id") or "").strip()
        character_id = str(data.get("characterId") or data.get("character_id") or "").strip()
        entry_id = str(data.get("entryId") or data.get("entry_id") or "").strip()
        if not user_id or not character_id or not entry_id:
            return self._json(
                {"ok": False, "error": "userId, characterId, and entryId are required"},
                status=400,
            )

        deleted = soft_delete_journal_entry(
            user_id=user_id,
            character_id=character_id,
            entry_id=entry_id,
        )
        if not deleted:
            return self._json({"ok": False, "error": "journal entry not found"}, status=404)
        return self._json({"ok": True, "entryId": entry_id, "isDeleted": True})

    def _json(self, payload: dict, *, status: int = 200) -> web.Response:
        response = web.json_response(payload, status=status)
        response.headers["Access-Control-Allow-Headers"] = (
            "authorization, client-id, content-type, device-id"
        )
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response
