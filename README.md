# gr-multi_kiwisdr

**Multi-Coherent KiwiSDR** — GNU Radio OOT module for GPS-synchronized IQ acquisition
from multiple [KiwiSDR](http://kiwisdr.com/) receivers.

---

## Overview

This Python-native GNU Radio Out-Of-Tree (OOT) module provides three source blocks
that stream IQ data from KiwiSDR WebSDR receivers over WebSocket, with GPS timestamp
tags compatible with the UHD/USRP stream-tag convention.

| Block | Class | Purpose |
|-------|-------|---------|
| `KiwiSDR Source` | `KiwiSDRSource` | Single receiver → 1× complex64 IQ, GPS timestamps as `rx_time` stream tags |
| `KiwiSDR Multi-Source (TDoA)` | `KiwiMultiSource` | N receivers → N IQ streams, each tagged with its own GPS `rx_time` |
| `KiwiSDR Wideband` | `KiwiWidebandSource` | Frequency-stitched wideband from 1 receiver |

All blocks appear in GRC under the **[Multi-Coherent KiwiSDR]** category.

> **Important — where coherent alignment happens.** The real-time GRC blocks
> (`KiwiSDRSource`, `KiwiMultiSource`) *stream* IQ and attach per-packet GPS
> timestamps as `rx_time` tags, but they do **not** resample/align the streams to a
> common absolute time base on the fly. Full GPS `t0` alignment and sample-rate
> **drift correction** (LS fit + resampling to a common Time-of-Week) is performed
> by the offline/async path **`CoherentMultiKiwiSource`** (`coh_multi_kiwi.py`),
> which is what you should use for TDoA-grade coherent multi-station captures.

### Comparison

| Feature | gr-kiwisdr (C. Mayer) | KiwiSDR built-in TDoA | **gr-multi_kiwisdr** |
|---------|-----------------------|-----------------------|----------------------|
| Language | C++ | Octave | **Python** |
| Multi-station TDoA | ✗ (same device) | ✓ (browser UI) | **✓ (GNU Radio)** |
| GPS t0 alignment | ✗ | ✗ | **✓ (LS fit, ±1 ms — `CoherentMultiKiwiSource`)** |
| Drift correction | ✗ | ✗ | **✓ (GPS resampling — `CoherentMultiKiwiSource`)** |
| Standalone (no GR) | ✗ | ✗ | **✓** |
| GNU Radio integration | ✓ | ✗ | **✓** |

---

## Requirements

```
Python >= 3.8
websockets >= 10.0
numpy >= 1.21
GNU Radio >= 3.10   (optional — only for GRC blocks)
```

## Installation

### Step 1 — install the Python module

**Important:** use the same Python interpreter that GNU Radio uses.

Linux/Mac — from the cloned repository root:

```bash
cd gr-multi_kiwisdr
pip install .
```

Windows + [radioconda](https://github.com/ryanvolz/radioconda):

```powershell
cd gr-multi_kiwisdr
C:\ProgramData\radioconda\python.exe -m pip install .
```

### Step 2 — install the GRC blocks

Copy the block definitions to your GRC local blocks path:

Linux/Mac:

```bash
mkdir -p ~/.gnuradio/grc/blocks
cp grc/*.block.yml ~/.gnuradio/grc/blocks/
```

Windows:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.gnuradio\grc\blocks"
Copy-Item grc\*.block.yml "$env:USERPROFILE\.gnuradio\grc\blocks\"
```

Restart GNU Radio Companion — the blocks appear under **[Multi-Coherent KiwiSDR]**.

> The local blocks path is set by `local_blocks_path` in
> `~/.config/gnuradio/config.conf` (section `[grc]`); the paths above are the defaults.

### Standalone use (no GNU Radio)

The module works without GNU Radio — only `websockets` and `numpy` are needed:

```bash
pip install .
# or just: pip install websockets numpy and add python/ to sys.path
```

---

## Quickstart

### Standalone — single receiver

```python
from multi_kiwisdr import KiwiSDRSource

src = KiwiSDRSource(
    host='kiwisdr.example.com',
    port=8073,
    freq_mhz=9.996,
    bandwidth_hz=10_000,
)
iq, gps_tl = src.acquire(duration_sec=30.0)
print(f"Samples: {iq.size}, drift: {gps_tl.drift_ppm:+.1f} ppm, "
      f"GPS t0: {gps_tl.t0_gps:.3f} s")
```

### Standalone — multi-station coherent acquisition

```python
import asyncio
from multi_kiwisdr import CoherentMultiKiwiSource

stations = [
    # (host, port, lat, lon)
    ("kiwi1.example.com", 8073, 59.03,  9.96),
    ("kiwi2.example.com", 8073, 47.66, 26.39),
    ("kiwi3.example.com", 8073, 52.11,  4.57),
]
src = CoherentMultiKiwiSource(stations, freq_mhz=9.996, duration_sec=30.0)
channels = asyncio.run(src.acquire())
src.print_report(channels)
```

Find public stations with free slots: [rx.kiwisdr.com](http://rx.kiwisdr.com/)
or use the included `find_kiwisdr_stations.py`.

### GNU Radio Companion

1. Complete both installation steps above
2. Open GRC, search for **"KiwiSDR"** in the block palette
3. Drag **KiwiSDR Source** or **KiwiSDR Multi-Source (TDoA)** into your flowgraph
4. Set `host`, `freq_mhz`, `bandwidth_hz`
5. Connect `out` → `QT GUI Freq Sink` or `File Sink`

The blocks self-limit to the real network stream rate (12 kSps),
so a Throttle block is not required.

---

## Module structure

```
gr-multi_kiwisdr/
├── python/
│   ├── multi_kiwisdr.py         ← entry-point (import this)
│   ├── kiwi_blocks_gr.py        ← GNU Radio sync_block implementations
│   ├── kiwisdr_source.py        ← standalone KiwiSDRSource + GpsTimeline
│   └── coh_multi_kiwi.py        ← CoherentMultiKiwiSource (async)
├── grc/
│   ├── multi_kiwisdr_source.block.yml
│   ├── multi_kiwisdr_multi_source.block.yml
│   └── multi_kiwisdr_wideband.block.yml
├── examples/
│   ├── demo_standalone.py       ← demo without GNU Radio
│   └── kiwisdr_rx_single.grc    ← example GRC flowgraph
├── tests/
│   └── test_blocks.py
├── setup.py
├── LICENSE
└── README.md
```

## Running tests

```bash
python -m pytest tests/ -v
# or directly:
python tests/test_blocks.py
```

---

## GPS synchronization

KiwiSDR firmware >= v1.560 embeds GPS timestamps in each SND frame:

```
SND frame (with GPS):
  [0:3]   b'SND'   — frame marker
  [3]     flags    — bit3=IQ mode
  [4:8]   seq      — frame sequence (u32 LE)
  [8:10]  smeter   — RSSI: 0.1·smeter − 127 dBm (u16 BE)
  [10]    last_sol — GPS fix AGE [s]: 0 = fresh, large value = stale/extrapolated
  [11]    dummy    — reserved
  [12:16] gpssec   — GPS Time-of-Week [s] (u32 LE)
  [16:20] gpsnsec  — fractional nanoseconds (u32 LE)
  [20:]   IQ data  — 512 × (I16 + Q16), big-endian
```

`GpsTimeline` fits a least-squares line through `(t_gps, sample_count)` pairs
to estimate the effective sample rate and `t0_gps`. Typical drift: **±80 ppm**
(±1 ms per 13 s), requiring resampling for coherent multi-station analysis.

---

## License

MIT — see [LICENSE](LICENSE).

## Credits and references

- KiwiSDR WebSocket protocol: [jks-prv/kiwiclient](https://github.com/jks-prv/kiwiclient) (J. Seamons)
- GPS LS drift estimation inspired by `proc_kiwi_iq_wav.m` from kiwiclient
- Phase-correction method adapted from `coh_stream_synth` in
  [hcab14/gr-kiwisdr](https://github.com/hcab14/gr-kiwisdr) (C. Mayer)
- TDoA multilateration reference: [hcab14/TDoA](https://github.com/hcab14/TDoA)
- GNU Radio OOT guide: [wiki.gnuradio.org](https://wiki.gnuradio.org/index.php/OutOfTreeModules)
