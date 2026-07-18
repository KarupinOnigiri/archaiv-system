from flask import request


def is_htmx_request():
    """True if this request came from htmx (it sets the HX-Request header
    on every request automatically). Routes use this to decide whether to
    render a Jinja2 HTML fragment or return JSON, once templates exist —
    see frontend-readiness notes. Until then, nothing calls this."""
    return request.headers.get("HX-Request") == "true"
