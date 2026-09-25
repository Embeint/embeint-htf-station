"""HTTP helpers for requests carrying station credentials."""

from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_no_redirect(request: Request, timeout: float):
    """Never forward a station credential to a redirected URL."""
    return build_opener(NoRedirect()).open(request, timeout=timeout)
