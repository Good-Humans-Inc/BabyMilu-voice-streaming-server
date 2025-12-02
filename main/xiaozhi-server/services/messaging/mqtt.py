from __future__ import annotations

import json
import os
from typing import Optional, Tuple

from paho.mqtt import client as mqtt_client

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
    topic = f"xiaozhi/{device_mac}/down"
    payload = {
        "type": "ws_start",
        "wss": ws_url,
        "version": version,
    }

    client = mqtt_client.Client()
    try:
        _log("info", f"Connecting to MQTT broker {host}:{port} for device {device_mac}")
        client.connect(host, port, keepalive=30)
        client.loop_start()
        
        _log("info", f"Publishing ws_start to topic {topic} for device {device_mac}")
        result = client.publish(topic, json.dumps(payload), qos=0)
        
        # Increased timeout from 2.0 to 5.0 seconds for better reliability
        if result.wait_for_publish(5.0):
            ok = result.is_published()
            if ok:
                _log("info", f"Successfully published ws_start to {device_mac}")
            else:
                _log("warning", f"Publish completed but not confirmed for {device_mac}")
            client.loop_stop()
            client.disconnect()
            return ok
        else:
            _log("error", f"MQTT publish timeout (5s) for device {device_mac} on topic {topic}")
            client.loop_stop()
            client.disconnect()
            return False
            
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
    topic = f"xiaozhi/{device_mac}/down"
    payload = {
        "type": "auto_update",
        "url": download_url,
    }

    client = mqtt_client.Client()
    try:
        _log("info", f"Connecting to MQTT broker {host}:{port} for device {device_mac}")
        client.connect(host, port, keepalive=30)
        client.loop_start()

        _log("info", f"Publishing auto_update to topic {topic} for device {device_mac}")
        result = client.publish(topic, json.dumps(payload), qos=0)

        if result.wait_for_publish(5.0):
            ok = result.is_published()
            if ok:
                _log("info", f"Successfully published auto_update to {device_mac}")
            else:
                _log("warning", f"Publish completed but not confirmed for {device_mac}")
            client.loop_stop()
            client.disconnect()
            return ok
        else:
            _log("error", f"MQTT publish timeout (5s) for device {device_mac} on topic {topic}")
            client.loop_stop()
            client.disconnect()
            return False
    except Exception as e:
        _log("error", f"MQTT auto_update publish failed for device {device_mac}: {type(e).__name__}: {e}")
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
        return False
