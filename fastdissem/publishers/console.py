"""Writes posts to the log instead of a network. Handy for testing."""

from __future__ import annotations

import logging

from ..models import Item, PublishResult, Rendered
from ..plugins import Field, Publisher, register_publisher

log = logging.getLogger("fastdissem.console")


@register_publisher
class ConsolePublisher(Publisher):
    name = "console"
    label = "Console (log only)"
    description = "Prints each post to the log. Useful for testing new sources."
    fields = [Field("char_limit", "Pretend character limit", "int", default=300)]

    def __init__(self, settings, context):
        super().__init__(settings, context)
        self.char_limit = int(settings.get("char_limit") or 300)
        self.include_url_in_text = True

    def publish(self, item: Item, rendered: Rendered) -> PublishResult:
        log.info("POST (%d/%d) %s\n%s", rendered.length, rendered.limit, item.uid, rendered.text)
        return PublishResult(post_id=item.uid)
