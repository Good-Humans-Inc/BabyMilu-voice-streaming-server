import os
import json
import re
from typing import Optional
from paho.mqtt import client as mqtt_client


def _normalize_device_id(device_id: str) -> str:
    """
    Normalize common MAC input formats for MQTT topics.

    Accepts colon, dash, underscore, or separator-free MAC strings and returns
    colon-separated lowercase. Non-MAC identifiers are returned lowercased.
    """
    if not isinstance(device_id, str):
        return device_id
    raw = device_id.strip()
    if not raw:
        return raw

    compact = re.sub(r"[^0-9A-Fa-f]", "", raw)
    if len(compact) == 12 and re.fullmatch(r"[0-9A-Fa-f]{12}", compact):
        compact = compact.lower()
        return ":".join(compact[i : i + 2] for i in range(0, 12, 2))

    return raw.lower()


def publish_ws_start(
    broker_url: Optional[str],
    device_mac: str,
    ws_url: str,
    version: int = 3,
) -> bool:
    """
    Publish a ws_start control message to the device downlink topic.
    Topic convention: xiaozhi/<MAC>/down

    Args:
        broker_url: e.g. 'mqtt://localhost:1883' or 'tcp://host:1883'. If None, read MQTT_URL env or default to 'mqtt://localhost:1883'
        device_mac: device identifier (MAC as used by firmware topics)
        ws_url: websocket url to connect, e.g. ws://host:8000/xiaozhi/v1/
        version: protocol framing version hint (3 by default)
    """
    url = broker_url or os.environ.get("MQTT_URL", "mqtt://localhost:1883")
    # Normalize scheme and parse host/port
    if url.startswith("mqtt://"):
        url = url.replace("mqtt://", "tcp://", 1)
    if not url.startswith(("tcp://", "ws://", "wss://", "ssl://")):
        url = "tcp://" + url
    # Parse host and port
    try:
        scheme, rest = url.split("://", 1)
        if ":" in rest:
            host, port_str = rest.split(":", 1)
            port = int(port_str)
        else:
            host = rest
            port = 1883
    except Exception:
        host, port = "localhost", 1883

    normalized_device_id = _normalize_device_id(device_mac)
    topic = f"xiaozhi/{normalized_device_id}/down"
    payload = {
        "type": "ws_start",
        "wss": ws_url,
        "version": version,
        # "token": ""  # no JWT for simplest flow
    }

    client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
    try:
        client.connect(host, port, keepalive=30)
        client.loop_start()
        result = client.publish(topic, json.dumps(payload), qos=1)
        result.wait_for_publish(2.0)
        ok = result.is_published()
        client.loop_stop()
        client.disconnect()
        return ok
    except Exception:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
        return False



def publish_down_command(
    broker_url: Optional[str],
    device_mac: str,
    payload: dict,
) -> bool:
    """
    Publish a JSON command to device downlink topic.
    Topic: xiaozhi/<MAC>/down
    """
    url = broker_url or os.environ.get("MQTT_URL", "mqtt://localhost:1883")
    if url.startswith("mqtt://"):
        url = url.replace("mqtt://", "tcp://", 1)
    if not url.startswith(("tcp://", "ws://", "wss://", "ssl://")):
        url = "tcp://" + url
    # Parse host and port
    try:
        scheme, rest = url.split("://", 1)
        if ":" in rest:
            host, port_str = rest.split(":", 1)
            port = int(port_str)
        else:
            host = rest
            port = 1883
    except Exception:
        host, port = "localhost", 1883

    normalized_device_id = _normalize_device_id(device_mac)
    topic = f"xiaozhi/{normalized_device_id}/down"
    client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
    try:
        client.connect(host, port, keepalive=30)
        client.loop_start()
        result = client.publish(topic, json.dumps(payload), qos=1)
        result.wait_for_publish(2.0)
        ok = result.is_published()
        client.loop_stop()
        client.disconnect()
        return ok
    except Exception:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
        return False
