"""docker-compose.yml's published ports (decision #35): every service binds
to 127.0.0.1 except the dashboard. A regression here is silent — the stack
works exactly the same, it's just reachable from the whole Wi-Fi — so it's
pinned by a test instead of left to review.

Reads the file as text, not YAML: PyYAML isn't a dependency of this project,
and the ports blocks are simple enough that a line scan is exact.
"""

from __future__ import annotations

import re
from pathlib import Path

COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
LAN_VISIBLE = {"ui"}  # the dashboard is demoed on the LAN on purpose


def _published_ports() -> dict[str, list[str]]:
    ports: dict[str, list[str]] = {}
    service = None
    in_ports = False
    for line in COMPOSE_FILE.read_text().splitlines():
        if re.match(r"^\S", line):  # top-level key: services/volumes/...
            service, in_ports = None, False
            continue
        if m := re.match(r"^  ([a-z][\w-]*):\s*$", line):
            service, in_ports = m.group(1), False
            continue
        if re.match(r"^    ports:\s*$", line):
            in_ports = True
            continue
        if in_ports and (m := re.match(r'^      - "([^"]+)"', line)):
            ports.setdefault(service, []).append(m.group(1))
            continue
        if re.match(r"^    \S", line):  # next key of the same service
            in_ports = False
    return ports


def test_internal_services_publish_on_localhost_only():
    ports = _published_ports()
    assert {"postgres", "redis", "qdrant", "api"} <= ports.keys()
    for service, mappings in ports.items():
        if service in LAN_VISIBLE:
            continue
        for mapping in mappings:
            assert mapping.startswith("127.0.0.1:"), f"{service} publishes {mapping!r} on every interface"


def test_dashboard_stays_reachable_on_the_lan():
    assert _published_ports()["ui"] == ["8501:8501"]


def test_dashboard_reaches_the_api_over_the_compose_network():
    """Localhost binding only affects ports published on the Mac; the ui
    container must keep using the service name, never a published port."""
    assert "API_URL: http://api:8000" in COMPOSE_FILE.read_text()
