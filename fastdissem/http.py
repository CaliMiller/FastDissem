"""Shared HTTP session with sane timeouts and retries."""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import APP_NAME, __version__

TIMEOUT = 20


def make_session(user_agent: str | None = None) -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({"GET"}), respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = user_agent or f"{APP_NAME}/{__version__}"
    return s
