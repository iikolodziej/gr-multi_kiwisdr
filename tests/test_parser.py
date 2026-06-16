#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_parser.py — testy parsera ramki SND KiwiSDR i artefaktu naglowka GPS.

Pominiecie 10-bajtowego naglowka GPS w ramce SND eliminuje deterministyczny
grzebien widmowy ~23 Hz w PSD obwiedni |IQ|^2. Test buduje syntetyczny
strumien ramek SND z czystym sygnalem
(stala obwiednia) i porownuje dwa parsery:

  * AKTUALNY (parse_snd_frame) — poprawnie pomija naglowek GPS,
  * LEGACY (blad < v2.0)       — pomija tylko 10-bajtowy naglowek SND, a bajty
    GPS interpretuje jako probki int16, wstawiajac je na poczatku kazdego
    pakietu -> periodyczny artefakt na granicach pakietow.

Mierzymy wysokosc grzebienia (peak/floor) w PSD obwiedni i sprawdzamy, ze
aktualny parser redukuje go o co najmniej 10 dB (typowo ~10-11 dB, zaleznie
od amplitudy sygnalu).

Uruchomienie:
    python -m pytest gr-multi_kiwisdr/tests/test_parser.py -v
albo bezposrednio:
    python gr-multi_kiwisdr/tests/test_parser.py
"""
import os
import struct
import sys
import unittest

_python_dir = os.path.join(os.path.dirname(__file__), '..', 'python')
if os.path.isdir(_python_dir):
    sys.path.insert(0, os.path.abspath(_python_dir))

import numpy as np
from kiwisdr_source import (parse_snd_frame, KIWI_SND_HDR_BYTES,
                            KIWI_GPS_HEADER_BYTES)

FS = 12000.0
N_SAMP = 512          # probek complex per pakiet (nominalnie)
N_FRAMES = 600        # ~25 s strumienia


def _build_frame(seq, gpssec, gpsnsec, payload_iq):
    """Buduje binarna ramke SND z naglowkiem GPS i payloadem IQ (int16 BE)."""
    hdr = b'SND' + bytes([0x08])          # marker + flags (bit3 = IQ)
    hdr += struct.pack('<I', seq)         # seq LE
    hdr += struct.pack('>H', 2540)        # smeter BE
    hdr += bytes([0, 0])                  # last_gps_sol=0 (locked) + pad
    hdr += struct.pack('<II', gpssec, gpsnsec)   # gpssec/gpsnsec LE
    inter = np.empty(2 * len(payload_iq), dtype=np.float64)
    inter[0::2] = payload_iq.real
    inter[1::2] = payload_iq.imag
    body = (np.clip(inter, -1.0, 1.0) * 32767.0).astype('>i2').tobytes()
    return hdr + body


def _parse_legacy(data):
    """Parser sprzed v2.0: pomija TYLKO 10-bajtowy naglowek SND (bez GPS).

    Bajty GPS trafiaja na poczatek payloadu jako probki int16 -> artefakt.
    """
    raw = data[KIWI_SND_HDR_BYTES:]
    samp = np.frombuffer(raw, dtype='>i2').astype(np.float32) / 32768.0
    m = len(samp) // 2
    return (samp[0:2 * m:2] + 1j * samp[1:2 * m:2]).astype(np.complex64)


def _comb_db(iq, f_packet):
    """Wysokosc grzebienia [dB] w PSD obwiedni |IQ|^2 przy f_packet i harmon."""
    e = np.abs(iq).astype(np.float64) ** 2
    e = e - e.mean()
    w = np.hanning(len(e))
    E = np.abs(np.fft.rfft(e * w)) ** 2
    f = np.fft.rfftfreq(len(e), 1.0 / FS)
    floor = np.median(E[(f > 1.0) & (f < FS / 2)]) + 1e-30
    pk = 0.0
    for h in (1, 2, 3):
        m = np.abs(f - h * f_packet) < 0.7
        if m.any():
            pk = max(pk, float(E[m].max()))
    return 10.0 * np.log10(pk / floor + 1e-30)


class TestGpsHeaderArtifact(unittest.TestCase):
    """Artefakt grzebienia z nieskipowanego naglowka GPS."""

    def _make_stream(self):
        # Realistyczny baseband HF: szum zespolony (plaska PSD obwiedni -> dobrze
        # zdefiniowany floor). Naglowek GPS narasta jak w prawdziwym strumieniu
        # (gpssec/gpsnsec rosna o 512/fs na pakiet), wiec artefakt nie jest
        # idealnie koherentny - daje realistyczny grzebien (rzedu 10 dB nad
        # floor; dokladna wartosc zalezy od stosunku amplitud sygnal/naglowek).
        rng = np.random.default_rng(2026)
        sig = (rng.standard_normal(N_FRAMES * N_SAMP)
               + 1j * rng.standard_normal(N_FRAMES * N_SAMP)) * 0.2
        t0 = 1_400_000_000.0
        frames = []
        for k in range(N_FRAMES):
            payload = sig[k * N_SAMP:(k + 1) * N_SAMP]
            t = t0 + k * N_SAMP / FS
            gpssec = int(t)
            gpsnsec = int((t - gpssec) * 1e9)
            frames.append(_build_frame(k, gpssec, gpsnsec, payload))
        return frames

    def test_artifact_reduced_by_skipping_gps_header(self):
        frames = self._make_stream()

        cur = np.concatenate([parse_snd_frame(f)[0] for f in frames])
        leg = np.concatenate([_parse_legacy(f) for f in frames])

        f_cur = FS / N_SAMP
        # legacy frame: (GPS 10 B + payload 2048 B) / 2 / 2 = 514 probek compl.
        n_leg_samp = (KIWI_GPS_HEADER_BYTES + N_SAMP * 4) // 2 // 2
        f_leg = FS / n_leg_samp

        # grzebien liczymy WZGLEDEM czestotliwosci kontrolnej (ni-harmonicznej),
        # by oddzielic prawdziwy artefakt pakietyzacji od piedestalu szumu, ktory
        # daje samo pickowanie maksimum po losowym widmie.
        f_ctrl_cur = f_cur * 1.37
        f_ctrl_leg = f_leg * 1.37
        comb_cur = _comb_db(cur, f_cur) - _comb_db(cur, f_ctrl_cur)
        comb_leg = _comb_db(leg, f_leg) - _comb_db(leg, f_ctrl_leg)
        reduction = comb_leg - comb_cur
        print(f"\n  grzebien (vs kontrola) legacy={comb_leg:.1f} dB  "
              f"aktualny={comb_cur:.1f} dB  -> redukcja {reduction:.1f} dB  "
              f"(f_leg={f_leg:.2f} Hz)")

        self.assertGreater(comb_leg, 10.0,
                           "legacy powinien miec wyrazny grzebien przy f_packet")
        self.assertGreaterEqual(reduction, 10.0,
                                f"redukcja {reduction:.1f} dB < 10 dB")

    def test_current_parser_no_strong_comb(self):
        """Dla aktualnego parsera f_packet nie jest wyrozniona vs kontrola."""
        frames = self._make_stream()
        cur = np.concatenate([parse_snd_frame(f)[0] for f in frames])
        f_cur = FS / N_SAMP
        excess = _comb_db(cur, f_cur) - _comb_db(cur, f_cur * 1.37)
        self.assertLess(abs(excess), 4.0,
                        f"aktualny parser nie powinien wyrozniac f_packet "
                        f"(nadwyzka {excess:.1f} dB)")


class TestSndPayloadLength(unittest.TestCase):
    """Aktualny parser zwraca dokladnie N probek (bez wstawiania bajtow GPS)."""

    def test_payload_exact_length(self):
        t = np.arange(N_SAMP) / FS
        payload = 0.3 * np.exp(2j * np.pi * 200.0 * t)
        frame = _build_frame(1, 1_400_000_000, 0, payload)
        iq, seq, rssi, gps = parse_snd_frame(frame)
        self.assertEqual(iq.size, N_SAMP)
        self.assertEqual(gps['gpssec'], 1_400_000_000)
        self.assertTrue(gps['locked'])


if __name__ == "__main__":
    unittest.main(verbosity=2)
