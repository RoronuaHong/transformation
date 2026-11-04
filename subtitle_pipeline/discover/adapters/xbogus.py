"""X-Bogus signature for Douyin web API requests (in-house, no external service).

Vendored from the canonical pure-Python implementation
(https://gist.github.com/yvbbrjdr/8d41ac82f650178f7cdec5bda94d72c2). Douyin signs
every web API call with ``X-Bogus``; without a valid signature the server rejects the
request (``status_code`` != 0). This lets the discovery crawler hit Douyin directly
(with cookies from ``cookies.douyin.txt``) instead of depending on ``DOUYIN_API_BASE``.

NOTE: X-Bogus is reverse-engineered and Douyin rotates its anti-bot scheme. If the
signature stops being accepted, regenerate from a current reference implementation.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import time
from urllib import parse

_X_BOGUS_SHORT_STR = "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe="
_X_BOGUS_UA_KEY = ["\u0000", "\u0001", "\u000e"]


def append_x_bogus(url: str, user_agent: str) -> str:
    """Append ``&X-Bogus=...`` to ``url`` (which must already carry its query string)."""
    return f"{url}&X-Bogus={x_bogus(url, user_agent)}"


def x_bogus(url: str, user_agent: str) -> str:
    query = parse.urlparse(url).query
    arr2 = _get_arr2(query, user_agent, "")
    garbled_string = _get_garbled_string(arr2)
    ret = ""
    for i in range(0, 21, 3):
        base_num = garbled_string[i] << 16 | garbled_string[i + 1] << 8 | garbled_string[i + 2]
        for j in range(18, -1, -6):
            ret += _X_BOGUS_SHORT_STR[(base_num >> j) & 63]
    return ret


def _0x30492c(a: list[str], b: str) -> bytearray:
    d = list(range(256))
    c = 0
    for i in range(256):
        c = (c + d[i] + ord(a[i % len(a)])) % 256
        e = d[i]
        d[i] = d[c]
        d[c] = e
    t = 0
    c = 0
    ret = bytearray(len(b))
    for i in range(len(b)):
        t = (t + 1) % 256
        c = (c + d[t]) % 256
        e = d[t]
        d[t] = d[c]
        d[c] = e
        ret[i] = ord(b[i]) ^ d[(d[t] + d[c]) % 256]
    return ret


def _get_arr2(payload: str, ua: str, form: str) -> list[int]:
    salt_payload = list(hashlib.md5(hashlib.md5(payload.encode()).digest()).digest())
    salt_form = list(hashlib.md5(hashlib.md5(form.encode()).digest()).digest())
    salt_ua = list(
        hashlib.md5(base64.b64encode(_0x30492c(_X_BOGUS_UA_KEY, ua))).digest()
    )
    ts = int(time.time())
    arr1 = [
        64,
        0,
        1,
        14,
        salt_payload[14],
        salt_payload[15],
        salt_form[14],
        salt_form[15],
        salt_ua[14],
        salt_ua[15],
        (ts >> 24) & 255,
        (ts >> 16) & 255,
        (ts >> 8) & 255,
        (ts >> 0) & 255,
        88,
        194,
        176,
        26,
    ]
    arr1.append(functools.reduce(lambda a, b: a ^ b, arr1))
    return arr1[::2] + arr1[1::2]


def _get_garbled_string(arr2: list[int]) -> list[int]:
    p = [
        arr2[0],
        arr2[10],
        arr2[1],
        arr2[11],
        arr2[2],
        arr2[12],
        arr2[3],
        arr2[13],
        arr2[4],
        arr2[14],
        arr2[5],
        arr2[15],
        arr2[6],
        arr2[16],
        arr2[7],
        arr2[17],
        arr2[8],
        arr2[18],
        arr2[9],
    ]
    return [2, 255] + list(_0x30492c(["ÿ"], "".join(chr(i) for i in p)))
