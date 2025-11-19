import os
import json
from typing import Optional
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

    topic = f"xiaozhi/{device_mac}/down"
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


