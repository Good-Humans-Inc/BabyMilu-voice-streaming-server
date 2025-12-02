from __future__ import annotations

import json
import os
from typing import Optional, Tuple
import time

from paho.mqtt import client as mqtt_client
from core.utils.mac import normalize_mac

# Try to import logger, but make it optional for backward compatibility
try:
    from services.logging import setup_logging
    _logger = setup_logging()
except Exception:
    _logger = None


def _log(level: str, message: str, *args, **kwargs):
    """Log message if logger is available."""
    if _logger:
        getattr(_logger, level)(message, *args, **kwargs)
    else:
        # Fallback to print if logger not available
        print(f"[{level.upper()}] {message}")


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
        broker_url: MQTT broker URL (e.g., "mqtt://host:1883")
        device_mac: Device MAC address
        ws_url: WebSocket URL to send to device
        version: WebSocket protocol version
        
    Returns:
        True if publish succeeded, False otherwise
    """
    host, port = _parse_broker(broker_url)
    normalized_mac = normalize_mac(device_mac or "")
    topic = f"xiaozhi/{normalized_mac}/down"
    payload = {
        "type": "ws_start",
        "wss": ws_url,
        "version": version,
    }

    # Unique, explicit client-id with device suffix for traceability
    cid = f"serverpub-{(device_mac or '').replace(':','')}-{int(time.time()*1000)}"
    client = mqtt_client.Client(client_id=cid, clean_session=True)
    try:
        _log("info", f"Connecting to MQTT broker {host}:{port} for device {device_mac}")
        client.connect(host, port, keepalive=30)
        client.loop_start()
        
        _log("info", f"Publishing ws_start to topic {topic} for device {device_mac}")
        result = client.publish(topic, json.dumps(payload), qos=0)
        # QoS 0 is fire-and-forget. Avoid false negatives from is_published timing.
        result.wait_for_publish(1.0)
        client.loop_stop()
        client.disconnect()
        return True
            
    except ConnectionRefusedError as e:
        _log("error", f"MQTT connection refused to {host}:{port} for device {device_mac}: {e}")
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
        return False
    except TimeoutError as e:
        _log("error", f"MQTT connection timeout to {host}:{port} for device {device_mac}: {e}")
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
        return False
    except Exception as e:
        _log("error", f"MQTT publish failed for device {device_mac}: {type(e).__name__}: {e}")
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


def publish_auto_update(
    broker_url: Optional[str],
    device_mac: str,
    download_url: str,
) -> bool:
    """
    Publish an auto_update control message to the device downlink topic.
    Topic convention: xiaozhi/<MAC>/down

    Args:
        broker_url: MQTT broker URL (e.g., "mqtt://host:1883"). Falls back to env MQTT_URL.
        device_mac: Device MAC address (keep exact casing/format the device subscribes with)
        download_url: Public URL to mega.bin/device.bin the firmware should download

    Returns:
        True if publish succeeded, False otherwise
    """
    host, port = _parse_broker(broker_url)
    normalized_mac = normalize_mac(device_mac or "")
    topic = f"xiaozhi/{normalized_mac}/down"
    payload = {
        "type": "auto_update",
        "url": download_url,
    }

    # Unique, explicit client-id with device suffix for traceability
    cid = f"serverpub-{(device_mac or '').replace(':','')}-{int(time.time()*1000)}"
    client = mqtt_client.Client(client_id=cid, clean_session=True)
    try:
        _log("info", f"Connecting to MQTT broker {host}:{port} for device {device_mac}")
        client.connect(host, port, keepalive=30)
        client.loop_start()

        _log("info", f"Publishing auto_update to topic {topic} for device {device_mac}")
        result = client.publish(topic, json.dumps(payload), qos=0)
        # QoS 0 is fire-and-forget. Avoid false negatives from is_published timing.
        result.wait_for_publish(1.0)
        client.loop_stop()
        client.disconnect()
        return True
    except Exception as e:
        _log("error", f"MQTT auto_update publish failed for device {device_mac}: {type(e).__name__}: {e}")
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
        return False
