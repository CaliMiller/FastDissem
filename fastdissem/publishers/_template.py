"""TEMPLATE for a new social network (files starting with "_" are not loaded).

1. Copy this file to e.g. ``facebook.py`` in this folder.
2. Fill in name/label/fields and implement ``publish`` (and ``check``).
3. Restart Fast Dissem. The network appears in the web UI with a settings
   form built from ``fields``; enable it there.

Everything else (duplicate detection per network, retries with back-off,
dry-run mode, fitting text to ``char_limit``, hashtags) is handled for you.
``rendered.text`` is already trimmed to your limit; ``rendered.card`` holds
the link preview (url, title, description, thumb_url) if the item has one.

Working examples to copy from: mastodon.py (simplest), facebook.py (form
post + link), twitter.py (signed requests, custom length counting),
threads.py / instagram.py (two-step publish, auto-renewed tokens, and
accepts() to skip items a network can't post).
"""

from __future__ import annotations

from ..models import Item, PublishResult, Rendered
from ..plugins import Field, Publisher, register_publisher


# @register_publisher            # <- uncomment in your copy
class ExamplePublisher(Publisher):
    name = "example"             # unique key used in settings and the database
    label = "Example Network"
    description = "What this network is."
    char_limit = 500
    include_url_in_text = True   # False if the network renders rendered.card itself
    fields = [
        Field("token", "Access token", "password", secret=True, default=""),
    ]

    def publish(self, item: Item, rendered: Rendered) -> PublishResult:
        # resp = requests.post(..., json={"text": rendered.text}, timeout=20)
        # resp.raise_for_status()   # raising = failed, will be retried
        raise NotImplementedError

    def check(self) -> str:
        return "OK"
