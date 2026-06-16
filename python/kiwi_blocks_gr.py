# -*- coding: utf-8 -*-
"""
kiwi_blocks_gr.py  -  GNU Radio blocks dla KiwiSDR
===================================================

Trzy bloki zrodlowe GNU Radio:

  1. KiwiSDRSource        - pojedynczy odbiornik KiwiSDR (1 wyjscie IQ)
  2. KiwiMultiSource      - wiele KiwiSDR, N wyjsc IQ zsynchronizowanych GPS (TDoA)
  3. KiwiWidebandSource   - wiele socketow do jednego KiwiSDR, stitching (szerokie pasmo)
"""

import asyncio
import logging
import queue
import struct
import threading
import time
from typing import List, Optional, Tuple

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
KIWI_SAMPLES_PER_FRAME = 512
KIWI_PAYLOAD_BYTES     = KIWI_SAMPLES_PER_FRAME * 4
KIWI_GPS_HEADER_BYTES  = 10
KIWI_SND_HDR_BYTES     = 10
KIWI_FRAME_WITH_GPS    = KIWI_SND_HDR_BYTES + KIWI_GPS_HEADER_BYTES + KIWI_PAYLOAD_BYTES
KIWI_FS_NOMINAL        = 12000  # Hz


# ═══════════════════════════════════════════════════════════════════════════
# Parser ramek SND
# ═══════════════════════════════════════════════════════════════════════════

def parse_snd_frame(data: bytes) -> Tuple[np.ndarray, int, float, Optional[dict]]:
    if len(data) < KIWI_SND_HDR_BYTES or data[:3] != b"SND":
        return np.zeros(0, dtype=np.complex64), 0, -127.0, None
    flags  = data[3]
    # seq i GPS sa LITTLE-endian, smeter BIG-endian (zgodnie z kiwiclient/
    # pe_receivers, potwierdzone empirycznie). Wczesniej '>I' dawal zly GPS.
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
            "seq": seq, "last_sol": last_sol,
            "gpssec": gpssec, "gpsnsec": gpsnsec,
            "t_gps": gpssec + gpsnsec * 1e-9,
            "locked": (last_sol == 0),
        }
        payload_off = KIWI_SND_HDR_BYTES + KIWI_GPS_HEADER_BYTES
    raw  = data[payload_off:]
    n    = len(raw) // 2
    if n < 2:
        return np.zeros(0, dtype=np.complex64), seq, rssi, gps
    samp = np.frombuffer(raw, dtype=">i2").astype(np.float32) / 32768.0
    iq   = (samp[0::2] + 1j * samp[1::2]).astype(np.complex64)
    return iq, seq, rssi, gps


def _ws_connect_kwargs(origin):
    """Zwraca kwargs dla websockets.connect() kompatybilne z v10-v16+."""
    try:
        import websockets as _ws
        # websockets >= v11 usunelo modul websockets.version; uzywaj __version__
        ver_str = getattr(_ws, '__version__', '10.0')
        major = int(ver_str.split(".")[0])
    except Exception:
        major = 10
    # 'additional_headers' od v11; 'extra_headers' w v10 i starszych
    hdr_key = "additional_headers" if major >= 11 else "extra_headers"
    return {
        hdr_key: {"Origin": origin, "User-Agent": "multi-kiwi-gr/1.0"},
        "max_size": 2 ** 22,
        "ping_interval": 20,
    }


# ═══════════════════════════════════════════════════════════════════════════
# _KiwiWorker  -  wspolny watek WebSocket dla wszystkich blokow
# ═══════════════════════════════════════════════════════════════════════════

class _KiwiWorker:
    """Jeden watek asyncio pobierajacy IQ z jednego KiwiSDR."""

    def __init__(self, host: str, port: int, freq_mhz: float,
                 bandwidth_hz: float, agc_on: bool = True, man_gain: int = 50,
                 q_size: int = 256):
        # strip() chroni przed wklejonym enterem/spacja w polu hosta w GRC
        self.host = str(host).strip()
        self.port = port
        self.freq_mhz = freq_mhz
        self.bandwidth_hz = bandwidth_hz
        self.agc_on = agc_on
        self.man_gain = man_gain
        self.q: "queue.Queue[Tuple[np.ndarray, Optional[dict]]]" = queue.Queue(q_size)
        self.t0_gps: Optional[float] = None
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._task: Optional[asyncio.Task] = None
        self._cum = 0

    def start(self):
        self._running = True
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"kiwi-{self.host}")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._loop and not self._loop.is_closed() and self._task is not None:
            # Anuluj task (pozwala websockets na czysty shutdown)
            self._loop.call_soon_threadsafe(self._task.cancel)
        if self._thread:
            self._thread.join(timeout=5.0)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_loop())
        finally:
            # Poczekaj na zakonczenie wszystkich pending tasks
            try:
                pending = asyncio.all_tasks(self._loop)
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                self._loop.close()
            except Exception:
                pass

    async def _run_loop(self):
        """Glowna petla z retry i czystym zamknieciem."""
        while self._running:
            self._task = asyncio.current_task()
            try:
                await self._ws_loop()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.warning("KiwiWorker %s: %s - retry in 3s", self.host, e)
                    try:
                        await asyncio.sleep(3)
                    except asyncio.CancelledError:
                        break

    async def _ws_loop(self):
        try:
            import websockets
        except ImportError:
            raise ImportError("pip install websockets")

        uri    = f"ws://{self.host}:{self.port}/{int(time.time()*1000)}/SND"
        origin = f"http://{self.host}:{self.port}"
        half_bw = max(500, min(6000, int(self.bandwidth_hz / 2)))

        conn_kw = _ws_connect_kwargs(origin)
        async with websockets.connect(uri, **conn_kw) as ws:
            # Sekwencja inicjalizacyjna odwzorowana z kiwirecorder.py / pe_receivers.py
            # KOLEJNOSC I "SET keepalive" MA ZNACZENIE — bez keepalive brak SND w IQ mode
            for cmd in [
                "SET auth t=kiwi p=",
                "SET ident_user=MKIWI-GR",
                "SET geo=",
                "SET geojson=",
                "SET nb algo=0",
                "SET nb param=0,0",
                "SET nb=0",
                "SET squelch=0 max=0",
                "SET lms_autonotch=0",
                "SET genattn=0",
                "SET gen=0 mix=-1",
                f"SET mod=iq low_cut=-{half_bw} high_cut={half_bw} freq={self.freq_mhz*1000:.3f}",
                f"SET agc={'1' if self.agc_on else '0'} hang=0 thresh=-100 slope=6 decay=1000 manGain={self.man_gain}",
                "SET compression=0",
                "SET AR OK in=12000 out=12000",
                "SET keepalive",   # KRYTYCZNE: bez tego brak ramek SND w trybie IQ
            ]:
                await ws.send(cmd)
                await asyncio.sleep(0.03)

            # Odbieraj ramki SND (i ignoruj MSG)
            async for msg in ws:
                if not self._running:
                    break
                if not isinstance(msg, bytes):
                    continue
                if msg[:3] == b"MSG":
                    text = msg[4:].decode("utf-8", errors="replace")
                    if "badp=1" in text:
                        raise ConnectionRefusedError(f"{self.host}: badp=1 (brak slotow/haslo)")
                    continue
                if msg[:3] != b"SND":
                    continue
                iq, seq, rssi, gps = parse_snd_frame(msg)
                if iq.size == 0:
                    continue
                if gps and gps["locked"] and self.t0_gps is None:
                    self.t0_gps = gps["t_gps"]
                self._cum += iq.size
                try:
                    self.q.put_nowait((iq, gps))
                except queue.Full:
                    try:
                        self.q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.q.put_nowait((iq, gps))
                    except queue.Full:
                        pass


# ═══════════════════════════════════════════════════════════════════════════
# Blok 1: KiwiSDRSource  (pojedynczy odbiornik)
# ═══════════════════════════════════════════════════════════════════════════

class KiwiSDRSource(gr.sync_block if _HAS_GR else object):
    """
    GNU Radio source block: jeden KiwiSDR -> complex64 IQ.

    Wyjscie: 1x complex64
    Stream tagi: 'rx_time' (UHD format) z GPS timestamps.
    """

    def __init__(self, host: str, port: int = 8073,
                 freq_mhz: float = 9.996, bandwidth_hz: float = 10_000.0,
                 agc_on: bool = True, man_gain: int = 50):
        if _HAS_GR:
            gr.sync_block.__init__(self, "KiwiSDR Source",
                                   in_sig=None, out_sig=[np.complex64])
        self._w = _KiwiWorker(host, port, freq_mhz, bandwidth_hz, agc_on, man_gain)
        self._offset = 0

    def start(self):
        self._w.start()
        return True

    def stop(self):
        self._w.stop()
        return True

    def work(self, input_items, output_items):
        out = output_items[0]
        n   = len(out)
        written = 0
        while written < n:
            try:
                iq, gps = self._w.q.get(timeout=0.05)
            except queue.Empty:
                break
            take = min(len(iq), n - written)
            out[written:written + take] = iq[:take]
            if gps and gps.get("locked") and _HAS_GR:
                key = pmt.intern("rx_time")
                val = pmt.cons(pmt.from_uint64(int(gps["t_gps"])),
                               pmt.from_double(gps["t_gps"] % 1.0))
                self.add_item_tag(0, self._offset + written, key, val)
            written += take
        self._offset += written
        return written


# ═══════════════════════════════════════════════════════════════════════════
# Blok 2: KiwiMultiSource  (N KiwiSDR, N wyjsc IQ, GPS-aligned TDoA)
# ═══════════════════════════════════════════════════════════════════════════

class KiwiMultiSource(gr.sync_block if _HAS_GR else object):
    """
    GNU Radio source block: N KiwiSDR -> N wyjsc complex64 IQ.

    Kazde wyjscie odpowiada jednej stacji odbiorczej.
    Strumienie sa wstepnie wyrownane GPS (t0 pierwszej stacji jako referencja).
    Uzycie do TDoA: podlacz N wyjsc do bloku korelatora/rejestratora.

    Parametry:
      hosts_csv  - adresy KiwiSDR oddzielone przecinkiem
                   np. "kiwi1.example.com,kiwi2.example.com,kiwi3.example.com"
      num_ch     - liczba kanalow (musi zgadzac sie z liczba adresow)
      port       - port WebSocket (domyslnie 8073)
      freq_mhz   - czestotliwosc strojenia [MHz]
      bandwidth_hz - szerokosc pasma IQ [Hz]
    """

    def __init__(self, hosts_csv: str, num_ch: int = 3,
                 port: int = 8073, freq_mhz: float = 9.996,
                 bandwidth_hz: float = 10_000.0):
        hosts = [h.strip() for h in hosts_csv.split(',') if h.strip()]
        if len(hosts) < num_ch:
            hosts += [hosts[-1]] * (num_ch - len(hosts))
        hosts = hosts[:num_ch]
        if _HAS_GR:
            gr.sync_block.__init__(self, "KiwiSDR Multi-Source",
                                   in_sig=None,
                                   out_sig=[np.complex64] * num_ch)
        self._workers = [
            _KiwiWorker(h, port, freq_mhz, bandwidth_hz) for h in hosts
        ]
        self._num_ch  = num_ch
        self._offsets = [0] * num_ch

    def start(self):
        for w in self._workers:
            w.start()
        return True

    def stop(self):
        for w in self._workers:
            w.stop()
        return True

    def work(self, input_items, output_items):
        n = len(output_items[0])
        written = [0] * self._num_ch

        # Zbierz dane ze wszystkich kanalow
        for ch, w in enumerate(self._workers):
            out = output_items[ch]
            while written[ch] < n:
                try:
                    iq, gps = w.q.get(timeout=0.05)
                except queue.Empty:
                    break
                take = min(len(iq), n - written[ch])
                out[written[ch]:written[ch] + take] = iq[:take]
                if gps and gps.get("locked") and _HAS_GR:
                    key = pmt.intern("rx_time")
                    val = pmt.cons(pmt.from_uint64(int(gps["t_gps"])),
                                   pmt.from_double(gps["t_gps"] % 1.0))
                    self.add_item_tag(ch, self._offsets[ch] + written[ch], key, val)
                written[ch] += take
            self._offsets[ch] += written[ch]

        # Zwroc minimum (sync_block wymaga tej samej liczby probek na wszystkich wyjsciach)
        return min(written) if written else 0


# ═══════════════════════════════════════════════════════════════════════════
# Blok 3: KiwiWidebandSource  (wiele socketow -> jeden KiwiSDR, szersze pasmo)
# ═══════════════════════════════════════════════════════════════════════════

class KiwiWidebandSource(gr.sync_block if _HAS_GR else object):
    """
    GNU Radio source block: M socketow do jednego KiwiSDR -> 1 wyjscie wideband IQ.

    KiwiSDR obsługuje do 3-4 jednoczesnych polaczen IQ z jednego urzadzenia.
    Kazde polaczenie dostaje inny kanal czestotliwosci, nastepnie sa one
    sklejane w czestotliwosci (frequency stitching) i podawane jako jeden
    szerszy sygnal IQ.

    Przyklad: center=9.996 MHz, num_slots=3, slot_bw=12000 Hz
      -> slot 0: 9.990 MHz  (+/-6 kHz)
      -> slot 1: 9.996 MHz  (+/-6 kHz)   <- kanal srodkowy
      -> slot 2: 10.002 MHz (+/-6 kHz)
      -> total BW: ~36 kHz (z zachodzeniem)

    Parametry:
      host         - adres KiwiSDR
      port         - port WebSocket (domyslnie 8073)
      center_mhz   - czestotliwosc centralna [MHz]
      num_slots    - liczba slotow (2, 3 lub 4)
      slot_bw_hz   - szerokosc IQ kazdego slota [Hz] (maks ~12000 Hz)
      overlap_hz   - zachodzenie kanalow [Hz] (domyslnie 1000 Hz)
    """

    def __init__(self, host: str, port: int = 8073,
                 center_mhz: float = 9.996, num_slots: int = 3,
                 slot_bw_hz: int = 10000, overlap_hz: int = 1000):
        self._host       = host
        self._port       = port
        self._center_mhz = center_mhz
        self._num_slots  = num_slots
        self._slot_bw    = slot_bw_hz
        self._overlap    = overlap_hz
        self._step_hz    = slot_bw_hz - overlap_hz
        self._total_bw   = int(num_slots * self._step_hz + overlap_hz)
        self._fs         = KIWI_FS_NOMINAL  # kazdy slot ma 12 kHz

        # Czestotliwosci centralnych slotow
        half = (num_slots - 1) / 2.0
        self._slot_freqs_mhz = [
            center_mhz + (i - half) * self._step_hz / 1e6
            for i in range(num_slots)
        ]

        if _HAS_GR:
            gr.sync_block.__init__(self, "KiwiSDR Wideband",
                                   in_sig=None, out_sig=[np.complex64])

        # Jeden worker na slot
        self._workers = [
            _KiwiWorker(host, port, f, slot_bw_hz)
            for f in self._slot_freqs_mhz
        ]

        # Bufor do sklejania
        self._buffers: List[np.ndarray] = [np.zeros(0, dtype=np.complex64)
                                            for _ in range(num_slots)]
        self._stitch_size = 1024   # probki na slot per stitch
        self._offset = 0

    def start(self):
        for w in self._workers:
            w.start()
        return True

    def stop(self):
        for w in self._workers:
            w.stop()
        return True

    def work(self, input_items, output_items):
        out   = output_items[0]
        n_out = len(out)
        written = 0

        while written < n_out:
            # Zbierz STITCH_SIZE probek z kazdego slota do buforow
            for ch, w in enumerate(self._workers):
                while len(self._buffers[ch]) < self._stitch_size:
                    try:
                        iq, _ = w.q.get(timeout=0.02)
                        self._buffers[ch] = np.concatenate([self._buffers[ch], iq])
                    except queue.Empty:
                        break

            # Sprawdz czy wszystkie sloty maja wystarczajaco danych
            if any(len(b) < self._stitch_size for b in self._buffers):
                break

            # Stitch: sklejanie w dziedzinie czestotliwosci
            stitched = self._stitch([b[:self._stitch_size] for b in self._buffers])
            self._buffers = [b[self._stitch_size:] for b in self._buffers]

            take = min(len(stitched), n_out - written)
            out[written:written + take] = stitched[:take]
            written += take

        self._offset += written
        return written

    def _stitch(self, slots: List[np.ndarray]) -> np.ndarray:
        """
        Skleja N slotow IQ w jeden szerszy sygnal przez FFT overlap-add.

        Kazdy slot jest przesuwany do swojej czestotliwosci i sumowany
        w dziedzinie czestotliwosci.
        """
        N    = self._stitch_size
        N_wb = N * self._num_slots
        win  = np.hanning(N).astype(np.float32)
        spec = np.zeros(N_wb, dtype=np.complex64)

        for i, iq in enumerate(slots):
            # Przesuniecie czestotliwosci slota wzgledem centrum
            half  = (self._num_slots - 1) / 2.0
            f_off = (i - half) * self._step_hz  # Hz
            # Modulacja czestotliwosci: przesun slot do wlasciwej pozycji
            t   = np.arange(N, dtype=np.float32) / self._fs
            mix = np.exp(1j * 2 * np.pi * f_off * t).astype(np.complex64)
            iq_shifted = iq * mix * win

            # FFT slota i wstaw do widma wideband
            S    = np.fft.fft(iq_shifted, N)
            # Pozycja w buforze wideband
            bin_offset = int(round(i * self._step_hz / self._fs * N_wb))
            for k in range(N):
                dst = (bin_offset + k - N // 2) % N_wb
                spec[dst] += S[k]

        # IFFT -> wideband IQ
        wb = np.fft.ifft(spec).astype(np.complex64)
        # Normalizacja
        peak = np.max(np.abs(wb))
        if peak > 0:
            wb /= peak * self._num_slots
        return wb
