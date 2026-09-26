import pathlib
import re

STATIC = pathlib.Path(__file__).resolve().parent.parent / "app" / "static"


def test_no_html_injection_and_no_inline_code():
    js = (STATIC / "app.js").read_text()
    assert ".innerHTML" not in js and "insertAdjacentHTML" not in js and "outerHTML" not in js
    html = (STATIC / "index.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)   # every script is external (CSP default-src 'self')
    assert "<style" not in html and " style=" not in html
    assert " on[a-z]+=" not in html and not re.search(r"\son\w+=", html)
