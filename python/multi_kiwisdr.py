# -*- coding: utf-8 -*-
"""
multi_kiwisdr.py  —  Flat entry-point for gr-multi_kiwisdr blocks

Use this module when adding the python/ directory to sys.path manually
(without a proper pip install of the package).

Example:
    import sys
    sys.path.insert(0, '/path/to/gr-multi_kiwisdr/python')
    from multi_kiwisdr import KiwiSDRSource, CoherentMultiKiwiSource
"""
from kiwi_blocks_gr import (
    KiwiSDRSource,
    KiwiMultiSource,
    KiwiWidebandSource,
    parse_snd_frame,
)
from kiwisdr_source import GpsTimeline
from coh_multi_kiwi import CoherentMultiKiwiSource

__all__ = [
    "KiwiSDRSource",
    "KiwiMultiSource",
    "KiwiWidebandSource",
    "CoherentMultiKiwiSource",
    "GpsTimeline",
    "parse_snd_frame",
]
