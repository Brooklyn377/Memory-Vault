"""Web access for the Assistant: search the web and read a page. Off until you turn it on under Settings > Web access.

This container makes the requests (not the model's server), so every address is checked first: only http and https,
and only addresses on the public internet. Anything on your local network, this machine, or a link-local address
(including the cloud-metadata address) is refused, also after a redirect, and the address is checked at the moment of
connecting. Requests carry no cookies, no API keys and no proxy. Page text comes back as plain text and is marked as
untrusted. Search queries are scrubbed of keys and tokens like everything else that leaves a model."""
import html
import http.client
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import agent
from scrub import scrub

FILE = agent.STATE / "web_access.json"
DEFAULT = {"enabled": False, "engine": "duckduckgo", "searxng_url": "", "models": {"pa": True}}
DDG_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = "Mozilla/5.0 (compatible; memory-agent/1.0; +https://github.com/Brooklyn377/Memory-Vault)"
MAX_BYTES = 1_500_000
TIMEOUT = 15
RESULTS = 6
MODELS_WITH_TOOLS = ("pa",)   # only the Assistant has tools; the other models would have nothing to use it with


# ---------- settings ----------
def config():
    try:
        d = json.loads(FILE.read_text())
    except (OSError, ValueError):
        d = {}
    return {**DEFAULT, **(d if isinstance(d, dict) else {}), "models": {**DEFAULT["models"], **((d or {}).get("models") or {})}}


def save_config(d):
    c = config()
    engine = d.get("engine", c["engine"])
    if engine not in ("duckduckgo", "searxng"):
        raise ValueError("the search engine is DuckDuckGo or SearXNG")
    url = str(d.get("searxng_url", c["searxng_url"]) or "").strip().rstrip("/")
    if engine == "searxng":
        p = urllib.parse.urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise ValueError("type the SearXNG address, for example http://192.168.1.60:8888")
    new = {"enabled": bool(d.get("enabled", c["enabled"])), "engine": engine, "searxng_url": url,
           "models": {m: bool((d.get("models") or {}).get(m, c["models"].get(m, False))) for m in MODELS_WITH_TOOLS}}
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(new, indent=1))
    os.replace(tmp, FILE)
    agent.log(f"settings: web access {'on' if new['enabled'] else 'off'} ({engine})")
    return new


def allowed(model):
    c = config()
    return bool(c["enabled"] and c["models"].get(model))


# ---------- a connection that only reaches the public internet ----------
def _public(ip):
    a = ipaddress.ip_address(ip.split("%")[0])
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified)


class _Guard:
    """Resolves the name once, refuses unless every address is public, and connects to one of those addresses."""
    def _public_socket(self):
        infos = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        if not infos or not all(_public(i[4][0]) for i in infos):
            raise ValueError(f"{self.host} is not on the public internet, so it is not opened")
        return socket.create_connection(infos[0][4][:2], self.timeout)


class _HTTP(_Guard, http.client.HTTPConnection):
    def connect(self):
        self.sock = self._public_socket()


class _HTTPS(_Guard, http.client.HTTPSConnection):
    def connect(self):
        self.sock = self._context.wrap_socket(self._public_socket(), server_hostname=self.host)


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTP, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPS, req, context=self._context)


# Its own opener: no proxy from the environment, no cookies, and not the one that adds a model server's API key.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPHandler, _HTTPSHandler)


def _get(url, data=None, accept="text/html,text/plain,application/json;q=0.9,*/*;q=0.1"):
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("only plain http and https addresses are opened")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with _opener.open(req, timeout=TIMEOUT) as r:
            body = r.read(MAX_BYTES + 1)
            return r.geturl(), r.headers.get_content_type(), r.headers.get_content_charset() or "utf-8", body[:MAX_BYTES], len(body) > MAX_BYTES
    except urllib.error.HTTPError as e:
        raise ValueError(f"the site answered {e.code} {e.reason}")
    except urllib.error.URLError as e:
        raise ValueError(str(e.reason) if isinstance(e.reason, ValueError) else f"could not reach it ({e.reason})")
    except (OSError, ValueError) as e:
        raise ValueError(str(e) if isinstance(e, ValueError) else f"could not reach it ({type(e).__name__})")


# ---------- reading a page as text ----------
class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "template", "iframe"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre", "table", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.title, self.skip, self.in_title = [], "", 0, False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data)


def page_text(body, ctype, charset):
    """-> (title, text) of a fetched page; a binary type is refused."""
    text = body.decode(charset if charset else "utf-8", errors="replace")
    if ctype in ("text/html", "application/xhtml+xml"):
        t = _Text()
        t.feed(text)
        lines = [" ".join(l.split()) for l in "".join(t.out).split("\n")]
        return " ".join(t.title.split()), "\n".join(l for l in lines if l)
    if ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
        return "", text
    raise ValueError(f"that is a {ctype} file, not a page of text")


# ---------- the two tools ----------
def search(query):
    """-> [{"title","url","snippet"}], and the engine's name. Raises ValueError with a plain reason."""
    q = scrub(" ".join(str(query or "").split()))[0][:200]
    if not q:
        raise ValueError("give the words to search for")
    c = config()
    if c["engine"] == "searxng":
        url, ctype, cs, body, _ = _get(c["searxng_url"] + "/search?" + urllib.parse.urlencode({"q": q, "format": "json"}), accept="application/json")
        try:
            rows = json.loads(body.decode(cs, errors="replace")).get("results") or []
        except ValueError:
            raise ValueError("that SearXNG did not answer in JSON; its settings must allow format=json")
        out = [{"title": " ".join(str(r.get("title", "")).split())[:120], "url": str(r.get("url", "")),
                "snippet": " ".join(str(r.get("content", "")).split())[:240]} for r in rows if str(r.get("url", "")).startswith(("http://", "https://"))]
        return out[:RESULTS], "SearXNG"
    url, ctype, cs, body, _ = _get(DDG_URL, data=urllib.parse.urlencode({"q": q}).encode())
    page = body.decode(cs, errors="replace")
    out = []
    for m in re.finditer(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*>', page):
        tag = m.group(0)
        href = re.search(r'href="([^"]+)"', tag)
        end = page.find("</a>", m.end())
        if not href or end < 0:
            continue
        link = html.unescape(href.group(1))
        if link.startswith("//"):
            link = "https:" + link
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        link = qs["uddg"][0] if "uddg" in qs else link
        if not link.startswith(("http://", "https://")):
            continue
        sn = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', page[end:end + 4000], re.S)
        out.append({"title": " ".join(html.unescape(re.sub(r"<[^>]+>", "", page[m.end():end])).split())[:120], "url": link,
                    "snippet": " ".join(html.unescape(re.sub(r"<[^>]+>", "", sn.group(1))).split())[:240] if sn else ""})
        if len(out) >= RESULTS:
            break
    return out, "DuckDuckGo"


def open_page(url, start=0, limit=6000):
    """-> (title, final address, text from `start`, whether more follows)."""
    final, ctype, cs, body, cut = _get(str(url or "").strip())
    title, text = page_text(body, ctype, cs)
    start = max(0, int(start or 0))
    more = len(text) > start + limit or cut
    return title, final, text[start:start + limit], more, len(text)
