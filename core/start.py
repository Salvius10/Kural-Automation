"""Where does this task start? (Plan §7.11)

The user never pastes a URL, and no model ever writes one. A URL can only come
from four places: `data/sites.yaml`, the tab already open, a link observed on
the page, or the search template with model-written *words* in the query slot.
"""

from urllib.parse import quote_plus

import yaml

from .config import ROOT, settings
from .events import StartResolved

_SITES = None


def load_sites(reload=False):
    global _SITES
    if _SITES is None or reload:
        path = ROOT / settings().navigation.sites_file
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        _SITES = data or {}
    return _SITES


def sites():
    return load_sites().get("sites") or {}


def search_template():
    return (load_sites().get("search") or {}).get("url_template", "https://www.google.com/search?q={query}")


def search_url(query):
    """The query is model-written words; the template is ours."""
    if not isinstance(query, str) or not query.strip() or "://" in query:
        raise ValueError("Refusing a search query that is not plain words")
    return search_template().format(query=quote_plus(query.strip()[:120]))


def site_url(site_id):
    site = sites().get(site_id)
    if not site:
        raise ValueError(f"Unknown site id: {site_id}")
    return site["url"]


class StartResolver:
    """Order of precedence: named site, current page, known site, web search."""

    def __init__(self, bus=None, text_writer=None):
        self.bus = bus
        self.text_writer = text_writer

    def emit(self, mode, site=None, url=None):
        if self.bus:
            self.bus.publish(StartResolved(mode=mode, site=site, url=url))
        return {"mode": mode, "site": site, "url": url}

    async def resolve(self, goal, facts, extra_answers):
        """`extra_answers` holds the validated start heads from the fused request."""
        config = settings().navigation

        named = (facts or {}).get("site")
        if named and named in sites():
            return self.emit("named", named, site_url(named))

        here = (extra_answers or {}).get("start_here")
        if here and here["probability"] >= config.start_here_threshold:
            return self.emit("here")

        chosen = (extra_answers or {}).get("start_site")
        if (
            chosen
            and chosen["choice"] != "none"
            and chosen["choice"] in sites()
            and float(chosen.get("confidence", 0)) >= config.start_site_min_confidence
        ):
            return self.emit("site", chosen["choice"], site_url(chosen["choice"]))

        query = goal
        if self.text_writer:
            try:
                query = await self.text_writer.search_query(goal)
            except Exception:
                query = goal
        return self.emit("search", None, search_url(query))
