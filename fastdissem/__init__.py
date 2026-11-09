"""Fast Dissem (Fast Dissemination).

Watches content sources (YouTube channels, NWS alerts, ...) and posts new
items to social networks (Bluesky, Mastodon, ...). Sources and publishers are
plug-ins: drop a module into ``fastdissem/sources`` or
``fastdissem/publishers`` and it is picked up automatically, including its
settings form in the web UI.
"""

__version__ = "1.0.0"
APP_NAME = "FastDissem"
