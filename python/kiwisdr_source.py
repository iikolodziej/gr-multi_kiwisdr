# -*- coding: utf-8 -*-
"""
kiwisdr_source.py  —  GNU Radio Python Source Block dla KiwiSDR
================================================================

Blok zrodlowy GNU Radio dostarczajacy strumien IQ z jednego KiwiSDR.
Odpowiednik gr-kiwisdr/kiwisdr (C. Mayer, C++) napisany w Pythonie.

Obsługuje:
  - Strumien IQ (complex64) przez WebSocket KiwiSDR
  - GPS timestamps jako stream tagi 'rx_time' (format UHD/USRP)
  - Automatyczna korekcja dryftu sample-rate (GPS LS fit)
  - GPS t0 alignment (korekcja offsetu GPS ToW do 2 s)

Uzycie w GNU Radio Companion:
  Zainstaluj modul (patrz README.md), w GRC:
  - Wyszukaj "KiwiSDR Source" w palecie blokow
  - Ustaw host, port, freq_mhz, bandwidth_hz
  - Wyj: complex64 (IQ)

Uzycie standalone (bez GNU Radio):
  from kiwisdr_source import KiwiSDRSource
  src = KiwiSDRSource(host='kiwi.example.com', freq_mhz=9.996)
  src.start()
  time.sleep(30)
  iq = src.get_buffer()
  src.stop()
"""

import asyncio
import logging
import queue
import struct
import threading
import time
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ── GNU Radio import – opcjonalny ──────────────────────────────────────────
try:
    import gnuradio.gr as gr
    import pmt
    _HAS_GR = True
except ImportError:
    _HAS_GR = False


# ── Stale protokolu KiwiSDR ────────────────────────────────────────────────
KIWI_SAMPLES_PER_FRAME = 512    # probek complex per ramka SND
KIWI_PAYLOAD_BYTES     = KIWI_SAMPLES_PER_FRAME * 4  # 2048 B (I16 + Q16)
KIWI_GPS_HEADER_BYTES  = 10     # last_sol(1) + pad(1) + gpssec(4) + gpsnsec(4)
KIWI_SND_HDR_BYTES     = 10     # 'SND'(3) + flags(1) + seq(4 LE) + smeter(2 BE)
KIWI_FRAME_WITH_GPS    = KIWI_SND_HDR_BYTES + KIWI_GPS_HEADER_BYTES + KIWI_PAYLOAD_BYTES


def parse_snd_frame(data: bytes) -> Tuple[np.ndarray, int, float, Optional[dict]]:
    """
    Parsuje binarna ramke SND KiwiSDR (z GPS lub bez).

    Wire format (z GPS, firmware >= v1.560):
        [0:3]  b'SND'          — marker
        [3]    flags            — bit3=IQ (0x08), bit0=ADPCM
        [4:8]  seq  u32 LE     — numer sekwencji ramki
        [8:10] smeter u16 BE   — RSSI: 0.1*smeter - 127 dBm
        [10]   last_gps_sol u8 — 0=GPS locked, >0=sek od ostatniego locka
        [11]   pad u8
        [12:16] gpssec  u32 LE — GPS Time-of-Week [s]
        [16:20] gpsnsec u32 LE — fractional ns
        [20:]  payload I16 BE  — interleaved I Q I Q ...

    Zwraca (iq_complex64, seq, rssi_dbm, gps_dict lub None)
    """
    if len(data) < KIWI_SND_HDR_BYTES or data[:3] != b"SND":
        return np.zeros(0, dtype=np.complex64), 0, -127.0, None

    flags  = data[3]
    # seq i GPS: LITTLE-endian; smeter: BIG-endian (zgodnie z kiwiclient/
    # pe_receivers, potwierdzone empirycznie). Wczesniej '>I' psul GPS.
    seq    = struct.unpack_from("<I", data, 4)[0]
    smeter = struct.unpack_from(">H", data, 8)[0]
    rssi   = 0.1 * smeter - 127.0

    has_gps = (len(data) >= KIWI_FRAME_WITH_GPS)
    gps = None
    payload_off = KIWI_SND_HDR_BYTES

    if has_gps:
        last_sol = data[10]
        gpssec   = struct.unpack_from("<I", data, 12)[0]
        gpsnsec  = struct.unpack_from("<I", data, 16)[0]
        gps = {
            "seq":      seq,
            "last_sol": last_sol,
            "gpssec":   gpssec,
            "gpsnsec":  gpsnsec,
            "t_gps":    gpssec + gpsnsec * 1e-9,
            "locked":   (last_sol == 0),
        }
        payload_off = KIWI_SND_HDR_BYTES + KIWI_GPS_HEADER_BYTES

    raw = data[payload_off:]
    n   = len(raw) // 2
    if n < 2:
        return np.zeros(0, dtype=np.complex64), seq, rssi, gps

    samp = np.frombuffer(raw, dtype=">i2").astype(np.float32) / 32768.0
    iq   = (samp[0::2] + 1j * samp[1::2]).astype(np.complex64)
    return iq, seq, rssi, gps


class GpsTimeline:
    """
    Akumuluje ramki GPS i estymuje fs_eff + t0_gps metoda LS.
    Identyczna z proc_kiwi_iq_wav.m (Seamons/kiwiclient).
    """
    def __init__(self, fs_nominal: int = 12000):
        self.fs_nominal = fs_nominal
        self._pts: list = []        # (t_gps, cum_samples)
        self._cum  = 0
        self.fs_eff:  float = float(fs_nominal)
        self.t0_gps:  Optional[float] = None
        self.fitted:  bool = False

    def push(self, gps: Optional[dict], n_samples: int) -> None:
        if gps and gps["locked"]:
            t = gps["t_gps"]
            if self._pts:
                dt = t - self._pts[-1][0]
                if dt >  302400: t -= 604800   # rollover GPS ToW
                elif dt < -302400: t += 604800
            self._pts.append((t, self._cum))
            if len(self._pts) >= 3:
                self._fit()
        self._cum += n_samples

    def _fit(self) -> None:
        t = np.array([p[0] for p in self._pts], dtype=np.float64)
        s = np.array([p[1] for p in self._pts], dtype=np.float64)
        dt = t - t[0];  ds = s - s[0]
        ok = dt > 0.05
        if ok.sum() < 2: return
        fs = float(np.dot(ds[ok], dt[ok]) / np.dot(dt[ok], dt[ok]))
        if fs < 100: return
        self.fs_eff = fs
        self.t0_gps = t[0] - s[0] / self.fs_eff
        self.fitted = True

    @property
    def drift_ppm(self) -> float:
        return (self.fs_eff - self.fs_nominal) / self.fs_nominal * 1e6

    def resample(self, iq: np.ndarray) -> np.ndarray:
        """Korekcja cumulative skew: resample z fs_eff do fs_nominal."""
        if not self.fitted or abs(self.drift_ppm) < 0.5:
            return iq
        n = len(iq)
        t_old = np.arange(n) / self.fs_eff
        t_new = np.arange(n) / self.fs_nominal
        valid = t_new <= t_old[-1]
        r = np.interp(t_new[valid], t_old, iq.real).astype(np.float32)
        i = np.interp(t_new[valid], t_old, iq.imag).astype(np.float32)
        return (r + 1j * i).astype(np.complex64)

    def align(self, iq: np.ndarray, ref_t0: float,
               max_sec: float = 2.0) -> np.ndarray:
        """Wyrownanie IQ do ref_t0 z korekcja GPS ToW rollover."""
        if self.t0_gps is None: return iq
        dt = self.t0_gps - ref_t0
        if dt >  302400: dt -= 604800
        if dt < -302400: dt += 604800
        if abs(dt) > max_sec:
            logger.warning("GPS t0 offset %.0f ms > %.0f s", dt*1000, max_sec)
            return iq
        sh = int(round(dt * self.fs_nominal))
        if sh == 0: return iq
        n  = len(iq)
        out = np.zeros(n, dtype=np.complex64)
        if sh > 0:
            if sh < n: out[sh:] = iq[:n-sh]
        else:
            s = abs(sh)
            if s < n: out[:n-s] = iq[s:]
        logger.debug("GPS align: shift=%+d samp (%.1f ms)", sh, dt*1000)
        return out


# ═══════════════════════════════════════════════════════════════════════════
# KiwiSDRSource — GNU Radio sync_block (fallback: standalone)
# ═══════════════════════════════════════════════════════════════════════════

_BASE = (gr.sync_block if _HAS_GR else object)


class KiwiSDRSource(_BASE):
    """
    GNU Radio Python Source Block: KiwiSDR → complex64 IQ stream.

    Parametry bloku (edytowalne w GNU Radio Companion):
      host         — adres KiwiSDR (np. 'kiwisdr.example.com')
      port         — port WebSocket (domyslnie 8073)
      freq_mhz     — czestotliwosc strojenia [MHz]
      bandwidth_hz — szerokosc pasma IQ [Hz] (domyslnie 10000)
      agc_on       — AGC wl/wyl (domyslnie True)
      man_gain     — wzmocnienie manualne 0-120 dB (gdy agc_on=False)

    Wyjscia:
      0: complex64 — surowe probki IQ

    Stream tagi (rx_time, zgodny z UHD/USRP):
      klucz:    'rx_time'
      wartosc:  PMT para (integer_seconds, fractional_seconds)
      emitowany przy kazdej ramce z GPS lock=0
    """

    ITEM_SIZE = np.dtype(np.complex64).itemsize

    def __init__(self,
                 host: str,
                 port: int = 8073,
                 freq_mhz: float = 9.996,
                 bandwidth_hz: float = 10_000.0,
                 agc_on: bool = True,
                 man_gain: int = 50):
        if _HAS_GR:
            gr.sync_block.__init__(self, "KiwiSDR Source",
                                   in_sig=None, out_sig=[np.complex64])
        self.host        = str(host).strip()
        self.port        = port
        self.freq_mhz    = freq_mhz
        self.bandwidth_hz = bandwidth_hz
        self.agc_on      = agc_on
        self.man_gain    = man_gain

        self._q: "queue.Queue[Tuple[np.ndarray, Optional[dict]]]" = queue.Queue(256)
        self._gps  = GpsTimeline()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._offset  = 0   # probki wyemitowane (do tagowania)

    # ── GNU Radio lifecycle ─────────────────────────────────────────────────
    def start(self) -> bool:
        self._running = True
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_thread, daemon=True,
                                         name=f"kiwi-{self.host}")
        self._thread.start()
        return True

    def stop(self) -> bool:
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=3.0)
        return True

    def work(self, input_items, output_items) -> int:
        out = output_items[0]
        n   = len(out)
        written = 0
        while written < n:
            try:
                iq, gps = self._q.get_nowait()
            except queue.Empty:
                break
            take = min(len(iq), n - written)
            out[written:written + take] = iq[:take]
            # Emituj GPS tag 'rx_time' (format UHD)
            if gps and gps["locked"] and _HAS_GR:
                key = pmt.intern("rx_time")
                val = pmt.cons(pmt.from_uint64(int(gps["t_gps"])),
                               pmt.from_double(gps["t_gps"] % 1.0))
                self.add_item_tag(0, self._offset + written, key, val)
            written += take
        self._offset += written
        return written

    # ── Asyncio loop w watku ─────────────────────────────────────────────────
    def _run_thread(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._ws_loop())
        except Exception as e:
            logger.error("KiwiSDRSource %s error: %s", self.host, e)
        finally:
            try: self._loop.close()
            except: pass

    async def _ws_loop(self) -> None:
        try:
            import websockets
        except ImportError:
            raise ImportError("Brak pakietu 'websockets' — pip install websockets")

        uri = f"ws://{self.host}:{self.port}/{int(time.time()*1000)}/SND"
        origin = f"http://{self.host}:{self.port}"
        bw_lo  = -int(self.bandwidth_hz / 2)
        bw_hi  =  int(self.bandwidth_hz / 2)
        cmds = [
            "SET auth t=kiwi p=",
            "SET ident_user=MKIWI-GR",
            "SET geo=",
            f"SET mod=iq low_cut={bw_lo} high_cut={bw_hi} "
            f"freq={self.freq_mhz*1000:.3f}",
            "SET compression=0",
            f"SET agc={'on' if self.agc_on else 'off'} "
            f"hang=0 thresh=-100 slope=6 decay=1000 manGain={self.man_gain}",
            "SET AR OK in=12000 out=12000",
            "SET keepalive",   # KRYTYCZNE: bez tego brak ramek SND w trybie IQ
        ]
        # websockets >= v11 uses 'additional_headers'; v10 used 'extra_headers'
        # websockets.version module removed in v11+; use __version__ instead
        import websockets as _ws_mod
        _ws_major = int(getattr(_ws_mod, '__version__', '10.0').split(".")[0])
        _hdr_kwarg = "additional_headers" if _ws_major >= 11 else "extra_headers"
        _conn_kwargs = {
            _hdr_kwarg: {"Origin": origin, "User-Agent": "multi-kiwi-gr/1.0"},
            "max_size": 2**22,
            "ping_interval": 30,
        }
        async with websockets.connect(uri, **_conn_kwargs) as ws:
            for cmd in cmds:
                await ws.send(cmd)
                await asyncio.sleep(0.05)
            cum = 0
            async for msg in ws:
                if not self._running: break
                if isinstance(msg, str): continue
                iq, seq, rssi, gps = parse_snd_frame(bytes(msg))
                if iq.size == 0: continue
                if gps: gps["sample_offset"] = cum
                self._gps.push(gps, iq.size)
                cum += iq.size
                try:
                    self._q.put_nowait((iq, gps))
                except queue.Full:
                    try: self._q.get_nowait()
                    except queue.Empty: pass
                    self._q.put_nowait((iq, gps))

    # ── Standalone (bez GNU Radio) ───────────────────────────────────────────
    def acquire(self, duration_sec: float = 30.0) -> Tuple[np.ndarray, GpsTimeline]:
        """
        Tryb standalone: zbiera IQ przez duration_sec sekund.
        Zwraca (iq_complex64_koryg, gps_timeline).
        Korekcja dryftu GPS jest aplikowana automatycznie.
        """
        chunks = []
        self.start()
        t_end = time.monotonic() + duration_sec + 1.0
        while time.monotonic() < t_end:
            try:
                iq, _ = self._q.get(timeout=0.5)
                chunks.append(iq)
            except queue.Empty:
                if not self._running: break
        self.stop()
        if not chunks:
            return np.zeros(0, dtype=np.complex64), self._gps
        raw = np.concatenate(chunks)
        return self._gps.resample(raw), self._gps
