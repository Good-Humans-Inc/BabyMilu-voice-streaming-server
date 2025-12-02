from __future__ import annotations


def normalize_mac(mac: str) -> str:
    """
    Normalize a MAC address to colon-separated lowercase format.
    Accepts inputs with or without separators, and with '-' or ':'.
    Examples:
        "30:ED:A0:AD:A0:DC" -> "30:ed:a0:ad:a0:dc"
        "30-ED-A0-AD-A0-DC" -> "30:ed:a0:ad:a0:dc"
        "30eda0ada0dc"      -> "30:ed:a0:ad:a0:dc"
    """
    if not isinstance(mac, str):
        return mac
    compact = mac.strip().lower().replace("-", "").replace(":", "")
    if not compact:
        return compact
    # Reinsert ':' every two characters
    return ":".join(compact[i : i + 2] for i in range(0, len(compact), 2))


