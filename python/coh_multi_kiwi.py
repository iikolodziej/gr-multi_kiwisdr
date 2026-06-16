# -*- coding: utf-8 -*-
"""
coh_multi_kiwi.py — Koherentny wielostacyjny odbiornik KiwiSDR
==============================================================

CoherentMultiKiwiSource:
  Rownolegla akwizycja z N stacji KiwiSDR + GPS t0 alignment.
  Wynik: lista wyrownanych tablic complex64 (jednej dlugosci).

  Roznica wzgledem gr-kiwisdr/coh_stream_synth (Mayer):
    Mayer:  3 strumienie z 1 KiwiSDR, korekcja fazy spektralnej
    Ten:    N stacji z N roznych lokalizacji, korekcja GPS t0 w czasie
            Przeznaczenie: TDoA multi-stacyjne (rozne wspolrzedne RX)

Przykladowe uzycie:
  import asyncio
  from coh_multi_kiwi import CoherentMultiKiwiSource

  stations = [
      ("22207.proxy.kiwisdr.com",      8073, 59.03,  9.96),
      ("kiwisdr-dorohoi.ddns.net",      8073, 47.66, 26.39),
      ("kiwi-sdr1-leiden.impactam.nl",  8073, 52.11,  4.57),
  ]
  src = CoherentMultiKiwiSource(stations, freq_mhz=9.996, duration_sec=30.0)
  channels = asyncio.run(src.acquire())
  for ch in channels:
      print(ch["host"], ch["iq"].shape, ch["drift_ppm"])
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

try:
    from .kiwisdr_source import KiwiSDRSource, GpsTimeline
except ImportError:
    from kiwisdr_source import KiwiSDRSource, GpsTimeline  # flat sys.path usage

logger = logging.getLogger(__name__)


@dataclass
class KiwiChannel:
    """Wynik akwizycji jednej stacji po GPS alignment."""
    host:       str
    port:       int
    lat:        float
    lon:        float
    iq:         np.ndarray      # complex64, po korekcji dryftu + GPS t0
    gps_tl:     GpsTimeline
    rssi_dbm:   float
    n_samples:  int
    drift_ppm:  float


class CoherentMultiKiwiSource:
    """
    Rownolegla akwizycja z N stacji KiwiSDR z GPS t0 alignment.

    Parametry:
      stations     — lista krotek (host, port, lat, lon)
      freq_mhz     — czestotliwosc [MHz]
      bandwidth_hz — pasmo IQ [Hz]
      duration_sec — czas nagrania [s]
      correct_phase— opcja korekcji fazy (dla stacji z 1 KiwiSDR)
    """

    def __init__(self,
                 stations: List[Tuple[str, int, float, float]],
                 freq_mhz: float = 9.996,
                 bandwidth_hz: float = 10_000.0,
                 duration_sec: float = 30.0,
                 correct_phase: bool = False):
        self.stations     = stations
        self.freq_mhz     = freq_mhz
        self.bandwidth_hz = bandwidth_hz
        self.duration_sec = duration_sec
        self.correct_phase = correct_phase

    async def _one_station(self, host: str, port: int,
                            lat: float, lon: float) -> KiwiChannel:
        src = KiwiSDRSource(host=host, port=port,
                             freq_mhz=self.freq_mhz,
                             bandwidth_hz=self.bandwidth_hz)
        try:
            iq, gps_tl = src.acquire(duration_sec=self.duration_sec)
        except Exception as e:
            logger.error("%s akwizycja blad: %s", host, e)
            iq = np.zeros(0, dtype=np.complex64)
            gps_tl = GpsTimeline()

        rssi = float(np.mean(20 * np.log10(np.abs(iq) + 1e-7))) if iq.size > 0 else -127.0
        return KiwiChannel(
            host=host, port=port, lat=lat, lon=lon,
            iq=iq, gps_tl=gps_tl,
            rssi_dbm=rssi,
            n_samples=iq.size,
            drift_ppm=gps_tl.drift_ppm,
        )

    async def acquire(self) -> List[KiwiChannel]:
        """
        Rownolegla akwizycja + GPS alignment + opcjonalna korekcja fazy.
        Zwraca liste KiwiChannel z wyrowanymi strumieniami IQ.
        """
        channels = await asyncio.gather(
            *[self._one_station(h, p, la, lo) for h, p, la, lo in self.stations]
        )

        # GPS t0 alignment: wyrownaj wszystkie strumienie do stacji ref
        ref_t0 = None
        for ch in channels:
            if ch.gps_tl.fitted and ch.gps_tl.t0_gps is not None:
                ref_t0 = ch.gps_tl.t0_gps
                logger.info("GPS ref t0 = %.6f s (%s)", ref_t0, ch.host)
                break

        if ref_t0 is None:
            logger.warning("Brak GPS t0 — pomijam alignment")
        else:
            for ch in channels:
                if ch.iq.size > 0:
                    ch.iq = ch.gps_tl.align(ch.iq, ref_t0)

        # Opcjonalna korekcja fazy miedzy strumieniami
        # (przydatna gdy wiele socketow z 1 KiwiSDR jak u Mayera)
        if self.correct_phase:
            channels = _phase_correct(channels)

        # Obciecie do wspolnej dlugosci
        sizes = [ch.iq.size for ch in channels if ch.iq.size > 0]
        if sizes:
            mn = min(sizes)
            for ch in channels:
                if ch.iq.size > mn:
                    ch.iq = ch.iq[:mn]

        return channels

    def print_report(self, channels: List[KiwiChannel]) -> None:
        print("=" * 70)
        print(f"  CoherentMultiKiwiSource @ {self.freq_mhz:.3f} MHz "
              f"({len(channels)} stacji)")
        print("=" * 70)
        print(f"  {'Host':<32} {'Lat':>6} {'Lon':>7} "
              f"{'N':>7} {'RSSI':>6} {'Drift':>9} {'t0_GPS':>14}")
        print("  " + "-" * 82)
        for ch in channels:
            t0s = f"{ch.gps_tl.t0_gps:.3f}" if ch.gps_tl.t0_gps else "N/A"
            print(f"  {ch.host:<32} {ch.lat:>6.2f} {ch.lon:>7.2f} "
                  f"{ch.n_samples:>7} {ch.rssi_dbm:>6.1f} "
                  f"{ch.drift_ppm:>+8.1f}ppm  {t0s:>14}")
        print("=" * 70)


def _phase_correct(channels: List[KiwiChannel]) -> List[KiwiChannel]:
    """
    Korekcja resztkowego przesuniecia fazy (metoda Mayera: cross-power spectrum).
    Przydatna dla N socketow z 1 KiwiSDR (wideband stitching).
    Dla roznych stacji TDoA nie jest potrzebna (kazda ma niezalezna faze).
    """
    ref = channels[0]
    n   = min(ch.iq.size for ch in channels if ch.iq.size > 0)
    if n == 0:
        return channels
    R = np.fft.rfft(ref.iq[:n])
    for ch in channels[1:]:
        if ch.iq.size == 0: continue
        S = np.fft.rfft(ch.iq[:n])
        cross = R * np.conj(S)
        phi   = np.angle(np.mean(cross / (np.abs(cross) + 1e-12)))
        ch.iq = (ch.iq * np.exp(1j * phi)).astype(np.complex64)
        logger.debug("Korekcja fazy %s: %.1f deg", ch.host, np.degrees(phi))
    return channels
