from news_intel.adapters.base import CollectedItem, SourceHealthState, get_adapter
from news_intel.adapters.federal_register import FederalRegisterAdapter
from news_intel.adapters.gdelt import GdeltAdapter
from news_intel.adapters.hn import HackerNewsAdapter
from news_intel.adapters.html_index import HtmlIndexAdapter
from news_intel.adapters.ical import IcalAdapter
from news_intel.adapters.rss import RssAdapter
from news_intel.adapters.social_stub import SocialStubAdapter

__all__ = [
    "CollectedItem",
    "SourceHealthState",
    "get_adapter",
    "RssAdapter",
    "FederalRegisterAdapter",
    "HackerNewsAdapter",
    "IcalAdapter",
    "GdeltAdapter",
    "HtmlIndexAdapter",
    "SocialStubAdapter",
]
