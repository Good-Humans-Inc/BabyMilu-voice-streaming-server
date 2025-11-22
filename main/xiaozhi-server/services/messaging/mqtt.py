from __future__ import annotations

import json
import os
from typing import Optional, Tuple

from paho.mqtt import client as mqtt_client


def publish_ws_start(
    broker_url: Optional[str],
    device_mac: str,
    ws_url: str,
    version: int = 3,
) -> bool:
    """
    Publish a ws_start control message to the device downlink topic.
    Topic convention: xiaozhi/<MAC>/down
    """
    host, port = _parse_broker(broker_url)
    topic = f"xiaozhi/{device_mac}/down"
    payload = {
        "type": "ws_start",
        "wss": ws_url,
        "version": version,
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


def _parse_broker(broker_url: Optional[str]) -> Tuple[str, int]:
    url = broker_url or os.environ.get("MQTT_URL", "mqtt://localhost:1883")
    if url.startswith("mqtt://"):
        url = url.replace("mqtt://", "tcp://", 1)
    if not url.startswith(("tcp://", "ws://", "wss://", "ssl://")):
        url = "tcp://" + url
    try:
        _, rest = url.split("://", 1)
        if ":" in rest:
            host, port_str = rest.split(":", 1)
            port = int(port_str)
        else:
            host = rest
            port = 1883
        return host, port
    except Exception:
        return "localhost", 1883

