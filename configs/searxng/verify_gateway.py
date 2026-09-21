"""One bounded public search and route checks; prints metadata and <=3 URLs."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request

ORIGIN = "http://127.0.0.1:8721"
MAX_BYTES = 262_144


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="bibliothèque Sorel-Tracy services")
    args = parser.parse_args()
    if not 1 <= len(args.query) <= 2_000:
        raise ValueError("query must contain 1 through 2000 characters")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    for method, path in (("GET", "/search"), ("POST", "/config")):
        request = urllib.request.Request(ORIGIN + path, method=method)
        try:
            opener.open(request, timeout=3)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise RuntimeError("unexpected route response") from None
        else:
            raise RuntimeError("gateway exposes an unexpected route")
    body = urllib.parse.urlencode({"q": args.query, "format": "json"}).encode()
    request = urllib.request.Request(
        ORIGIN + "/search",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=15) as response:
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES or response.status != 200:
            raise RuntimeError("bounded search response failed")
        result = json.loads(raw)
    entries = result.get("results")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("search returned no usable results")
    urls = []
    for item in entries:
        url = item.get("url") if isinstance(item, dict) else None
        if isinstance(url, str) and urllib.parse.urlsplit(url).scheme in {"http", "https"}:
            urls.append(url)
        if len(urls) == 3:
            break
    if not urls:
        raise RuntimeError("search returned no HTTP sources")
    print(json.dumps({
        "passed": True,
        "origin": ORIGIN,
        "method": "POST",
        "format": "json",
        "unexpected_routes_blocked": True,
        "result_count": len(entries),
        "response_bytes": len(raw),
        "source_urls": urls,
        "model_calls": 0,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
