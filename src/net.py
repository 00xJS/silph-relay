"""Small HTTP helpers on the standard library.

Everything the relay sends is a GET, a JSON POST or one multipart upload, and
urllib covers all three — so the relay carries no third-party packages and a
run installs nothing. That keeps PyPI out of the hot path: on 2026-09-22 a PyPI
hiccup failed a run before it fetched a single post.
"""
import json
import urllib.error
import urllib.request
import uuid
import zlib

# FxTwitter rejects an empty User-Agent and may block generic ones — and we
# share GitHub runner egress IPs with everyone else — so say who we are.
USER_AGENT = "silph-relay/2.0 (+https://github.com/00xJS/silph-relay)"

# Never buffer more than this from one response (images are the big ones).
MAX_BODY_BYTES = 20 * 1024 * 1024


class Response:
    """A finished request: status, headers and body, whatever the status code."""

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers  # case-insensitive, e.g. headers.get("Retry-After")
        self.body = body

    @property
    def ok(self):
        return 200 <= self.status < 300

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body)


def _gunzip(body):
    """Decompress a gzip body without letting a hostile one expand unbounded."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(body, MAX_BODY_BYTES + 1)
    if len(out) > MAX_BODY_BYTES or d.unconsumed_tail:
        raise ValueError("decompressed response too large")
    return out


def request(url, method="GET", headers=None, data=None, timeout=15):
    """Send one request and return a Response.

    HTTP error statuses (4xx/5xx) come back as a Response for the caller to
    judge. Network failures — DNS, refused connection, TLS, timeout — raise,
    the same way `requests` did.
    """
    all_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    all_headers.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        resp = e  # an HTTPError is also the response: status, headers and body
    with resp:
        body = resp.read(MAX_BODY_BYTES + 1) or b""
        if len(body) > MAX_BODY_BYTES:
            raise ValueError(f"response larger than {MAX_BODY_BYTES} bytes")
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            body = _gunzip(body)
        return Response(resp.getcode(), resp.headers, body)


def multipart(fields, files):
    """Encode a multipart/form-data body.

    fields: [(name, text, content_type or None)]
    files:  [(name, filename, data_bytes, content_type)]
    Returns (body_bytes, content_type_header).
    """
    boundary = uuid.uuid4().hex
    parts = []
    for name, value, ctype in fields:
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n'
        if ctype:
            head += f"Content-Type: {ctype}\r\n"
        parts += [(head + "\r\n").encode(), value.encode("utf-8"), b"\r\n"]
    for name, filename, data, ctype in files:
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n')
        parts += [head.encode(), data, b"\r\n"]
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
