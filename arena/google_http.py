"""Google AI mode over plain HTTP — no browser window, no DOM scraping.

Google's AI mode page is not a chat widget: the document that comes back for
``/search?...&udm=50`` is only a shell, and the answer itself is fetched by the
page from ``/async/folwr``. This module does the same two requests itself:

1. ``GET /search?q=<query>&udm=50&hl=<hl>`` — the shell. It carries the
   per-page tokens the page would use (``data-srtst``, ``data-garc``,
   ``data-lro-token``, ``data-lro-signature``, ``data-ei``,
   ``data-xsrf-folwr-token``).
2. ``GET /async/folwr?<those tokens>&q=<query>`` — the rendered answer.

The answer arrives as HTML fragments, so it is converted back to markdown
(headings, paragraphs, lists, tables) and the sources are pulled out of the
same payload. Nothing is invented: if Google returns no answer, the caller
gets an error and says so.

Cookies are the person's own Google session (``SID``, ``SSID``,
``__Secure-1PSID``, …). Google answers only when the session is valid; without
it the module reports ``signed_out`` instead of pretending.
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
import time
import urllib.parse
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

LOG = logging.getLogger("arena.google.http")

SEARCH_URL = "https://www.google.com/search"
FOLWR_URL = "https://www.google.com/async/folwr"
THREADS_URL = "https://www.google.com/httpservice/web/AimThreadsService/ListThreads"
HOME_URL = "https://www.google.com/"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}
DOC_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,image/apng,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
ASYNC_HEADERS = {
    "Accept": "*/*",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# Cookie names that mean "this is a real signed-in Google session".
AUTH_COOKIES = ("SID", "SSID", "__Secure-1PSID", "__Secure-3PSID",
                "HSID", "APISID", "SAPISID")
DEFAULT_TIMEOUT = 45.0
MAX_QUERY = 1800          # Google answers long questions, but the URL is a URL.


class GoogleHTTPError(RuntimeError):
    """A turn that could not be completed, with a code the UI can explain."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# cookies
# --------------------------------------------------------------------------
def cookie_pairs(raw: str) -> List[Tuple[str, str]]:
    """``name=value; name2=value2`` → pairs, ignoring anything malformed."""
    out: List[Tuple[str, str]] = []
    for chunk in str(raw or "").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip()
        if not name or " " in name:
            continue
        out.append((name, value.strip()))
    return out


def has_auth_cookie(raw: str) -> bool:
    return any(name in AUTH_COOKIES for name, _ in cookie_pairs(raw))


def _clean_cookie_header(raw: str) -> str:
    """Keep only the pairs, so a pasted header never carries stray text."""
    return "; ".join(f"{k}={v}" for k, v in cookie_pairs(raw))


# --------------------------------------------------------------------------
# a small, tolerant HTML tree (stdlib only: the frozen build has no lxml)
# --------------------------------------------------------------------------
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "param", "source", "track", "wbr"}


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: Optional[Dict[str, str]] = None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: List[object] = []
        self.parent = parent

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<_Node {self.tag} {len(self.children)}>"

    @property
    def classes(self) -> set:
        return set((self.attrs.get("class") or "").split())


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = _Node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(data)


def parse_html(text: str) -> _Node:
    builder = _TreeBuilder()
    try:
        builder.feed(text or "")
        builder.close()
    except Exception:  # pragma: no cover - HTMLParser is very forgiving
        LOG.debug("html parse stopped early", exc_info=True)
    return builder.root


def _walk(node: _Node):
    for child in node.children:
        if isinstance(child, _Node):
            yield child
            yield from _walk(child)


def _squash(text: str) -> str:
    return " ".join(str(text or "").replace("\xa0", " ").split())


# --------------------------------------------------------------------------
# HTML fragments → markdown
# --------------------------------------------------------------------------
_INLINE_TAGS = {
    "strong": "**", "b": "**", "mark": "**",
    "em": "_", "i": "_",
    "code": "`", "kbd": "`",
}


def _render_inline(node, bold: bool = False) -> str:
    """Text of a node with Google's inline markup turned into markdown."""
    if isinstance(node, str):
        return node.replace("\xa0", " ")
    if not isinstance(node, _Node):
        return ""
    if node.tag in ("script", "style", "svg", "img", "noscript"):
        return ""
    classes = node.classes
    # A citation chip: <span class="dNwgB"><span>euromed.ru</span><span>+3</span></span>
    if "dNwgB" in classes:
        label = ""
        for sub in node.children:
            if isinstance(sub, _Node) and "QNca8b" in sub.classes:
                label = _squash(_render_inline(sub))
                break
        return f" [{label}]" if label else ""
    if node.tag == "br":
        return "\n"
    marker = _INLINE_TAGS.get(node.tag)
    inner = "".join(_render_inline(child, bold or marker == "**")
                    for child in node.children)
    if marker:
        if not inner.strip():
            return inner
        # Google nests <mark><strong><span>, which would become ****text****.
        if marker == "**":
            return inner if bold else f"**{inner.strip()}**"
        return f"{marker}{inner.strip()}{marker}"
    if node.tag == "a":
        href = str(node.attrs.get("href") or "")
        label = _squash(inner)
        if href.startswith("http") and label:
            return f"[{label}]({href})"
        return inner
    return inner


def _block_text(node: _Node) -> str:
    text = _render_inline(node)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    out: List[str] = []
    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1] != "":
            out.append("")
    return "\n".join(out).strip()


def _list_markdown(node: _Node, ordered: bool = False) -> str:
    lines: List[str] = []
    index = 1
    for child in node.children:
        if not isinstance(child, _Node) or child.tag != "li":
            continue
        text = _block_text(child).replace("\n", " ")
        if not text:
            continue
        bullet = f"{index}." if ordered else "-"
        lines.append(f"{bullet} {text}")
        index += 1
    return "\n".join(lines)


def _table_markdown(node: _Node) -> str:
    rows: List[List[str]] = []
    for tr in _walk(node):
        if tr.tag != "tr":
            continue
        cells = [c for c in tr.children
                 if isinstance(c, _Node) and c.tag in ("td", "th")]
        if not cells:
            continue
        values = [_block_text(c).replace("\n", " ").replace("|", "\\|") for c in cells]
        if any(values):
            rows.append(values)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "| " + " | ".join(["---"] * width) + " |"]
    for row in rows[1:]:
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


_ANSWER_HINT = re.compile(r"n6owBd")


def _answer_root(root: _Node) -> Optional[_Node]:
    """The deepest node that still contains every answer block."""
    nodes = [n for n in _walk(root) if _ANSWER_HINT.search(n.attrs.get("class") or "")]
    if not nodes:
        return None
    common = nodes[0]
    for node in nodes[1:]:
        while common is not None and not _contains(common, node):
            common = common.parent
    return common


def _contains(parent: _Node, node: _Node) -> bool:
    cur = node
    while cur is not None:
        if cur is parent:
            return True
        cur = cur.parent
    return False


def answer_markdown(body: str) -> str:
    """Turn the ``/async/folwr`` payload into readable markdown."""
    root = parse_html(body)
    scope = _answer_root(root) or root
    blocks: List[str] = []
    heading_ids = set()

    def add(text: str):
        text = (text or "").strip()
        if text:
            blocks.append(text)

    def visit(node: _Node):
        for child in node.children:
            if isinstance(child, str):
                continue
            classes = child.classes
            if child.tag in ("script", "style"):
                continue
            if "IQTBxd" in classes:      # sources card: everything after it is chrome
                return
            role = child.attrs.get("role")
            if child.tag == "div" and role == "heading":
                level = child.attrs.get("aria-level") or "3"
                try:
                    # Google numbers the answer's own title as level 2, so the
                    # section headings it gives us are one step deeper than
                    # they read in a chat message.
                    level_n = max(2, min(6, int(level) - 1))
                except ValueError:
                    level_n = 2
                text = _block_text(child)
                if text:
                    heading_ids.add(id(child))
                    blocks.append("#" * level_n + " " + text.replace("\n", " "))
                continue
            if "n6owBd" in classes:
                add(_block_text(child))
                continue
            if child.tag in ("ul", "ol"):
                text = _list_markdown(child, ordered=child.tag == "ol")
                add(text)
                continue
            if child.tag == "table":
                add(_table_markdown(child))
                continue
            if "AHmQrc" in classes:
                text = _block_text(child)
                if text:
                    add("*" + text.replace("\n", " ") + "*")
                continue
            visit(child)

    visit(scope)
    text = "\n\n".join(blocks)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Google bolds the trailing period as its own <strong>, which would leave
    # "**term**" + "**.**" glued together as ****.
    while "****" in text:
        text = text.replace("****", "")
    return text.strip()


_SOURCE_RE = re.compile(
    r'\["((?:[^"\\]|\\.){2,300})",null,"[^"]*",'
    r'"(https?://[^"]+)",\["([^"]*)"\],'
    r'"((?:[^"\\]|\\.){2,600})"')


def _json_unescape(text: str) -> str:
    try:
        return json.loads('"' + text + '"')
    except Exception:
        return text.replace('\\"', '"').replace("\\\\", "\\")


def answer_sources(body: str, limit: int = 24) -> List[dict]:
    """Titles + URLs of the pages Google used, in the order Google lists them."""
    plain = _html.unescape(body or "")
    out: List[dict] = []
    seen = set()
    for match in _SOURCE_RE.finditer(plain):
        title, domain, label, url = match.groups()
        url = _json_unescape(url)
        if not url.startswith("http"):
            continue
        if "gstatic.com" in domain or "googleusercontent.com" in domain:
            continue
        key = url.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "title": _squash(_json_unescape(title))[:200],
            "domain": _squash(label or domain)[:80],
            "url": url[:600],
        })
        if len(out) >= limit:
            break
    return out


def html_unescape_text(body: str) -> str:
    return _html.unescape(body or "")


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------
_SORRY_MARKERS = (
    "/sorry/",
    "unusual traffic",
    "подозрительный трафик",
    "необычный трафик",
    "g-recaptcha",
    "recaptcha/api2",
)


# Google's own refusal when the "query" is an instruction rather than a
# question: it prints this instead of an answer.
_DECLINE_MARKERS = (
    "не удается дать ответ",
    "не удаётся дать ответ",
    "попробуйте спросить что-нибудь другое",
    "wasn't able to answer",
    "was not able to answer",
    "try asking something else",
    "isn't able to answer",
)


def _declined_page(body: str) -> bool:
    """Did Google answer with "I cannot answer that query" instead of content?"""
    low = (body or "").lower()
    return any(marker in low for marker in _DECLINE_MARKERS)


def _sorry_page(html: str) -> bool:
    """Google's 'I think you are a robot' page, in any language we have seen."""
    low = (html or "").lower()
    return any(marker in low for marker in _SORRY_MARKERS)


def _attr(html: str, name: str) -> Optional[str]:
    match = re.search(r'%s="([^"]+)"' % re.escape(name), html)
    return match.group(1) if match else None


def page_tokens(html: str) -> Dict[str, Optional[str]]:
    """Everything ``/async/folwr`` needs, taken from the search page itself."""
    plain = _html.unescape(html or "")
    return {
        "ei": _attr(plain, "data-ei"),
        "garc": _attr(plain, "data-garc"),
        "mlro": _attr(plain, "data-lro-token"),
        "mlros": _attr(plain, "data-lro-signature"),
        "srtst": _attr(plain, "data-srtst"),
        "xsrf": _attr(plain, "data-xsrf-folwr-token"),
    }


def folwr_params(query: str, tokens: Dict[str, Optional[str]], hl: str) -> Dict[str, str]:
    params = {
        "srtst": tokens.get("srtst") or "",
        "garc": tokens.get("garc") or "",
        "mlro": tokens.get("mlro") or "",
        "mlros": tokens.get("mlros") or "",
        "ei": tokens.get("ei") or "",
        "q": query,
        "yv": "3",
        "hl": hl,
        "udm": "50",
        "stkp": "0",
        "cs": "0",
        "async": "_fmt:adl,_xsrf:" + (tokens.get("xsrf") or ""),
    }
    return {k: v for k, v in params.items() if v}


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------
_ACCOUNT_PREFIX = re.compile(
    r"^\s*(?:Google Account|Аккаунт Google|Google-аккаунт|Cuenta de Google)\s*:?", re.I)


def _account_label(body: str) -> dict:
    """Email and name out of the account button Google prints in the header."""
    label = ""
    for pattern in (r'aria-label="([^"]{2,160})"[^>]{0,400}?SignOutOptions',
                    r'SignOutOptions[^>]{0,400}?aria-label="([^"]{2,160})"'):
        match = re.search(pattern, body)
        if match:
            label = _html.unescape(match.group(1))
            break
    if not label:
        return {}
    label = _squash(label)
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", label)
    out = {"email": email.group(0) if email else ""}
    name = label.replace(email.group(0), "") if email else label
    name = _ACCOUNT_PREFIX.sub("", name)
    name = re.sub(r"[()]", " ", name)
    out["name"] = _squash(name)[:80]
    return out


class GoogleHTTP:
    """One Google AI mode turn per :meth:`ask`, straight over HTTPS."""

    def __init__(self, cookie: str, *, hl: str = "ru", timeout: float = DEFAULT_TIMEOUT,
                 session=None):
        self.cookie = _clean_cookie_header(cookie)
        if not self.cookie:
            raise GoogleHTTPError("no_cookie", "No Google cookies are saved.")
        if not has_auth_cookie(self.cookie):
            raise GoogleHTTPError(
                "no_auth_cookie",
                "These cookies carry no Google session (SID, SSID, __Secure-1PSID). "
                "Copy the whole Cookie request header from DevTools → Network.")
        self.hl = (hl or "ru").strip() or "ru"
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.session = session or self._new_session()

    # -- plumbing --------------------------------------------------------
    def _new_session(self):
        import requests  # imported lazily so tests can inject their own session
        session = requests.Session()
        session.headers.update(BASE_HEADERS)
        return session

    def _get(self, url: str, *, params=None, headers=None, allow_redirects=True):
        merged = dict(BASE_HEADERS)
        merged["Cookie"] = self.cookie
        merged.update(headers or {})
        try:
            response = self.session.get(url, params=params, headers=merged,
                                        timeout=self.timeout,
                                        allow_redirects=allow_redirects)
        except Exception as exc:  # network problems are reported, never hidden
            raise GoogleHTTPError("network", f"Google did not answer: {type(exc).__name__}: {exc}")
        return response

    # -- public ----------------------------------------------------------
    def account(self) -> dict:
        """Who is signed in, according to Google itself (no window needed)."""
        response = self._get(HOME_URL, headers=DOC_HEADERS)
        body = response.text or ""
        signed_in = "SignOutOptions" in body
        info = {"signed_in": signed_in, "email": "", "name": "", "source": "http",
                "status": response.status_code}
        if signed_in:
            info.update(_account_label(body))
        return info

    def ask(self, query: str) -> dict:
        """One AI mode answer: ``{"text", "sources", "chars", "elapsed"}``."""
        query = _squash(query)
        if not query:
            raise GoogleHTTPError("empty", "The message is empty.")
        if len(query) > MAX_QUERY:
            query = query[:MAX_QUERY].rsplit(" ", 1)[0]
        started = time.time()
        page = self._get(SEARCH_URL, params={"q": query, "udm": "50", "hl": self.hl},
                         headers=DOC_HEADERS)
        if page.status_code != 200:
            raise GoogleHTTPError("http_" + str(page.status_code),
                                  f"Google answered with HTTP {page.status_code}.")
        html = page.text or ""
        if "SignOutOptions" not in html and "accounts.google.com/ServiceLogin" in html:
            raise GoogleHTTPError(
                "signed_out",
                "Google does not see a signed-in session: the saved cookies are "
                "expired. Paste a fresh Cookie header.")
        tokens = page_tokens(html)
        if not tokens.get("srtst") or not tokens.get("garc"):
            if "enablejs" in html or "если переадресация не будет выполнена" in html:
                raise GoogleHTTPError(
                    "throttled",
                    "Google answered with its JavaScript interstitial instead of the "
                    "results page: this session is being rate-limited. Wait a minute "
                    "and send again.")
            if _sorry_page(html):
                raise GoogleHTTPError(
                    "human_check",
                    "Google wants a human check for this network ('unusual traffic'). "
                    "Open the Google window, pass the check there, then send again.")
            raise GoogleHTTPError(
                "no_tokens",
                "Google returned a page without AI mode tokens — it may be showing "
                "a consent or 'unusual traffic' check for this session.")
        stream = self._get(FOLWR_URL, params=folwr_params(query, tokens, self.hl),
                           headers=ASYNC_HEADERS)
        body = stream.text or ""
        text = answer_markdown(body)
        if not text:
            if _declined_page(body):
                raise GoogleHTTPError(
                    "declined",
                    "Google AI mode declined this query: it only answers questions, and "
                    "it refuses prompts that try to give it a tool protocol. Send it a "
                    "normal question instead.")
            raise GoogleHTTPError(
                "no_answer",
                "Google returned the page but no AI mode answer: the session may "
                "have been asked for a check, or AI mode is off for this account.")
        sources = answer_sources(body)
        return {
            "text": text,
            "sources": sources,
            "chars": len(text),
            "elapsed": round(time.time() - started, 1),
            "query": query,
        }

    def threads(self, limit: int = 40) -> List[dict]:
        """The person's own AI mode threads (title + a URL that reopens one)."""
        response = self._get(THREADS_URL, params={
            "hl": self.hl, "udm": "50", "reqpld": "[null,null,0]",
            "msc": "gwsclient", "opi": "89978449"})
        body = response.text or ""
        start = body.find("[")
        if start < 0:
            raise GoogleHTTPError("no_threads", "Google returned no thread list.")
        try:
            data = json.loads(body[start:])
        except ValueError as exc:
            raise GoogleHTTPError("no_threads", f"The thread list was not JSON: {exc}")
        items: List[dict] = []
        seen = set()

        def walk(node):
            if isinstance(node, list):
                if (len(node) >= 3 and isinstance(node[0], list) and len(node[0]) == 2
                        and isinstance(node[0][0], str) and isinstance(node[0][1], str)
                        and node[0][0].startswith("AUtExf")
                        and isinstance(node[1], str) and node[1].strip()):
                    mtid = node[0][1]
                    title = _squash(node[1])[:120]
                    if mtid not in seen:
                        seen.add(mtid)
                        url = SEARCH_URL + "?" + urllib.parse.urlencode(
                            {"q": title, "udm": "50", "hl": self.hl, "mtid": mtid})
                        items.append({"title": title, "url": url, "mtid": mtid})
                for child in node:
                    walk(child)

        walk(data)
        return items[:limit]


def thread_answer(cookie: str, mtid: str, query: str, *, hl: str = "ru",
                  session=None) -> dict:
    """Read one saved thread back out of Google (used by the history import)."""
    client = GoogleHTTP(cookie, hl=hl, session=session)
    page = client._get(SEARCH_URL, params={"q": query, "udm": "50", "hl": hl,
                                           "mtid": mtid}, headers=DOC_HEADERS)
    tokens = page_tokens(page.text or "")
    if not tokens.get("srtst"):
        raise GoogleHTTPError("no_tokens", "That thread did not open.")
    stream = client._get(FOLWR_URL, params=folwr_params(query, tokens, hl),
                         headers=ASYNC_HEADERS)
    body = stream.text or ""
    return {"text": answer_markdown(body), "sources": answer_sources(body)}
