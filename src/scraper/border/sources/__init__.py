"""Where wait times come from.

CBP is authoritative for structure — it is the only source that covers all six ports,
every lane type, hours and closures. Other sites are useful for one thing CBP is bad
at: freshness. CBP republishes about once an hour; some sites refresh every few
minutes, and when they disagree that disagreement is itself information.

Independence matters more than count. Most border sites re-publish the CBP feed
verbatim, so three of them agreeing is one source agreeing with itself. Only sources
marked independent=True count toward corroboration.
"""
from .base import Reading, Source
from .borderswaittime import BordersWaitTimeSource
from .cbp_source import CbpSource, readings_from
from .pasosfronterizos import PasosFronterizosSource

# The sources BorderFeed reads besides CBP, which it reads itself for the full snapshot.
SOURCES: list[Source] = [PasosFronterizosSource(), BordersWaitTimeSource()]

__all__ = ["Reading", "Source", "SOURCES", "CbpSource", "PasosFronterizosSource",
           "BordersWaitTimeSource", "readings_from"]
