"""Exact CSP hashes for the build-owned inline first-paint presentation."""
import base64
import hashlib
import re


def first_paint_csp(document: str) -> tuple[str, str]:
    block = re.search(r"<!-- setup-first-paint:start -->(.*?)<!-- setup-first-paint:end -->",
                      document, re.DOTALL)
    if block is None:
        return "", ""
    def source(tag: str) -> str:
        items = re.findall(rf"<{tag} data-setup-first-paint>(.*?)</{tag}>", block.group(1), re.DOTALL)
        if len(items) != 1:
            raise ValueError("Invalid build-owned first-paint presentation")
        digest = base64.b64encode(hashlib.sha256(items[0].encode("utf-8")).digest()).decode("ascii")
        return f" 'sha256-{digest}'"
    return source("script"), source("style")
