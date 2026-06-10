"""Build a ytmusicapi `browser.json` from a copied 'Copy as cURL' command.

Usage:
    pbpaste > /tmp/yt_curl.txt          # paste the Safari "Copy as cURL"
    python scripts/ytmusic_from_curl.py /tmp/yt_curl.txt

Reads the cURL command, extracts its request headers, and writes browser.json
in the current directory. Prints only counts / a yes-no on the cookie — never the
header values — so your live Google session never lands in any log or transcript.

Browsers emit the cookie differently: Safari hides it under curl's ``-b`` flag,
while Chrome/Firefox include it as ``-H 'cookie: ...'``. Both forms are handled.
"""

from __future__ import annotations

import shlex
import sys

from ytmusicapi import setup


def headers_from_curl(text: str) -> list[str]:
    # Join shell line-continuations so shlex sees one command.
    text = text.replace("\\\n", " ")
    tokens = shlex.split(text)
    headers: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] in ("-H", "--header") and i + 1 < len(tokens):
            headers.append(tokens[i + 1])
            i += 2
        elif tokens[i] in ("-b", "--cookie") and i + 1 < len(tokens):
            # Safari emits the cookie via curl's -b flag, not as an -H header.
            headers.append(f"cookie: {tokens[i + 1]}")
            i += 2
        else:
            i += 1
    return headers


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python scripts/ytmusic_from_curl.py <curl-file>")
        return 2
    with open(sys.argv[1], encoding="utf-8") as fh:
        text = fh.read()

    headers = headers_from_curl(text)
    if not headers:
        print("No -H headers found in that file. Did you 'Copy as cURL'?")
        return 1

    has_cookie = any(h.lower().startswith("cookie:") for h in headers)
    setup(filepath="browser.json", headers_raw="\n".join(headers))
    print(f"Wrote browser.json ({len(headers)} headers). cookie present: {has_cookie}")
    if not has_cookie:
        print("WARNING: no cookie header — playlist creation will fail. "
              "Re-copy from a logged-in music.youtube.com request.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
