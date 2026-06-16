#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_standalone.py — Demo gr-multi_kiwisdr BEZ GNU Radio
Uruchomienie: python demo_standalone.py
Wymaga tylko: pip install websockets numpy
"""
import sys, os, asyncio, time, logging
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python'))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# ── Test 1: Pojedynczy odbiornik (standalone acquire) ─────────────────────
def test_single():
    print("\n" + "="*60)
    print("  TEST 1: Pojedynczy odbiornik KiwiSDR (LA3L Norway)")
    print("="*60)
    from kiwisdr_source import KiwiSDRSource
    src = KiwiSDRSource(
        host="22207.proxy.kiwisdr.com",
        port=8073,
        freq_mhz=9.996,
        bandwidth_hz=10000,
    )
    print("Akwizycja 10s...")
    iq, gps_tl = src.acquire(duration_sec=10.0)
    print(f"  Probki:    {iq.size}")
    print(f"  RMS IQ:    {float(abs(iq).mean()):.5f}")
    print(f"  fs_eff:    {gps_tl.fs_eff:.3f} Hz")
    print(f"  Drift:     {gps_tl.drift_ppm:+.2f} ppm")
    print(f"  GPS t0:    {gps_tl.t0_gps}")
    print(f"  Fitted:    {gps_tl.fitted}")
    return iq, gps_tl


# ── Test 2: Koherentny wielostacyjny ──────────────────────────────────────
def test_multi():
    print("\n" + "="*60)
    print("  TEST 2: Koherentny wielostacyjny odbiornik (3 stacje)")
    print("="*60)
    from coh_multi_kiwi import CoherentMultiKiwiSource

    stations = [
        ("22207.proxy.kiwisdr.com",     8073, 59.03,  9.96),   # LA3L Norway
        ("kiwisdr-dorohoi.ddns.net",     8073, 47.66, 26.39),   # Dorohoi Romania
        ("kiwi-sdr1-leiden.impactam.nl", 8073, 52.11,  4.57),   # Leiden NL
    ]
    src = CoherentMultiKiwiSource(
        stations=stations,
        freq_mhz=9.996,
        bandwidth_hz=10000,
        duration_sec=15.0,
    )
    print("Rownolegla akwizycja 15s z 3 stacji...")
    channels = asyncio.run(src.acquire())
    src.print_report(channels)

    # Sprawdz wyrownanie GPS
    t0s = [ch.gps_tl.t0_gps for ch in channels if ch.gps_tl.t0_gps]
    if len(t0s) >= 2:
        spread_ms = (max(t0s) - min(t0s)) * 1000
        print(f"\n  Rozrzut GPS t0 po alignment: {spread_ms:.1f} ms")
    return channels


if __name__ == "__main__":
    if "--multi" in sys.argv:
        test_multi()
    elif "--single" in sys.argv:
        test_single()
    else:
        print("Uzycie:")
        print("  python demo_standalone.py --single   (1 stacja, 10s)")
        print("  python demo_standalone.py --multi    (3 stacje, 15s)")
        print()
        print("Uruchamiam test single domyslnie...")
        test_single()
