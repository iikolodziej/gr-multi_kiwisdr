#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_blocks.py  —  Unit tests for gr-multi_kiwisdr parser and GPS utilities

Run from the repo root:
    python -m pytest gr-multi_kiwisdr/tests/test_blocks.py -v
Or directly:
    cd gr-multi_kiwisdr/tests
    python test_blocks.py
"""
import sys
import os
import struct
import unittest

# Support both: pip install -e . (package) and direct sys.path usage
_python_dir = os.path.join(os.path.dirname(__file__), '..', 'python')
if os.path.isdir(_python_dir):
    sys.path.insert(0, os.path.abspath(_python_dir))

import numpy as np
from kiwisdr_source import parse_snd_frame, GpsTimeline


class TestParseSndFrame(unittest.TestCase):

    def _make_frame(self, seq, smeter, gpssec, gpsnsec, n_samples=512):
        """Build a synthetic SND frame with GPS header."""
        hdr = b'SND' + bytes([0x08])
        hdr += struct.pack('<I', seq)
        hdr += struct.pack('>H', smeter)
        hdr += bytes([0, 0])                          # last_sol=0 (locked), pad
        hdr += struct.pack('<II', gpssec, gpsnsec)    # GPS LE fields
        payload = np.zeros(n_samples * 2, dtype=np.int16).tobytes()
        return hdr + payload

    def test_seq_little_endian(self):
        frame = self._make_frame(seq=42, smeter=2540, gpssec=472296, gpsnsec=500_000_000)
        iq, seq, rssi, gps = parse_snd_frame(frame)
        self.assertEqual(seq, 42, f"seq parsed wrong: {seq}")

    def test_gps_fields_little_endian(self):
        frame = self._make_frame(seq=1, smeter=2540, gpssec=472296, gpsnsec=500_000_000)
        iq, seq, rssi, gps = parse_snd_frame(frame)
        self.assertIsNotNone(gps)
        self.assertEqual(gps['gpssec'], 472296, f"gpssec wrong: {gps['gpssec']}")
        self.assertEqual(gps['gpsnsec'], 500_000_000, f"gpsnsec wrong: {gps['gpsnsec']}")
        self.assertAlmostEqual(gps['t_gps'], 472296.5, places=6)

    def test_rssi_formula(self):
        smeter = 2540
        expected_rssi = 0.1 * smeter - 127.0
        frame = self._make_frame(seq=1, smeter=smeter, gpssec=0, gpsnsec=0)
        _, _, rssi, _ = parse_snd_frame(frame)
        self.assertAlmostEqual(rssi, expected_rssi, places=5)

    def test_gps_locked(self):
        frame = self._make_frame(seq=1, smeter=2000, gpssec=1, gpsnsec=0)
        _, _, _, gps = parse_snd_frame(frame)
        self.assertTrue(gps['locked'])

    def test_invalid_frame_returns_zeros(self):
        iq, seq, rssi, gps = parse_snd_frame(b'BAD_DATA')
        self.assertEqual(iq.size, 0)
        self.assertIsNone(gps)

    def test_iq_shape(self):
        frame = self._make_frame(seq=1, smeter=2000, gpssec=1, gpsnsec=0, n_samples=512)
        iq, _, _, _ = parse_snd_frame(frame)
        self.assertEqual(iq.shape, (512,))
        self.assertEqual(iq.dtype, np.complex64)


class TestGpsTimeline(unittest.TestCase):

    def _build_timeline(self, n_frames=15, fs=12000, drift_ppm=0.0):
        tl = GpsTimeline(fs)
        for k in range(n_frames):
            t = 472296.0 + k * 512 / (fs * (1 + drift_ppm * 1e-6))
            gps = {"locked": True, "t_gps": t, "sample_offset": k * 512}
            tl.push(gps, 512)
        return tl

    def test_fit_no_drift(self):
        tl = self._build_timeline(n_frames=15, fs=12000, drift_ppm=0.0)
        self.assertTrue(tl.fitted)
        self.assertAlmostEqual(tl.fs_eff, 12000.0, delta=0.5)
        self.assertAlmostEqual(tl.drift_ppm, 0.0, delta=1.0)

    def test_fit_with_drift(self):
        tl = self._build_timeline(n_frames=20, fs=12000, drift_ppm=+80.0)
        self.assertTrue(tl.fitted)
        self.assertAlmostEqual(tl.drift_ppm, +80.0, delta=5.0)

    def test_align_positive_offset(self):
        tl = self._build_timeline(n_frames=15)
        iq = np.ones(24000, dtype=np.complex64)
        # Shift by +500 ms (6000 samples at 12 kSps)
        aligned = tl.align(iq, tl.t0_gps - 0.5)
        zeros_at_start = int(np.sum(np.abs(aligned[:6000]) == 0))
        self.assertEqual(zeros_at_start, 6000,
                         f"Expected 6000 zeros at start, got {zeros_at_start}")

    def test_align_no_gps(self):
        tl = GpsTimeline(12000)
        iq = np.ones(1000, dtype=np.complex64)
        result = tl.align(iq, 0.0)
        np.testing.assert_array_equal(result, iq)


class TestImport(unittest.TestCase):
    def test_package_import(self):
        from kiwisdr_source import KiwiSDRSource, GpsTimeline, parse_snd_frame
        from coh_multi_kiwi import CoherentMultiKiwiSource
        try:
            from kiwi_blocks_gr import KiwiMultiSource, KiwiWidebandSource
        except ImportError:
            pass  # GNU Radio not required


if __name__ == "__main__":
    unittest.main(verbosity=2)
