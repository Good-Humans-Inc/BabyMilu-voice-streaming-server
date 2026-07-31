from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

import requests
from google.cloud import firestore, storage

from ..context import ScenarioContext
from ..models import ScenarioResult, utc_now_iso
from ..scenario import BaseScenario


def _response_data(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _delete_document_tree(reference: Any) -> None:
    for collection in reference.collections():
        for child in collection.stream():
            _delete_document_tree(child.reference)
    reference.delete()


class DaycareFoodGiftScenario(BaseScenario):
    name = "interaction.daycare_food_gift"
    description = (
        "Create isolated Firebase-authenticated Food and Gift actions, verify "
        "private generated images and send completion, and clean up."
    )

    def _create_test_identity(
        self,
        *,
        api_key: str,
    ) -> tuple[str, str]:
        email = f"codex-daycare-{uuid.uuid4().hex}@example.invalid"
        password = uuid.uuid4().hex + uuid.uuid4().hex
        response = requests.post(
            (
                "https://identitytoolkit.googleapis.com/v1/"
                f"accounts:signUp?key={api_key}"
            ),
            json={
                "email": email,
                "password": password,
                "returnSecureToken": True,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        return str(payload["localId"]), str(payload["idToken"])

    def _delete_test_identity(
        self,
        *,
        api_key: str,
        id_token: str,
    ) -> None:
        response = requests.post(
            (
                "https://identitytoolkit.googleapis.com/v1/"
                f"accounts:delete?key={api_key}"
            ),
            json={"idToken": id_token},
            timeout=30,
        )
        response.raise_for_status()

    def _seed(
        self,
        *,
        context: ScenarioContext,
        uid: str,
        character_id: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        context.firestore.collection("users").document(uid).set(
            {
                "uid": uid,
                "auth_uid": uid,
                "name": "Codex Daycare Smoke",
                "lumis": 500,
                "lumisSeq": 0,
                "characterSlots": [character_id],
                "createdAt": now,
                "updatedAt": now,
            }
        )
        context.firestore.collection("characters").document(character_id).set(
            {
                "ownerUid": uid,
                "uid": uid,
                "name": "Milu Smoke",
                "profile": {
                    "name": "Milu Smoke",
                    "personality": "cheerful, curious, and kind",
                    "speechStyle": "warm and concise",
                    "nicknameCharacterCallsUser": "friend",
                },
                "createdAt": now,
                "updatedAt": now,
            }
        )

    def _run_action(
        self,
        *,
        session: requests.Session,
        base_url: str,
        character_id: str,
        daycare_action: str,
        item_description: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        started = time.monotonic()
        create = session.post(
            f"{base_url}/v1/daycare/actions",
            json={
                "characterId": character_id,
                "daycareAction": daycare_action,
                "inputModality": "text",
                "itemDescription": item_description,
                "noteToCharacter": "This is an isolated release smoke test.",
            },
            timeout=min(max(timeout_seconds, 60), 540),
        )
        create_data = _response_data(create)
        details: dict[str, Any] = {
            "daycareAction": daycare_action,
            "createStatus": create.status_code,
            "createSeconds": round(time.monotonic() - started, 3),
            "statusSequence": [],
        }
        if create.status_code != 202:
            details["createError"] = {
                "code": create_data.get("errorCode"),
                "message": create_data.get("msg"),
            }
            return details

        action_id = str(create_data.get("actionId") or "")
        details["actionId"] = action_id
        deadline = time.monotonic() + max(timeout_seconds, 60)
        terminal = ""
        while time.monotonic() < deadline:
            status_response = session.get(
                f"{base_url}/v1/daycare/actions/{action_id}",
                params={"character_id": character_id},
                timeout=30,
            )
            status_data = _response_data(status_response)
            status = str(status_data.get("status") or "")
            if (
                not details["statusSequence"]
                or details["statusSequence"][-1] != status
            ):
                details["statusSequence"].append(status)
            if status in {
                "preview_ready",
                "done",
                "failed",
                "image_failed",
            }:
                terminal = status
                break
            time.sleep(2)
        details["previewStatus"] = terminal or "timeout"
        details["previewSeconds"] = round(time.monotonic() - started, 3)
        if terminal not in {"preview_ready", "done"}:
            return details

        image_response = session.get(
            f"{base_url}/v1/daycare/actions/{action_id}/image-url",
            params={"character_id": character_id},
            timeout=30,
        )
        image_data = _response_data(image_response)
        details["imageUrlStatus"] = image_response.status_code
        signed_url = str(image_data.get("itemImageUrl") or "")
        if image_response.status_code != 200 or not signed_url:
            return details

        generated_image = requests.get(signed_url, timeout=60)
        details["imageFetchStatus"] = generated_image.status_code
        details["imageContentType"] = generated_image.headers.get(
            "content-type",
            "",
        )
        details["imageBytes"] = len(generated_image.content)
        if generated_image.status_code != 200 or not generated_image.content:
            return details

        send = session.post(
            f"{base_url}/v1/daycare/actions/{action_id}/send",
            json={"characterId": character_id},
            timeout=30,
        )
        send_data = _response_data(send)
        details["sendStatus"] = send.status_code
        details["reactionPresent"] = bool(
            str(send_data.get("characterReaction") or "").strip()
        )
        if send.status_code != 200:
            return details

        final_response = session.get(
            f"{base_url}/v1/daycare/actions/{action_id}",
            params={"character_id": character_id},
            timeout=30,
        )
        details["finalStatus"] = str(
            _response_data(final_response).get("status") or ""
        )
        return details

    def _cleanup(
        self,
        *,
        context: ScenarioContext,
        uid: str,
        character_id: str,
        api_key: str,
        id_token: str,
    ) -> dict[str, Any]:
        cleanup: dict[str, Any] = {"errors": []}
        user_ref = context.firestore.collection("users").document(uid)
        character_ref = (
            context.firestore.collection("characters").document(character_id)
        )

        try:
            corrections: dict[tuple[str, str, str], int] = {}
            for snapshot in user_ref.collection("lumisTransactions").stream():
                data = snapshot.to_dict() or {}
                txn_type = str(data.get("type") or "")
                action = str(data.get("action") or "")
                day = str(data.get("date") or "")
                amount = int(data.get("amount") or 0)
                if txn_type == "earn":
                    field, delta = "minted", amount
                elif txn_type == "spend":
                    field, delta = "burned", -amount
                else:
                    field, delta = "adjusted", amount
                if day and action:
                    key = (day, field, action)
                    corrections[key] = corrections.get(key, 0) + delta
            for (day, field, action), delta in corrections.items():
                context.firestore.collection("lumisEconomy").document(
                    day
                ).update(
                    {f"{field}.{action}": firestore.Increment(-delta)}
                )
            cleanup["economyCorrections"] = len(corrections)
        except Exception as exc:
            cleanup["errors"].append(
                f"economy: {type(exc).__name__}: {exc}"
            )

        try:
            _delete_document_tree(character_ref)
            _delete_document_tree(user_ref)
            cleanup["firestore"] = "deleted"
        except Exception as exc:
            cleanup["errors"].append(
                f"firestore: {type(exc).__name__}: {exc}"
            )

        try:
            storage_client = storage.Client(
                project=context.environment.project
            )
            deleted_objects = 0
            for blob in storage_client.list_blobs(
                "milu-user",
                prefix=f"users/{uid}/daycare/",
            ):
                blob.delete()
                deleted_objects += 1
            cleanup["storageObjectsDeleted"] = deleted_objects
        except Exception as exc:
            cleanup["errors"].append(
                f"storage: {type(exc).__name__}: {exc}"
            )

        try:
            self._delete_test_identity(
                api_key=api_key,
                id_token=id_token,
            )
            cleanup["firebaseAuth"] = "deleted"
        except Exception as exc:
            cleanup["errors"].append(
                f"firebaseAuth: {type(exc).__name__}: {exc}"
            )
        return cleanup

    async def run(self, context: ScenarioContext) -> ScenarioResult:
        started = utc_now_iso()
        base_url = context.environment.daycare_url.rstrip("/")
        api_key = context.environment.firebase_api_key.strip()
        details: dict[str, Any] = {
            "environmentType": context.environment.environment_type,
            "dataMode": context.environment.data_mode,
            "firestoreDatabase": context.environment.firestore_database,
            "actions": [],
        }
        if not base_url or not api_key:
            return ScenarioResult(
                name=self.name,
                success=False,
                started_at=started,
                finished_at=utc_now_iso(),
                summary="Daycare URL or Firebase API key is missing.",
                artifact_dir=str(context.artifact_dir),
                details=details,
            )
        if context.environment.firestore_database != "development":
            return ScenarioResult(
                name=self.name,
                success=False,
                started_at=started,
                finished_at=utc_now_iso(),
                summary="Daycare smoke requires the development database.",
                artifact_dir=str(context.artifact_dir),
                details=details,
            )

        uid = ""
        id_token = ""
        character_id = f"ch_smoke_{uuid.uuid4().hex[:16]}"
        cleanup: dict[str, Any] = {}
        error = ""
        try:
            uid, id_token = self._create_test_identity(api_key=api_key)
            self._seed(
                context=context,
                uid=uid,
                character_id=character_id,
            )
            session = requests.Session()
            session.headers.update(
                {
                    "Authorization": f"Bearer {id_token}",
                    "Content-Type": "application/json",
                }
            )
            for action, description in (
                ("feed", "a tiny strawberry pancake with star sprinkles"),
                ("give_gift", "a soft lavender friendship ribbon"),
            ):
                details["actions"].append(
                    self._run_action(
                        session=session,
                        base_url=base_url,
                        character_id=character_id,
                        daycare_action=action,
                        item_description=description,
                        timeout_seconds=context.args.timeout_seconds,
                    )
                )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if uid and id_token:
                try:
                    cleanup = self._cleanup(
                        context=context,
                        uid=uid,
                        character_id=character_id,
                        api_key=api_key,
                        id_token=id_token,
                    )
                except Exception as exc:
                    cleanup = {
                        "error": f"{type(exc).__name__}: {exc}",
                    }
        details["cleanup"] = cleanup
        if error:
            details["error"] = error

        actions_pass = (
            len(details["actions"]) == 2
            and all(
                item.get("createStatus") == 202
                and item.get("previewStatus") in {"preview_ready", "done"}
                and item.get("imageUrlStatus") == 200
                and item.get("imageFetchStatus") == 200
                and item.get("imageBytes", 0) > 0
                and item.get("sendStatus") == 200
                and item.get("reactionPresent") is True
                and item.get("finalStatus") == "done"
                for item in details["actions"]
            )
        )
        cleanup_pass = (
            cleanup.get("firestore") == "deleted"
            and cleanup.get("firebaseAuth") == "deleted"
            and not cleanup.get("errors")
        )
        success = not error and actions_pass and cleanup_pass
        return ScenarioResult(
            name=self.name,
            success=success,
            started_at=started,
            finished_at=utc_now_iso(),
            summary=(
                "Food and Gift completed with private generated images."
                if success
                else "Food/Gift smoke failed; inspect scenario details."
            ),
            artifact_dir=str(context.artifact_dir),
            details=details,
        )
