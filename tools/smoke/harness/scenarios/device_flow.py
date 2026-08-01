from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import ssl
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt
import psycopg
import requests
from google.cloud import storage

from ..context import ScenarioContext
from ..models import ScenarioResult, utc_now_iso
from ..scenario import BaseScenario


_GIF_1X1 = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _data(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    nested = payload.get("data")
    return nested if isinstance(nested, dict) else payload


class _MqttObserver:
    def __init__(self, device_id: str, host: str) -> None:
        self.device_id = device_id
        self.host = host
        self.port = int(os.environ.get("BABYMILU_SMOKE_MQTT_PORT", "1883"))
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.connected = threading.Event()
        try:
            self.client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
                client_id=f"codex-device-delivery-{uuid.uuid4().hex[:8]}",
            )
        except (AttributeError, TypeError):
            self.client = mqtt.Client(
                client_id=f"codex-device-delivery-{uuid.uuid4().hex[:8]}"
            )
        username = os.environ.get("BABYMILU_SMOKE_MQTT_USERNAME", "")
        password = os.environ.get("BABYMILU_SMOKE_MQTT_PASSWORD", "")
        if username:
            self.client.username_pw_set(username, password)
        if os.environ.get("BABYMILU_SMOKE_MQTT_TLS", "").lower() in {
            "1", "true", "yes"
        }:
            self.client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, _userdata, _flags, rc) -> None:
        if rc == 0:
            client.subscribe(f"xiaozhi/{self.device_id}/down", qos=1)
            self.connected.set()

    def _on_message(self, _client, _userdata, message) -> None:
        raw = message.payload.decode("utf-8", errors="replace")
        try:
            payload: Any = json.loads(raw)
        except ValueError:
            payload = raw
        self.events.put(
            {"topic": message.topic, "qos": message.qos, "payload": payload}
        )

    def start(self) -> None:
        self.client.connect(self.host, self.port, keepalive=30)
        self.client.loop_start()
        if not self.connected.wait(10):
            self.stop()
            raise RuntimeError("MQTT observer could not connect and subscribe")

    def wait_for_animation(self, timeout_seconds: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                event = self.events.get(timeout=min(1, deadline - time.monotonic()))
            except queue.Empty:
                continue
            payload = event.get("payload")
            if isinstance(payload, dict) and payload.get("command") == "remote_anim_update":
                return event
        raise TimeoutError("No remote_anim_update MQTT command was observed")

    def stop(self) -> None:
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass


class DevicePairAndDeliverAnimationScenario(BaseScenario):
    name = "device.pair_and_deliver_animation"
    description = (
        "Prove authenticated atomic pairing, universal Supabase character "
        "refresh, bundle/checksum upload, MQTT notification, and download."
    )

    def _create_identity(self, api_key: str) -> tuple[str, str]:
        response = requests.post(
            "https://identitytoolkit.googleapis.com/v1/accounts:signUp",
            params={"key": api_key},
            json={
                "email": f"codex-device-flow-{uuid.uuid4().hex}@example.invalid",
                "password": uuid.uuid4().hex + uuid.uuid4().hex,
                "returnSecureToken": True,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        return str(payload["localId"]), str(payload["idToken"])

    def _create_user_and_character(
        self, context: ScenarioContext, uid: str, token: str
    ) -> str:
        headers = {"Authorization": f"Bearer {token}"}
        user = requests.post(
            f"{context.environment.user_api_url.rstrip('/')}/v3/user",
            headers=headers,
            json={"uid": uid, "phoneNumber": uid, "name": "Device Flow Smoke"},
            timeout=30,
        )
        if user.status_code not in {200, 201}:
            raise AssertionError(f"user creation failed with HTTP {user.status_code}")
        character = requests.post(
            f"{context.environment.character_api_url.rstrip('/')}/v3/character",
            headers=headers,
            json={
                "uid": uid,
                "slotIndex": 0,
                "profile": {
                    "name": "Device Flow Milu",
                    "personality": "curious release-smoke character",
                },
            },
            timeout=45,
        )
        if character.status_code not in {200, 201}:
            raise AssertionError(
                f"character creation failed with HTTP {character.status_code}"
            )
        character_id = str(_data(character).get("characterId") or "")
        if not character_id:
            raise AssertionError("character API did not return characterId")
        context.firestore.collection("users").document(uid).set(
            {
                "mainCharacterId": character_id,
                "activeCharacterId": character_id,
                "smokeTest": True,
            },
            merge=True,
        )
        context.firestore.collection("characters").document(character_id).set(
            {"smokeTest": True}, merge=True
        )
        return character_id

    def _wait_for_supabase(self, character_ids: list[str], timeout: int) -> dict[str, Any]:
        database_url = os.environ.get("BABYMILU_SMOKE_DATABASE_URL", "").strip()
        if not database_url:
            raise RuntimeError("BABYMILU_SMOKE_DATABASE_URL is required")
        deadline = time.monotonic() + timeout
        rows: dict[str, Any] = {}
        while time.monotonic() < deadline:
            with psycopg.connect(database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "select character_id, owner_user_id, memory_state, "
                        "next_starter, updated_at from public.character_memory_model "
                        "where character_id = any(%s)",
                        (character_ids,),
                    )
                    rows = {
                        str(row[0]): {
                            "ownerUserId": str(row[1] or ""),
                            "memoryStatePresent": bool(row[2]),
                            "nextStarterPresent": bool(row[3]),
                            "updatedAt": str(row[4]),
                        }
                        for row in cursor.fetchall()
                    }
            if all(
                character_id in rows and rows[character_id]["nextStarterPresent"]
                for character_id in character_ids
            ):
                return rows
            time.sleep(2)
        raise TimeoutError(
            "Supabase character_memory_model did not refresh for every character"
        )

    def _cleanup_supabase(self, character_ids: list[str]) -> None:
        database_url = os.environ.get("BABYMILU_SMOKE_DATABASE_URL", "").strip()
        if not database_url:
            return
        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "delete from public.character_memory_model "
                    "where character_id = any(%s)",
                    (character_ids,),
                )
            connection.commit()

    def _cleanup_supabase_audio(self, character_ids: list[str]) -> int:
        base_url = os.environ.get("BABYMILU_SMOKE_SUPABASE_URL", "").rstrip("/")
        service_key = os.environ.get(
            "BABYMILU_SMOKE_SUPABASE_SERVICE_ROLE_KEY", ""
        ).strip()
        if not base_url or not service_key:
            raise RuntimeError(
                "Supabase URL and service-role key are required for audio cleanup"
            )
        headers = {
            "Authorization": f"Bearer {service_key}",
            "apikey": service_key,
            "Content-Type": "application/json",
        }
        object_names: list[str] = []
        for character_id in character_ids:
            listing = requests.post(
                f"{base_url}/storage/v1/object/list/next-starter-audio",
                headers=headers,
                json={"prefix": f"{character_id}/", "limit": 1000},
                timeout=30,
            )
            listing.raise_for_status()
            for item in listing.json():
                name = str(item.get("name") or "")
                if not name:
                    continue
                object_names.append(f"{character_id}/{name}")
        if object_names:
            deletion = requests.delete(
                f"{base_url}/storage/v1/object/next-starter-audio",
                headers=headers,
                json={"prefixes": object_names},
                timeout=30,
            )
            deletion.raise_for_status()
        return len(object_names)

    def _cleanup_storage(
        self, client: storage.Client, bucket_name: str, prefixes: list[str]
    ) -> int:
        deleted = 0
        for prefix in prefixes:
            for blob in client.list_blobs(bucket_name, prefix=prefix):
                blob.delete()
                deleted += 1
        return deleted

    async def run(self, context: ScenarioContext) -> ScenarioResult:
        started = utc_now_iso()
        details: dict[str, Any] = {
            "environmentType": context.environment.environment_type,
            "dataMode": context.environment.data_mode,
            "firestoreDatabase": context.environment.firestore_database,
            "assertions": {},
            "cleanup": {"errors": []},
        }
        required = {
            "firebaseApiKey": context.environment.firebase_api_key,
            "deviceApiUrl": context.environment.device_api_url,
            "userApiUrl": context.environment.user_api_url,
            "characterApiUrl": context.environment.character_api_url,
            "deviceBinUrl": context.environment.device_bin_url,
            "mqttHost": context.environment.mqtt_host,
        }
        missing = [name for name, value in required.items() if not value]
        if context.environment.firestore_database != "development":
            missing.append("firestoreDatabase=development")
        if missing:
            return ScenarioResult(
                name=self.name,
                success=False,
                started_at=started,
                finished_at=utc_now_iso(),
                summary=f"Missing device-flow configuration: {', '.join(missing)}",
                artifact_dir=str(context.artifact_dir),
                details=details,
            )

        run_id = uuid.uuid4().hex[:10]
        device_id = ":".join(
            f"{value:02x}" for value in bytes.fromhex(f"02{run_id}")
        )
        uid = token = character_id = ""
        no_device_character_id = f"ch_no_device_{run_id}"
        bucket_name = context.environment.device_assets_bucket
        source_prefix = ""
        device_prefix = f"device_bin/{device_id}/"
        storage_client = storage.Client(project=context.environment.project)
        observer: _MqttObserver | None = None
        success = False
        error = ""
        try:
            uid, token = self._create_identity(context.environment.firebase_api_key)
            character_id = self._create_user_and_character(context, uid, token)
            details.update(
                {"uid": uid, "deviceId": device_id, "characterId": character_id}
            )
            source_prefix = f"users/{uid}/characters/{character_id}/"
            storage_client.bucket(bucket_name).blob(
                f"{source_prefix}normal.gif"
            ).upload_from_string(_GIF_1X1, content_type="image/gif")

            context.firestore.collection("characters").document(
                no_device_character_id
            ).set(
                {
                    "characterId": no_device_character_id,
                    "ownerUid": uid,
                    "uid": uid,
                    "profile": {"name": "No Device Refresh Milu"},
                    "voice": "smoke-voice",
                    "smokeTest": True,
                    "updatedAt": datetime.now(timezone.utc),
                }
            )
            context.firestore.collection("characters").document(character_id).set(
                {
                    "profile": {
                        "name": "Device Flow Milu Updated",
                        "personality": "profile refresh proof",
                    },
                    "voice": "smoke-voice-updated",
                    "updatedAt": datetime.now(timezone.utc),
                },
                merge=True,
            )

            headers = {"Authorization": f"Bearer {token}"}
            unauthenticated = requests.post(
                f"{context.environment.device_api_url.rstrip('/')}/v3/device/claim",
                json={"deviceId": device_id},
                timeout=30,
            )
            if unauthenticated.status_code != 401:
                raise AssertionError("device claim did not reject missing Firebase auth")
            claim = requests.post(
                f"{context.environment.device_api_url.rstrip('/')}/v3/device/claim",
                headers=headers,
                json={"deviceId": device_id, "name": "Codex E2E Device"},
                timeout=30,
            )
            if claim.status_code != 201:
                raise AssertionError(f"device claim failed with HTTP {claim.status_code}")
            claim_data = _data(claim)
            binding = claim_data.get("binding") or {}
            if claim_data.get("activeCharacterId") != character_id:
                raise AssertionError("claim did not derive active character server-side")
            if binding.get("verified") is not True or binding.get("uid") != uid:
                raise AssertionError("claim did not return a verified atomic binding")
            device_doc = context.firestore.collection("devices").document(device_id).get()
            user_doc = context.firestore.collection("users").document(uid).get()
            device_data = device_doc.to_dict() or {}
            user_data = user_doc.to_dict() or {}
            if device_data.get("ownerUid") != uid:
                raise AssertionError("device document owner pointer is missing")
            if user_data.get("currentDeviceId") != device_id:
                raise AssertionError("user document currentDeviceId is missing")
            details["assertions"]["atomicClaim"] = True

            replay = requests.post(
                f"{context.environment.device_api_url.rstrip('/')}/v3/device/claim",
                headers=headers,
                json={"deviceId": device_id, "name": "Codex E2E Device"},
                timeout=30,
            )
            if replay.status_code != 200:
                raise AssertionError("idempotent device claim replay failed")
            details["assertions"]["idempotentClaim"] = True

            supabase_rows = self._wait_for_supabase(
                [character_id, no_device_character_id],
                timeout=max(90, context.args.timeout_seconds),
            )
            if any(row["ownerUserId"] != uid for row in supabase_rows.values()):
                raise AssertionError("Supabase character row has the wrong owner")
            details["supabase"] = supabase_rows
            details["assertions"]["deviceCharacterRefresh"] = True
            details["assertions"]["nonDeviceCharacterRefresh"] = True

            observer = _MqttObserver(device_id, context.environment.mqtt_host)
            observer.start()
            generation = requests.post(
                context.environment.device_bin_url,
                headers=headers,
                json={"deviceId": device_id, "characterId": character_id},
                timeout=max(120, context.args.timeout_seconds),
            )
            generation_data = _data(generation)
            details["generation"] = {
                "status": generation.status_code,
                "errorCode": generation_data.get("errorCode"),
                "deliveryState": generation_data.get("deliveryState"),
                "serviceStatus": generation_data.get("status"),
                "message": generation_data.get("message"),
                "manifestPresent": bool(generation_data.get("manifestUrl")),
                "contentSha256Present": bool(
                    generation_data.get("contentSha256")
                ),
            }
            if generation.status_code not in {200, 202}:
                raise AssertionError(
                    "device bundle generation failed: "
                    f"HTTP {generation.status_code} "
                    f"{generation_data.get('errorCode') or ''}".strip()
                )
            if not generation_data.get("manifestUrl") or not generation_data.get(
                "contentSha256"
            ):
                raise AssertionError(
                    "generator returned success HTTP without the required "
                    "manifestUrl and contentSha256"
                )

            bucket = storage_client.bucket(bucket_name)
            binary_blob = bucket.blob(f"{device_prefix}test.bin")
            checksum_blob = bucket.blob(f"{device_prefix}test.bin.sha256")
            binary = binary_blob.download_as_bytes()
            checksum = checksum_blob.download_as_text().strip().lower()
            digest = hashlib.sha256(binary).hexdigest()
            if not binary or not _SHA256.fullmatch(checksum) or checksum != digest:
                raise AssertionError("test.bin or its SHA-256 sidecar is invalid")
            details["bundle"] = {
                "bytes": len(binary),
                "sha256": digest,
                "generation": str(binary_blob.generation or ""),
            }
            details["assertions"]["bundleAndChecksum"] = True

            mqtt_event = observer.wait_for_animation(
                timeout_seconds=max(30, context.args.timeout_seconds)
            )
            payload = mqtt_event["payload"]
            if payload.get("deviceId") != device_id or payload.get("sha256") != digest:
                raise AssertionError("MQTT command does not match the generated bundle")
            asset_url = str(payload.get("assetUrl") or "")
            if not asset_url.startswith("https://storage.googleapis.com/"):
                raise AssertionError("MQTT command did not use an HTTPS GCS asset URL")
            download = requests.get(asset_url, timeout=60)
            download.raise_for_status()
            if hashlib.sha256(download.content).hexdigest() != digest:
                raise AssertionError("simulated device download failed checksum validation")
            details["mqtt"] = {
                "topic": mqtt_event["topic"],
                "qos": mqtt_event["qos"],
                "command": payload.get("command"),
                "revision": payload.get("revision"),
                "generation": payload.get("generation"),
            }
            details["assertions"]["mqttAfterUpload"] = True
            details["assertions"]["simulatedDeviceDownload"] = True
            success = True
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            details["failure"] = error
        finally:
            if observer is not None:
                observer.stop()
            try:
                prefixes = [prefix for prefix in (source_prefix, device_prefix) if prefix]
                details["cleanup"]["gcsObjectsDeleted"] = self._cleanup_storage(
                    storage_client, bucket_name, prefixes
                )
            except Exception as exc:
                details["cleanup"]["errors"].append(f"gcs: {type(exc).__name__}: {exc}")
            try:
                cleanup_character_ids = [
                    value for value in (character_id, no_device_character_id) if value
                ]
                details["cleanup"]["supabaseAudioObjectsDeleted"] = (
                    self._cleanup_supabase_audio(cleanup_character_ids)
                )
            except Exception as exc:
                details["cleanup"]["errors"].append(
                    f"supabase-storage: {type(exc).__name__}: {exc}"
                )
            try:
                cleanup_character_ids = [
                    value for value in (character_id, no_device_character_id) if value
                ]
                self._cleanup_supabase(cleanup_character_ids)
                details["cleanup"]["supabase"] = "deleted"
            except Exception as exc:
                details["cleanup"]["errors"].append(
                    f"supabase-db: {type(exc).__name__}: {exc}"
                )
            try:
                if uid:
                    context.firestore.collection("users").document(uid).collection(
                        "devices"
                    ).document(device_id).delete()
                    context.firestore.collection("devices").document(device_id).delete()
                    for value in (character_id, no_device_character_id):
                        if value:
                            context.firestore.collection("characters").document(value).delete()
                    context.firestore.collection("users").document(uid).delete()
                details["cleanup"]["firestore"] = "deleted"
            except Exception as exc:
                details["cleanup"]["errors"].append(
                    f"firestore: {type(exc).__name__}: {exc}"
                )
            try:
                if token:
                    deletion = requests.post(
                        "https://identitytoolkit.googleapis.com/v1/accounts:delete",
                        params={"key": context.environment.firebase_api_key},
                        json={"idToken": token},
                        timeout=30,
                    )
                    deletion.raise_for_status()
                details["cleanup"]["firebaseAuth"] = "deleted"
            except Exception as exc:
                details["cleanup"]["errors"].append(
                    f"firebaseAuth: {type(exc).__name__}: {exc}"
                )

        context.artifact_writer.write_json(
            "scenario-details.json", details, context.artifact_dir
        )
        return ScenarioResult(
            name=self.name,
            success=success,
            started_at=started,
            finished_at=utc_now_iso(),
            summary=(
                "Complete authenticated device pairing and animation delivery passed."
                if success
                else f"Device pairing/delivery proof failed closed: {error}"
            ),
            artifact_dir=str(context.artifact_dir),
            details=details,
        )
