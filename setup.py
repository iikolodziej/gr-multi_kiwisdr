#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup.py for gr-multi_kiwisdr

Multi-Coherent KiwiSDR GNU Radio OOT module.
GPS-synchronized IQ acquisition from multiple KiwiSDR receivers.

IMPORTANT: install with the same Python interpreter that GNU Radio uses,
e.g. on radioconda (Windows):
    C:\\ProgramData\\radioconda\\python.exe -m pip install .

Then copy grc/*.block.yml to your GRC local blocks path
(default: ~/.gnuradio/grc/blocks/) so the blocks appear in GRC.
"""
import os
from setuptools import setup

here = os.path.abspath(os.path.dirname(__file__))

setup(
    name="gr-multi_kiwisdr",
    version="1.0.0",
    description=(
        "Multi-Coherent KiwiSDR GNU Radio OOT module — "
        "GPS-synchronized IQ acquisition from multiple KiwiSDR receivers"
    ),
    long_description=open(os.path.join(here, "README.md"), encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="Multi-Coherent KiwiSDR contributors",
    license="MIT",
    package_dir={"": "python"},
    py_modules=[
        "multi_kiwisdr",
        "kiwi_blocks_gr",
        "kiwisdr_source",
        "coh_multi_kiwi",
    ],
    python_requires=">=3.8",
    install_requires=[
        "websockets>=10.0",
        "numpy>=1.21",
    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Topic :: Communications :: Ham Radio",
        "Topic :: Scientific/Engineering",
    ],
)
