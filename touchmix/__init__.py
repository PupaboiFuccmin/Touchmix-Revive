"""Control a QSC TouchMix-16 over the network. See docs/PROTOCOL.md."""
from .client import TouchMix, TouchMixError
from .discover import discover
from .params import ParamTable

__all__ = ['TouchMix', 'TouchMixError', 'discover', 'ParamTable']
