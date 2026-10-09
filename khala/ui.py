"""Page rendering: Jinja2 templates (autoescaped) + uniform security response headers.

Only the sign-in, two-step verification and security settings pages may load scripts (passkeys require WebAuthn),
and only this site's own webauthn.js; on every other page the CSP allows no scripts at all.
"""
import hashlib
import time
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.responses import HTMLResponse, Response

from . import clock

HERE = Path(__file__).parent
env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]),
                  trim_blocks=True, lstrip_blocks=True)

CSP = {
    # OAuth pages skip form-action: Chrome would block the post-submit redirect to the client callback URL
    "oauth": "default-src 'none'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    "app": "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; "
           "base-uri 'none'",
}
CSP["oauth_js"] = CSP["oauth"] + "; script-src 'self'; connect-src 'self'"
CSP["app_js"] = CSP["app"] + "; script-src 'self'; connect-src 'self'"
HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
           "Referrer-Policy": "same-origin", "Strict-Transport-Security": "max-age=31536000"}


def when(ts, fmt="%Y-%m-%d %H:%M"):
    if not ts:
        return ""
    return clock.local(ts).strftime(fmt)


def ago(ts):
    if not ts:
        return "never"
    s = int(time.time() - float(ts))
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60)):
        if s >= n:
            return "%d %s ago" % (s // n, unit)
    return "just now"


env.filters["when"] = when
env.globals["instance_name"] = "Khala"          # create_app sets the configured name
env.filters["ago"] = ago
CSS = (HERE / "static" / "app.css").read_bytes()
env.globals["css_version"] = hashlib.sha256(CSS).hexdigest()[:10]      # new URL when styles change; no stale cache
JS = (HERE / "static" / "webauthn.js").read_bytes()
env.globals["js_version"] = hashlib.sha256(JS).hexdigest()[:10]
ICON = (HERE / "static" / "khala-icon.png").read_bytes()
env.globals["icon_version"] = hashlib.sha256(ICON).hexdigest()[:10]


def render(template, status=200, csp="app", headers=None, **ctx):
    body = env.get_template(template).render(**ctx)
    h = dict(HEADERS, **{"Content-Security-Policy": CSP[csp]})
    h.update(headers or {})
    return HTMLResponse(body, status_code=status, headers=h)


def stylesheet():
    return Response(CSS, media_type="text/css",
                    headers={"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})


def script():
    return Response(JS, media_type="text/javascript",
                    headers={"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})


def brand_icon():
    return Response(ICON, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})
