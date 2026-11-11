"""X (Twitter) publisher – EXPERIMENTAL, written from the X API docs and not
yet tested with a real account.

Uses POST https://api.x.com/2/tweets with OAuth 1.0a user-context keys, which
don't expire. X bills every post (pay-per-use); posts containing a link have
been priced far higher than plain posts, so the link is optional.
"""

from __future__ import annotations

import unicodedata

from ..http import TIMEOUT, make_session
from ..models import Item, PublishResult, Rendered
from ..oauth1 import auth_header
from ..plugins import Field, Publisher, register_publisher
from ..text import URL_RE, compose, graphemes

API = "https://api.x.com/2/tweets"

# twitter-text v3: these ranges weigh 1, everything else 2; emoji weigh 2;
# every URL counts as 23. The limit is 280 weighted characters.
_LIGHT = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))


def _is_emoji(g: str) -> bool:
    for ch in g:
        cp = ord(ch)
        if cp >= 0x1F000 or cp in (0xFE0F, 0x200D) or 0x2600 <= cp <= 0x27BF \
                or unicodedata.category(ch) == "So":
            return True
    return False


def x_measure(text: str) -> int:
    total = 0
    pos = 0
    for m in URL_RE.finditer(text):
        total += _weigh(text[pos:m.start()]) + 23
        pos = m.end()
    return total + _weigh(text[pos:])


def _weigh(s: str) -> int:
    n = 0
    for g in graphemes(s):
        if _is_emoji(g):
            n += 2
            continue
        for ch in g:
            cp = ord(ch)
            n += 1 if any(a <= cp <= b for a, b in _LIGHT) else 2
    return n


@register_publisher
class XPublisher(Publisher):
    name = "twitter"
    label = "X (Twitter)"
    description = "Posts to an X account. X charges for each post; see the link setting."
    experimental = True
    char_limit = 280
    fields = [
        Field("api_key", "API key (consumer key)", "password", secret=True, default=""),
        Field("api_secret", "API key secret", "password", secret=True, default=""),
        Field("access_token", "Access token", "password", secret=True, default=""),
        Field("access_secret", "Access token secret", "password", secret=True, default="",
              help="From console.x.com → your app → Keys and tokens. Set the app's permissions to "
                   "Read and write BEFORE generating the access token."),
        Field("include_link", "Include the link", "bool", default=True,
              help="X has charged much more for posts that contain a link. Turn off to post text only."),
    ]

    def __init__(self, settings, context):
        super().__init__(settings, context)
        self.http = make_session()

    def measure(self, text: str) -> int:
        return x_measure(text)

    def render(self, item: Item) -> Rendered:
        r = compose(item, self.char_limit, self.measure,
                    include_url=bool(self.settings.get("include_link", True) and item.url))
        r.card = None    # X builds its own preview from the link in the text
        return r

    def _auth(self, method: str, url: str) -> str:
        s = self.settings
        missing = [k for k in ("api_key", "api_secret", "access_token", "access_secret") if not s.get(k)]
        if missing:
            raise RuntimeError("X keys are not all set: " + ", ".join(missing))
        return auth_header(method, url, consumer_key=s["api_key"], consumer_secret=s["api_secret"],
                           token=s["access_token"], token_secret=s["access_secret"])

    def publish(self, item: Item, rendered: Rendered) -> PublishResult:
        r = self.http.post(API, json={"text": rendered.text}, timeout=TIMEOUT,
                           headers={"Authorization": self._auth("POST", API)})
        if not r.ok:
            raise RuntimeError(f"X: HTTP {r.status_code} {r.text[:300]}")
        pid = (r.json().get("data") or {}).get("id", "")
        return PublishResult(post_id=pid, post_url=f"https://x.com/i/web/status/{pid}" if pid else "")

    def check(self) -> str:
        url = "https://api.x.com/2/users/me"
        r = self.http.get(url, headers={"Authorization": self._auth("GET", url)}, timeout=TIMEOUT)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        return f"Signed in as @{(r.json().get('data') or {}).get('username')}"
