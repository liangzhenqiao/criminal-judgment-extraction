#!/usr/bin/env python3
"""Refetch judgment texts from China Judgments Online by case number.

The deposit does not ship the full texts. Judgments can be retracted after
publication, and they carry personal information (names, ID numbers, card
numbers, addresses). Every case number with a platform link is kept in
``id_to_cjo_url.csv.gz`` instead, so any text can be fetched again.

Two platform facts drive this script:

* The judgment arrives through the JSON endpoint behind the detail page, so
  that endpoint is the route. The page's own HTML is only a template on this
  platform (it holds placeholders, not the text), which is why a plain GET
  alone yields nothing useful; it stays as a fallback in case the endpoint is
  ever moved.
* Since 2024 the platform requires a signed-in account to read a text. Sign in
  in a browser, then pass the session cookie with ``--cookie`` (the raw
  ``Cookie`` header, or ``@/path/to/cookie.txt``). Without a cookie the page
  still comes back, but only as a shell without the text.

The platform has an anti-crawling layer and rate limits. The default wait is
3 seconds between cases. Raise ``--delay`` when the platform starts answering
403 or 429; keep the default when in doubt.

Case numbers carry Chinese characters and full-width parentheses, so the
request URL is percent-encoded before it is sent. Document URLs are otherwise
used exactly as the map holds them; the ``docId`` is not assumed to have a
particular length or alphabet. Requests are made as a browser would make
them.

The script stores plain text only. It does not parse the HTML and does not
extract fields. A successful response must be non-empty and must contain
``判决书`` or ``裁定书``.

Failures are retried with exponential backoff and then appended to
``<out-dir>/failures.csv`` (``case_no,url,error,attempts,utc``); the run
continues with the next case. The script exits 0 when it ran, even if every
case failed, and exits 2 on a usage error.

Usage::

    python3 examples/refetch_texts.py \\
        --url-map id_to_cjo_url.csv.gz --ids my_cases.txt \\
        --out-dir out/texts --cookie @cookie.txt

    python3 examples/refetch_texts.py \\
        --url-map id_to_cjo_url.csv.gz --ids my_cases.txt \\
        --out-dir out/texts --dry-run
"""

import argparse
import base64
import csv
import datetime
import gzip
import html
import html.parser
import http.cookiejar
import itertools
import json
import pathlib
import random
import re
import string
import sys
import time
import urllib.parse
import urllib.request

DEFAULT_USER_AGENT = (
    "criminal-judgment-extraction/1.0 "
    "(reproduction; +https://github.com/liangzhenqiao/criminal-judgment-extraction)"
)
MARKERS = ("判决书", "裁定书")
API_URL = "https://wenshu.court.gov.cn/website/parse/rest.q4w"
AUTHORIZE_URL = "https://wenshu.court.gov.cn/tongyiLogin/authorize"
API_CFG = "com.lawyee.judge.dc.parse.dto.SearchDataDsoDTO@docInfoSearch"
ORIGIN = "https://wenshu.court.gov.cn"
ALNUM = string.digits + string.ascii_letters


class FetchError(Exception):
    pass


# ---------------------------------------------------------------------------
# Triple DES in pure Python (the endpoint key is 24 ASCII characters and the
# initialization vector is the current date as yyyyMMdd, as in the page's own
# JavaScript). Tables are the standard DES tables.
# ---------------------------------------------------------------------------

IP = [
    58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4,
    62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
    57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
    61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7,
]
FP = [
    40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31,
    38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
    36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
    34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25,
]
E = [
    32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11,
    12, 13, 12, 13, 14, 15, 16, 17, 16, 17, 18, 19, 20, 21, 20, 21,
    22, 23, 24, 25, 24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1,
]
P = [
    16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
    2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25,
]
PC1 = [
    57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2,
    59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36, 63, 55, 47, 39,
    31, 23, 15, 7, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37,
    29, 21, 13, 5, 28, 20, 12, 4,
]
PC2 = [
    14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4,
    26, 8, 16, 7, 27, 20, 13, 2, 41, 52, 31, 37, 47, 55, 30, 40,
    51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32,
]
SHIFTS = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1]
SBOX = [
    [14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7,
     0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0,
     15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13],
    [15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10,
     3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15,
     13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9],
    [10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8,
     13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7,
     1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12],
    [7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
     13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4,
     3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14],
    [2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9,
     14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14,
     11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3],
    [12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11,
     10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6,
     4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13],
    [4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1,
     13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2,
     6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12],
    [13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7,
     1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8,
     2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11],
]


def _bits(data):
    out = []
    for byte in data:
        out.extend((byte >> (7 - i)) & 1 for i in range(8))
    return out


def _bytes(bits):
    out = bytearray()
    for i in range(0, len(bits), 8):
        value = 0
        for bit in bits[i:i + 8]:
            value = (value << 1) | bit
        out.append(value)
    return bytes(out)


def _permute(block, table):
    return [block[index - 1] for index in table]


def _subkeys(key):
    key_bits = _permute(_bits(key), PC1)
    left, right = key_bits[:28], key_bits[28:]
    keys = []
    for shift in SHIFTS:
        left = left[shift:] + left[:shift]
        right = right[shift:] + right[:shift]
        keys.append(_permute(left + right, PC2))
    return keys


def _des(block, keys):
    block = _permute(block, IP)
    left, right = block[:32], block[32:]
    for key in keys:
        expanded = [a ^ b for a, b in zip(_permute(right, E), key)]
        substituted = []
        for i in range(8):
            chunk = expanded[i * 6:(i + 1) * 6]
            row = (chunk[0] << 1) | chunk[5]
            column = (chunk[1] << 3) | (chunk[2] << 2) | (chunk[3] << 1) | chunk[4]
            value = SBOX[i][row * 16 + column]
            substituted.extend(
                ((value >> 3) & 1, (value >> 2) & 1, (value >> 1) & 1, value & 1)
            )
        left, right = right, [a ^ b for a, b in zip(left, _permute(substituted, P))]
    return _permute(right + left, FP)


def _triple_encrypt(block, k1, k2, k3):
    block = _des(block, k1)
    block = _des(block, list(reversed(k2)))
    return _des(block, k3)


def _triple_decrypt(block, k1, k2, k3):
    block = _des(block, list(reversed(k3)))
    block = _des(block, k2)
    return _des(block, list(reversed(k1)))


def des3_encrypt(plaintext, key, iv):
    data = plaintext.encode("utf-8")
    pad = 8 - len(data) % 8
    data += bytes([pad]) * pad
    key, iv_bytes = key.encode(), iv.encode()
    k1, k2, k3 = _subkeys(key[0:8]), _subkeys(key[8:16]), _subkeys(key[16:24])
    previous = iv_bytes
    out = bytearray()
    for i in range(0, len(data), 8):
        block = bytes(a ^ b for a, b in zip(data[i:i + 8], previous))
        previous = _bytes(_triple_encrypt(_bits(block), k1, k2, k3))
        out += previous
    return bytes(out)


def des3_decrypt(ciphertext, key, iv):
    key, iv_bytes = key.encode(), iv.encode()
    k1, k2, k3 = _subkeys(key[0:8]), _subkeys(key[8:16]), _subkeys(key[16:24])
    previous = iv_bytes
    out = bytearray()
    for i in range(0, len(ciphertext), 8):
        block = ciphertext[i:i + 8]
        plain = _bytes(_triple_decrypt(_bits(block), k1, k2, k3))
        out += bytes(a ^ b for a, b in zip(plain, previous))
        previous = block
    return out[:-out[-1]].decode("utf-8")


# ---------------------------------------------------------------------------
# Command line and input files
# ---------------------------------------------------------------------------


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Refetch judgment texts by case number from a case-number/URL map."
    )
    parser.add_argument(
        "--url-map",
        required=True,
        type=pathlib.Path,
        help="gzipped CSV with columns id,url (id is the case number)",
    )
    parser.add_argument(
        "--ids",
        type=pathlib.Path,
        help="case numbers to fetch: one per line, or a CSV with an id column",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        type=pathlib.Path,
        help="directory for <case_no>.txt and failures.csv",
    )
    parser.add_argument(
        "--cookie",
        help="Cookie header from a signed-in browser, or @/path/to/cookie.txt",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="maximum number of cases to process (required without --ids)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="seconds between requests (default: 3)",
    )
    parser.add_argument(
        "--jitter",
        type=float,
        default=0.5,
        help="random +/- share applied to --delay (default: 0.5)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="per-request timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="retries after a failed request (default: 2)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print case_no<TAB>url and do not touch the network or the disk",
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="skip cases whose output file exists and is non-empty (default: on)",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="User-Agent header",
    )
    args = parser.parse_args(argv)

    if not args.url_map.is_file():
        parser.error(f"--url-map is not a file: {args.url_map}")
    if args.ids is not None and not args.ids.is_file():
        parser.error(f"--ids is not a file: {args.ids}")
    if args.ids is None and args.limit is None:
        parser.error("--limit is required when --ids is omitted")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.delay < 0:
        parser.error("--delay must not be negative")
    if args.jitter < 0:
        parser.error("--jitter must not be negative")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.retries < 0:
        parser.error("--retries must not be negative")
    if args.out_dir.exists() and not args.out_dir.is_dir():
        parser.error(f"--out-dir exists and is not a directory: {args.out_dir}")
    if args.cookie is not None:
        try:
            args.cookie = read_cookie(args.cookie)
        except OSError as exc:
            if not args.dry_run:
                parser.error(f"cannot read --cookie: {exc}")
            print(f"ignoring unreadable --cookie in dry run: {exc}", file=sys.stderr)
            args.cookie = None
        if args.cookie is not None and not args.cookie:
            parser.error("--cookie is empty")
    return args


def read_cookie(value):
    if value.startswith("@"):
        raw = pathlib.Path(value[1:]).read_bytes()
        try:
            return raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            # Chrome stores some cookies as raw bytes; a header carries them as is.
            return raw.decode("latin-1").strip()
    return value.strip()


def norm_case(value):
    """Strip, decode HTML entities and drop control characters.

    One map id is entity-escaped (``&#x8;`` prefix), which decodes to a
    backspace. Removing it lets a plain case number match that row.
    """
    text = html.unescape(value)
    text = "".join(char for char in text if char >= " " or char == "\t")
    return text.strip()


def load_ids(path):
    """Return the requested case numbers in order, without duplicates."""
    lines = [
        line
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    if not lines:
        return []
    rows = list(csv.reader(lines))
    header = [cell.strip().lower() for cell in rows[0]]
    column = header.index("id") if "id" in header else 0
    if "id" in header:
        rows = rows[1:]
    seen = set()
    ordered = []
    for row in rows:
        if len(row) <= column:
            continue
        case_no = norm_case(row[column])
        if case_no and case_no not in seen:
            seen.add(case_no)
            ordered.append(case_no)
    return ordered


def read_url_map(path, wanted):
    """Yield (case_no, url) from the map, in file order.

    The header is accepted with an ``id`` column and a ``url`` or ``cjo_url``
    column; a file without a header is read as columns 0 and 1.
    """
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            first = next(reader)
        except StopIteration:
            return
        if first and first[0].strip().lower() == "id":
            header = [cell.strip().lower() for cell in first]
            url_column = next(
                (i for i, name in enumerate(header) if name in ("url", "cjo_url")),
                1,
            )
            rows = reader
        else:
            url_column = 1
            rows = itertools.chain([first], reader)

        for row in rows:
            if len(row) <= url_column:
                continue
            case_no = norm_case(row[0])
            url = row[url_column].strip()
            if not case_no or not url:
                continue
            if wanted is not None and case_no not in wanted:
                continue
            yield case_no, url


def safe_filename(case_no):
    """Make a case number usable as one file name component."""
    name = "".join(
        "／" if char in "/\\" else char
        for char in case_no
        if char >= " " or char == "\t"
    )
    name = name.strip().strip(".")
    return name or "case"


def encode_url(url):
    """Percent-encode non-ASCII characters, leaving URL syntax intact."""
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    query = urllib.parse.quote(parts.query, safe="=&%:@!$'()*+,;/?-._~")
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, path, query, parts.fragment)
    )


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def make_ciphertext():
    """Build the encrypted timestamp the endpoint expects.

    The page sends ``strTobinary(salt + iv + base64(3DES(timestamp)))``: each
    character of that string as a space-separated character code.
    """
    now = datetime.datetime.now()
    timestamp = str(int(now.timestamp() * 1000))
    salt = "".join(random.choice(ALNUM) for _ in range(24))
    iv = now.strftime("%Y%m%d")
    encrypted = base64.b64encode(des3_encrypt(timestamp, salt, iv)).decode("ascii")
    raw = salt + iv + encrypted
    return " ".join(str(ord(char)) for char in raw), iv


def random_token(size=24):
    return "".join(random.choice(ALNUM) for _ in range(size))


def request_headers(url, user_agent, cookie=None):
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": url,
        "Origin": ORIGIN,
        "X-Requested-With": "XMLHttpRequest",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def session_cookie(timeout, user_agent, target):
    """Get the anonymous SESSION cookie the platform issues to browsers."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    data = urllib.parse.urlencode({"url": target}).encode()
    request = urllib.request.Request(
        AUTHORIZE_URL, data=data, headers=request_headers(target, user_agent)
    )
    with opener.open(request, timeout=timeout) as response:
        response.read()
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in jar)


def extract_text(fragment):
    """Turn a fragment of judgment HTML into plain text."""

    class Collector(html.parser.HTMLParser):
        blocks = {
            "p", "div", "br", "tr", "li", "ul", "ol", "table",
            "h1", "h2", "h3", "h4", "h5", "h6", "section",
        }

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_starttag(self, tag, attrs):
            if tag in self.blocks:
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in self.blocks:
                self.parts.append("\n")

        def handle_data(self, data):
            self.parts.append(data)

    if "<" not in fragment:
        text = fragment
    else:
        collector = Collector()
        collector.feed(fragment)
        collector.close()
        text = "".join(collector.parts)
    text = html.unescape(text)
    lines = [
        re.sub(r"[ \t　\xa0]+", " ", line).strip()
        for line in text.splitlines()
    ]
    return "\n".join(line for line in lines if line)


def fetch_api(doc_id, timeout, user_agent, cookie):
    """Fetch through the JSON endpoint behind the page."""
    ciphertext, iv = make_ciphertext()
    form = urllib.parse.urlencode(
        {
            "ciphertext": ciphertext,
            "docId": doc_id,
            "cfg": API_CFG,
            "pageId": "181107ANFZ0BXSK4",
            "wh": "900",
            "ww": "1440",
            "cs": "0",
            "__RequestVerificationToken": random_token(),
        }
    ).encode()
    request = urllib.request.Request(
        API_URL, data=form, headers=request_headers(API_URL, user_agent, cookie)
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.getcode() != 200:
            raise FetchError(f"HTTP {response.getcode()}")
        body = response.read()
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FetchError("endpoint did not answer JSON")
    if payload.get("code") not in (1, "success"):
        reason = payload.get("description") or payload.get("code")
        raise FetchError(f"endpoint refused: {reason}")
    result = payload.get("result")
    if payload.get("secretKey"):
        if not isinstance(result, str):
            raise FetchError("encrypted result is not a string")
        result = des3_decrypt(base64.b64decode(result), payload["secretKey"], iv)
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            pass
    if not isinstance(result, dict):
        raise FetchError("unexpected endpoint result")
    title = result.get("s1") or ""
    body = result.get("qwContent") or ""
    if not body:
        raise FetchError("document has no text (maybe 不公开)")
    text = extract_text(f"{title}\n{body}")
    if not any(marker in text for marker in MARKERS):
        raise FetchError("text does not contain 判决书 or 裁定书")
    return text


def page_body(document):
    """Cut the judgment container out of a served detail page, if present."""
    starts = [index for index in (
        document.find(marker)
        for marker in ('class="PDF_pox"', 'id="qwContent"', 'class="qwContent"')
    ) if index >= 0]
    if not starts:
        return document
    start = document.rfind("<", 0, min(starts))
    if start < 0:
        start = min(starts)
    ends = [index for index in (
        document.find(marker, start)
        for marker in ('class="detail_guanlian"', 'class="PDF_gg"', "</body>")
    ) if index >= 0]
    return document[start:min(ends) if ends else len(document)]


def fetch_get(url, timeout, user_agent, cookie):
    """Fetch the detail page; with a session its HTML carries the judgment."""
    request = urllib.request.Request(
        encode_url(url), headers=request_headers(url, user_agent, cookie)
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.getcode() != 200:
            raise FetchError(f"HTTP {response.getcode()}")
        charset = response.headers.get_content_charset() or "utf-8"
        body = response.read()
    if not body.strip():
        raise FetchError("empty body")
    text = extract_text(page_body(body.decode(charset, errors="replace")))
    if not any(marker in text for marker in MARKERS):
        if "diy:lawyee" in text or "文书全文" in text:
            raise FetchError("page shell only (pass --cookie from a signed-in browser)")
        raise FetchError("response does not look like a judgment")
    return text


def doc_id_of(url):
    """Return the docId exactly as the URL spells it."""
    parts = urllib.parse.urlsplit(url)
    values = urllib.parse.parse_qs(parts.query).get("docId")
    if values:
        return values[0]
    match = re.search(r"docId[=/]([^&/?#]+)", url)
    return match.group(1) if match else None


def fetch_case(url, timeout, user_agent, cookie):
    errors = []
    doc_id = doc_id_of(url)
    if doc_id:
        try:
            return fetch_api(doc_id, timeout, user_agent, cookie)
        except Exception as exc:
            errors.append(f"api: {str(exc) or exc.__class__.__name__}")
    try:
        return fetch_get(url, timeout, user_agent, cookie)
    except Exception as exc:
        errors.append(f"page fallback: {str(exc) or exc.__class__.__name__}")
    raise FetchError("; ".join(errors) or "failed")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def backoff_seconds(attempt):
    """Exponential backoff with a little jitter: 1, 2, 4, ... seconds."""
    return (2 ** (attempt - 1)) * (1.0 + random.random() * 0.5)


def sleep_between(delay, jitter):
    if delay <= 0:
        return
    spread = delay * jitter
    time.sleep(max(0.0, delay + random.uniform(-spread, spread)))


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def write_failure(path, case_no, url, error, attempts):
    new_file = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(["case_no", "url", "error", "attempts", "utc"])
        writer.writerow([case_no, url, error, attempts, utc_now()])


def write_text(path, text):
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def main(argv=None):
    args = parse_args(argv)

    wanted_ids = load_ids(args.ids) if args.ids is not None else None
    wanted = set(wanted_ids) if wanted_ids is not None else None
    requested_total = len(wanted_ids) if wanted_ids is not None else None

    if wanted_ids is not None and not wanted_ids:
        print("no case numbers in --ids", file=sys.stderr)

    cookie = args.cookie
    if not args.dry_run and cookie is None:
        print(
            "no --cookie: the platform serves only a page shell", file=sys.stderr
        )

    if not args.dry_run:
        args.out_dir.mkdir(parents=True, exist_ok=True)
    failures_path = args.out_dir / "failures.csv"

    fetched = skipped = failed = 0
    seen = set()
    processed = 0
    session = None

    for case_no, url in read_url_map(args.url_map, wanted):
        if args.limit is not None and processed >= args.limit:
            break
        if case_no in seen:
            continue
        seen.add(case_no)
        processed += 1

        if args.dry_run:
            print(f"{case_no}\t{url}")
        else:
            target = args.out_dir / f"{safe_filename(case_no)}.txt"
            if args.resume and target.is_file() and target.stat().st_size > 0:
                skipped += 1
                print(f"[{processed}] {case_no}: skipped (exists)")
            else:
                if cookie is not None and session is None:
                    try:
                        anonymous = session_cookie(
                            args.timeout, args.user_agent, url
                        )
                        session = "; ".join(
                            part for part in (cookie, anonymous) if part
                        )
                    except Exception:
                        session = cookie
                attempts = 0
                error = None
                ok = False
                for attempt in range(1, args.retries + 2):
                    attempts = attempt
                    try:
                        text = fetch_case(
                            url, args.timeout, args.user_agent, session
                        )
                    except Exception as exc:
                        error = str(exc) or exc.__class__.__name__
                        if attempt <= args.retries:
                            time.sleep(backoff_seconds(attempt))
                        continue
                    try:
                        write_text(target, text)
                    except OSError as exc:
                        error = f"write failed: {exc}"
                        break
                    ok = True
                    break
                if ok:
                    fetched += 1
                    print(f"[{processed}] {case_no}: fetched")
                else:
                    failed += 1
                    write_failure(
                        failures_path, case_no, url, error or "unknown", attempts
                    )
                    print(f"[{processed}] {case_no}: failed ({error or 'unknown'})")

                if args.delay and processed < (
                    args.limit or requested_total or 0
                ):
                    sleep_between(args.delay, args.jitter)

        if wanted is not None and len(seen) >= len(wanted):
            break

    missing = [case_no for case_no in wanted_ids or [] if case_no not in seen]
    truncated = (
        wanted is not None
        and args.limit is not None
        and processed >= args.limit
        and len(seen) < len(wanted)
    )

    if not args.dry_run and missing:
        reason = (
            "not reached (--limit)" if truncated else "case number not in --url-map"
        )
        for case_no in missing:
            failed += 1
            write_failure(failures_path, case_no, "", reason, 0)
        print(
            f"{len(missing)} case number(s) {reason}", file=sys.stderr
        )

    if args.dry_run:
        return 0

    print(f"fetched {fetched}, skipped {skipped}, failed {failed}")
    if failed:
        print(f"failures: {failures_path}")
    else:
        print("failures: none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
