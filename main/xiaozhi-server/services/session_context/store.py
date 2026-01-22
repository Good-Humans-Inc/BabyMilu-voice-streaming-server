from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Any

from google.cloud import firestore

from services.logging import setup_logging
from config.settings import get_gcp_credentials_path
from services.session_context import models

_UNSET = object()

TAG = __name__
logger = setup_logging()


class SessionContextStore:
    """Firestore-backed store for proactive session metadata."""

    def __init__(self, collection_name: str = "sessionContexts"):
        self.collection_name = collection_name
        self._firestore_client: Optional[firestore.Client] = None

    def _client(self) -> firestore.Client:
        if self._firestore_client is None:
            creds_path = get_gcp_credentials_path()
            if creds_path:
                # Explicitly set the env var to ensure it's used
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path
            self._firestore_client = firestore.Client()
        return self._firestore_client

    def _collection(self):
        return self._client().collection(self.collection_name)

    def create_session(
        self,
        *,
        device_id: str,
        session_type: str,
        session_config: Optional[Dict[str, Any]] = None,
        ttl: Optional[timedelta] = None,
        triggered_at: Optional[datetime] = None,
        conversation: Optional[Dict[str, Any]] = None,
        is_snooze_follow_up: bool = False,
        ) -> models.ModeSession:
        triggered_at = triggered_at or datetime.now(timezone.utc)
        ttl_seconds = models.ttl_seconds_from_delta(ttl)
        session = models.ModeSession(
            device_id=device_id,
            session_type=session_type,
            triggered_at=triggered_at,
            ttl_seconds=ttl_seconds,
            session_config=session_config or {},
            conversation=conversation or {},
            is_snooze_follow_up=is_snooze_follow_up,
        )
        self._collection().document(device_id).set(
            {
                "sessionType": session.session_type,
                "triggeredAt": session.triggered_at,
                "ttlSeconds": session.ttl_seconds,
                "expiresAt": session.expires_at,
                "sessionConfig": session.session_config,
                "conversation": session.conversation,
                "isSnoozeFollowUp": session.is_snooze_follow_up,
            },
            merge=True,
        )
        logger.bind(tag=TAG).info(
            f"Created sessionContext for {device_id} ({session_type}) ttl={ttl_seconds}s"
        )
        return session

    def get_session(
        self,
        device_id: str,
        now: Optional[datetime] = None,
        delete_if_expired: bool = True,
        timeout: float = 3.0,
        ) -> Optional[models.ModeSession]:
        try:
            doc = self._collection().document(device_id).get(timeout=timeout)
        except Exception as e:
            logger.bind(tag=TAG).warning(
                f"Failed to get session from Firestore for {device_id}: {e}"
            )
            return None
        
        if not doc.exists:
            return None
        data = doc.to_dict() or {}
        session = self._hydrate_session(device_id, data)
        if not session:
            return None
        if session.is_expired(now):
            logger.bind(tag=TAG).info(
                f"SessionContext for {device_id} expired; delete={delete_if_expired}"
            )
            if delete_if_expired:
                self.delete_session(device_id)
            return None
        return session

    def delete_session(self, device_id: str) -> None:
        self._collection().document(device_id).delete()

    def update_session(
        self,
        device_id: str,
        *,
        session_config: Optional[Dict[str, Any]] = None,
        conversation: Any = _UNSET,
        is_snooze_follow_up: Optional[bool] = None,
    ) -> None:
        updates: Dict[str, Any] = {}
        if session_config is not None:
            updates["sessionConfig"] = session_config
        if conversation is not _UNSET:
            if conversation is None:
                updates["conversation"] = firestore.DELETE_FIELD
                logger.bind(tag=TAG).debug(
                    f"Deleting conversation field for session {device_id}"
                )
            else:
                updates["conversation"] = conversation
                logger.bind(tag=TAG).debug(
                    f"Setting conversation for session {device_id}: {conversation}"
                )
        if is_snooze_follow_up is not None:
            updates["isSnoozeFollowUp"] = bool(is_snooze_follow_up)
        if not updates:
            return
        self._collection().document(device_id).set(updates, merge=True)

    def _hydrate_session(
        self, device_id: str, payload: Dict[str, Any]
    ) -> Optional[models.ModeSession]:
        triggered_at = payload.get("triggeredAt")
        if isinstance(triggered_at, str):
            triggered_at = datetime.fromisoformat(triggered_at)
        if not isinstance(triggered_at, datetime):
            triggered_at = datetime.now(timezone.utc)
        expires_at = payload.get("expiresAt")
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        ttl_seconds = payload.get("ttlSeconds") or models.DEFAULT_SESSION_TTL_SECONDS
        session_config = payload.get("sessionConfig")
        if not isinstance(session_config, dict):
            session_config = {}
        conversation = payload.get("conversation")
        if not isinstance(conversation, dict):
            conversation = {}
        is_snooze_follow_up = bool(payload.get("isSnoozeFollowUp", False))
        logger.bind(tag=TAG).debug(
            f"Hydrating session for {device_id}: conversation={conversation}, "
            f"is_snooze_follow_up={is_snooze_follow_up}"
        )
        try:
            return models.ModeSession(
                device_id=device_id,
                session_type=payload.get("sessionType") or "alarm",
                triggered_at=triggered_at,
                ttl_seconds=int(ttl_seconds),
                expires_at=expires_at,
                session_config=session_config,
                conversation=conversation,
                is_snooze_follow_up=is_snooze_follow_up,
            )
        except Exception as exc:
            logger.bind(tag=TAG).warning(
                f"Failed to hydrate sessionContext for {device_id}: {exc}"
            )
            return None


_DEFAULT_STORE = SessionContextStore()


def get_store() -> SessionContextStore:
    return _DEFAULT_STORE


def create_session(**kwargs) -> models.ModeSession:
    return _DEFAULT_STORE.create_session(**kwargs)


def get_session(device_id: str, **kwargs) -> Optional[models.ModeSession]:
    return _DEFAULT_STORE.get_session(device_id, **kwargs)


def delete_session(device_id: str) -> None:
    _DEFAULT_STORE.delete_session(device_id)


def update_session(device_id: str, **kwargs) -> None:
    _DEFAULT_STORE.update_session(device_id, **kwargs)

