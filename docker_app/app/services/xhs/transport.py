from __future__ import annotations

import requests


def direct_session() -> requests.Session:
    """XHS requests never inherit site, environment, or system proxy settings."""
    session = requests.Session()
    session.trust_env = False
    session.proxies.clear()
    return session
