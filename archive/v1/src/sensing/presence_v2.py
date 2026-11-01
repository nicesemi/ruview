"""Presence v2 — motion path + breathing-path, calibrated on the real ESP32.

Why this exists
---------------
The stock classifier decides presence from the variance of the *wideband*
mean-amplitude series (10 s window, threshold 0.5 dBm^2). Measured on this
hardware the empty room already sits far above that threshold, because the
variance is dominated by common-mode gain / interference jitter that is shared
by every subcarrier — not by human multipath. Consequence: presence was pinned
to True in an empty room, and a person sitting still actually *lowered* the
variance (26.1 -> 16.7 per-frame, 3.28 -> 2.63 in a 1 s window), so variance
carries motion information only.

Calibration (2026-11-01, ESP32-S3, 306 subcarriers, 27 fps)

    empty room 60 s          1 s-window mean-amp variance   3.3
                             breathing-share subcarriers     1 %
    person sitting 190 s     1 s-window mean-amp variance   2.6
                             breathing-share subcarriers    40 %
    person walking           1 s-window mean-amp variance  >= 35

So presence is the OR of two independent paths:
    motion    : 1 s window mean-amplitude variance >= 8.0
    breathing : >= 20 % of subcarriers carry >20 % of their 0.05-5 Hz energy
                inside the 0.13-0.5 Hz breathing band (60 s window)

Breathing rate is only reported once the breathing path holds, using the
spectral peak of the high-SNR subcarriers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

# -- calibrated thresholds ---------------------------------------------------
MOTION_WIN_S = 1.0
# Motion is measured on gain-normalised amplitudes: the ESP32 AGC flips the
# whole band between two gain states, so the raw mean amplitude jumps 13 <-> 25
# even in an empty room and its 1 s variance (28-36) carries no human signal.
# Normalising each frame by its own mean removes that common-mode gain step;
# what remains is per-subcarrier multipath motion.
MOTION_STD_TH = 0.30   # empty-room p90 = 0.14, seated p90 = 0.12 -> 2x headroom
MOTION_MIN_FRAMES = 6
BREATH_WIN_S = 60.0
BREATH_MIN_S = 25.0
TOTAL_BAND = (0.05, 5.0)     # denominator
BREATH_BAND = (0.13, 0.5)    # 7.8 - 30 breaths/min
SC_RATIO_TH = 0.20           # per-subcarrier breathing energy share
SC_SHARE_TH = 0.20           # fraction of subcarriers that must clear it
MIN_FRAMES = 32

# -- phase path (calibrated 2026-11-02, empty 150 s vs seated 125 s) ----------
# The amplitude-domain breathing verdict does not survive contact with a real
# empty room: measured on this bridge an empty room reaches a breathing share of
# 0.74 (median) while a seated person reaches only 0.62, i.e. the criterion is
# inverted, because the amplitude series is set by common-mode gain/interference
# drift (PSD slope ~ -0.75) rather than by human multipath.  The phase-difference
# series is what separates them, and it separates cleanly:
#     empty room   median per-SC std of detrended phase diff  0.032 - 0.041 rad
#     seated       median per-SC std of detrended phase diff  0.113 - 0.138 rad
# 2.8x with no overlap, and it is width-agnostic (broadband micro-motion, so no
# single narrow band has to be trusted).  Note the spectrum shows no narrow
# breathing peak on either side, so bpm stays unreportable on this hardware.
PHASE_STD_TH = 0.07
PHASE_MIN_FRAMES = 64
BREATHING_ENABLED = False    # amplitude breathing path is inverted: keep for audit only


@dataclass
class PresenceResult:
    presence: bool
    motion_active: bool
    breathing_present: bool
    motion_score: float
    sc_share: float
    n_sc: int
    n_frames: int
    window_s: float
    breathing_bpm: Optional[float] = None
    phase_std: float = 0.0

    def as_dict(self) -> dict:
        return {
            "presence": self.presence,
            "motion_active": self.motion_active,
            "breathing_present": self.breathing_present,
            "motion_score": round(self.motion_score, 3),
            "breathing_sc_share": round(self.sc_share, 3),
            "phase_std": round(self.phase_std, 4),
            "subcarriers": self.n_sc,
            "window_frames": self.n_frames,
            "window_s": round(self.window_s, 1),
            "breathing_bpm": None if self.breathing_bpm is None else round(self.breathing_bpm, 1),
        }


def _uniform(ts_series, mat, min_gap: float = 0.05):
    """Collapse burst-identical frames and resample onto an even time grid.

    The node emits ~3 CSI snapshots every 102 ms.  All three share almost the
    same wall-clock arrival time, so treating them as independent samples
    inflates the apparent sample rate (fs is derived from frames/span) and
    stretches the frequency axis, which drags the 0.13-0.5 Hz breathing band
    down onto the slow AGC drift.  Collapse each burst, then interpolate onto a
    uniform grid so the FFT sees honest time spacing.
    """
    ts = np.asarray(ts_series, dtype=np.float64)
    m = np.asarray(mat, dtype=np.float64)
    keep_t, keep_m = [], []
    i, n = 0, len(ts)
    while i < n:
        j = i + 1
        while j < n and (ts[j] - ts[i]) < min_gap:
            j += 1
        keep_t.append(ts[i])
        keep_m.append(m[i:j].mean(axis=0))
        i = j
    t = np.asarray(keep_t, dtype=np.float64)
    mm = np.asarray(keep_m, dtype=np.float64)
    if len(t) < 8:
        return t, mm
    step = float(np.median(np.diff(t)))
    if not np.isfinite(step) or step <= 0:
        return t, mm
    grid = np.arange(t[0], t[-1] + 1e-9, step)
    if len(grid) < 8:
        return t, mm
    out = np.empty((len(grid), mm.shape[1]), dtype=np.float64)
    for k in range(mm.shape[1]):
        out[:, k] = np.interp(grid, t, mm[:, k])
    return grid, out


def _amp_of(d) -> np.ndarray:
    """Per-frame amplitude vector, whether the frame holds I/Q pairs or amps."""
    a = np.asarray(d, dtype=np.float64)
    if a.ndim == 2 and a.shape[1] >= 2:
        return np.hypot(a[:, 0], a[:, 1])
    return a.reshape(-1)


def _phase_std(hist: Sequence[Tuple[float, int, np.ndarray]], now: float
               ) -> Tuple[float, int, int, float]:
    """Median per-SC std of the detrended neighbouring-SC phase difference.

    Presence on this hardware lives in the phase, not in the amplitude: an empty
    room drifts but its phase difference across subcarriers stays put (0.032 -
    0.041 rad), while a seated body perturbs it by 3x (0.113 - 0.138 rad).  The
    common delay ramp is removed by taking the difference between neighbouring
    subcarriers, and the slow drift by a per-column linear detrend.
    """
    rows = [h for h in hist if now - h[0] <= BREATH_WIN_S]
    iq_rows = [h for h in rows if np.asarray(h[2]).ndim == 2]
    if len(iq_rows) < PHASE_MIN_FRAMES:
        return 0.0, 0, len(iq_rows), 0.0

    # Same group-vs-span selection as the breathing path: the node interleaves
    # 306/190/128/64-SC frames and only one group covers the whole window.
    best = None
    for cand in set(h[1] for h in iq_rows):
        sel = [(h[0], np.asarray(h[2], dtype=np.float64)) for h in iq_rows if h[1] == cand]
        if len(sel) < PHASE_MIN_FRAMES:
            continue
        ts_c = np.asarray([s[0] for s in sel])
        span = float(ts_c[-1] - ts_c[0])
        if span <= 0 or span < BREATH_MIN_S:
            continue
        score = (span, len(sel))
        if best is None or score > best[0]:
            best = (score, cand, sel, span)
    if best is None:
        return 0.0, 0, len(iq_rows), 0.0

    _, n_sc, sel, span = best
    ts_arr = np.asarray([s[0] for s in sel], dtype=np.float64)
    mat = np.vstack([s[1] for s in sel])              # (frames, n_sc*2)
    try:
        mat = mat.reshape(len(sel), -1, 2)
        ph = np.unwrap(np.arctan2(mat[:, :, 1], mat[:, :, 0]), axis=1)
        dphi = np.diff(ph, axis=1)                    # (frames, n_sc-1)
    except Exception:
        return 0.0, n_sc, len(sel), span
    if dphi.shape[1] < 4:
        return 0.0, n_sc, len(sel), span

    ts_u, dphi = _uniform(ts_arr, dphi)
    if len(ts_u) < PHASE_MIN_FRAMES:
        return 0.0, n_sc, len(sel), span
    span = float(ts_u[-1] - ts_u[0])

    # Per-column linear detrend: AGC / thermal drift is common to every column
    # and would otherwise dominate the std.
    t = np.arange(len(ts_u), dtype=np.float64)[:, None]
    tc = t - t.mean()
    x = dphi - dphi.mean(axis=0, keepdims=True)
    slope = (tc * x).sum(axis=0) / float((tc ** 2).sum())
    x = x - tc * slope
    x = x - np.median(x, axis=0, keepdims=True)
    return float(np.median(np.std(x, axis=0))), n_sc, len(ts_u), span


def _motion_score(hist: Sequence[Tuple[float, int, np.ndarray]], now: float) -> float:
    """Gain-normalised per-subcarrier spread inside the last MOTION_WIN_S.

    Returns the 90th percentile of the per-subcarrier standard deviation of
    ``amp / mean_amp``. Empty room ~0.10-0.14, seated ~0.08-0.12, a walking
    person moves every subcarrier far harder than the AGC step does.
    """
    rows = [h for h in hist if now - h[0] <= MOTION_WIN_S]
    if len(rows) < MOTION_MIN_FRAMES:
        return 0.0
    counts: dict = {}
    for _, n_sc, _ in rows:
        counts[n_sc] = counts.get(n_sc, 0) + 1
    n_sc = max(counts, key=counts.get)
    sel = [h[2] for h in rows if h[1] == n_sc]
    if len(sel) < MOTION_MIN_FRAMES:
        return 0.0
    mat = np.vstack([_amp_of(d) for d in sel])
    _t_sel = np.asarray([h[0] for h in rows if h[1] == n_sc], dtype=np.float64)
    try:
        _tu, mat = _uniform(_t_sel, mat)
    except Exception:
        pass
    if mat.ndim != 2 or len(mat) < MOTION_MIN_FRAMES:
        return 0.0
    mean = mat.mean(axis=1, keepdims=True)
    if not np.all(mean > 0):
        return 0.0
    norm = mat / mean
    return float(np.percentile(norm.std(axis=0, ddof=1), 90))


def _breathing(hist: Sequence[Tuple[float, int, np.ndarray]], now: float
               ) -> Tuple[float, int, int, float, Optional[float]]:
    """Per-subcarrier breathing-band share -> (share, n_sc, n_frames, win_s, bpm)."""
    # Group by subcarrier count: the node mixes 306 / 190 / 128 / 64 SC frames.
    rows = [h for h in hist if now - h[0] <= BREATH_WIN_S]
    if len(rows) < MIN_FRAMES:
        return 0.0, 0, len(rows), 0.0, None

    # Pick the group that actually spans a usable window: the node interleaves
    # 306/190/128/64-SC frames, and a group can dominate the frame count while
    # covering far less than BREATH_MIN_S of wall time.
    best = None
    for cand in set(ns for _, ns, _ in rows):
        sel_c = [(ts, amp) for ts, ns, amp in rows if ns == cand]
        if len(sel_c) < MIN_FRAMES:
            continue
        ts_c = np.asarray([s[0] for s in sel_c], dtype=np.float64)
        span_c = float(ts_c[-1] - ts_c[0])
        if span_c <= 0:
            continue
        fs_c = (len(sel_c) - 1) / span_c
        if fs_c < 2.0 or span_c < BREATH_MIN_S:
            continue
        score_c = (span_c, fs_c, len(sel_c))
        if best is None or score_c > best[0]:
            best = (score_c, cand, sel_c, span_c, fs_c)
    if best is None:
        return 0.0, 0, len(rows), 0.0, None

    _, n_sc, sel, span, fs = best
    ts_arr = np.asarray([s[0] for s in sel], dtype=np.float64)
    mat = np.vstack([_amp_of(s[1]) for s in sel])  # (frames, n_sc)
    # Burst frames share one capture instant: collapse them and work on an
    # evenly spaced grid, otherwise fs is inflated ~3x and the breathing band
    # lands on AGC drift (empty rooms then read as breathing).
    ts_u, mat_u = _uniform(ts_arr, mat)
    if len(ts_u) >= MIN_FRAMES:
        span = float(ts_u[-1] - ts_u[0])
        fs = (len(ts_u) - 1) / span if span > 0 else fs
        ts_arr, mat = ts_u, mat_u
        sel = [None] * len(ts_u)

    x = mat - mat.mean(axis=0, keepdims=True)
    win = np.hanning(len(sel))[:, None]
    spec = np.abs(np.fft.rfft(x * win, axis=0)) ** 2
    freqs = np.fft.rfftfreq(len(sel), d=1.0 / fs)

    total_band = (freqs >= TOTAL_BAND[0]) & (freqs <= TOTAL_BAND[1])
    breath_band = (freqs >= BREATH_BAND[0]) & (freqs <= BREATH_BAND[1])
    if not total_band.any() or not breath_band.any():
        return 0.0, n_sc, len(sel), span, None

    total = spec[total_band].sum(axis=0)
    breath = spec[breath_band].sum(axis=0)
    usable = total > 0
    if not usable.any():
        return 0.0, n_sc, len(sel), span, None

    ratio = np.zeros_like(total)
    ratio[usable] = breath[usable] / total[usable]
    n_use = int(usable.sum())
    share = float((ratio[usable] > SC_RATIO_TH).sum()) / float(n_use)

    bpm: Optional[float] = None
    if share >= SC_SHARE_TH:
        order = np.argsort(ratio)[::-1]
        top = [i for i in order if usable[i]][: max(1, n_use // 5)]
        peaks: List[float] = []
        for i in top:
            seg = spec[breath_band, i]
            if seg.size:
                f = freqs[breath_band][int(np.argmax(seg))]
                if f > 0:
                    peaks.append(float(f))
        if peaks:
            bpm = float(np.median(peaks)) * 60.0

    return share, n_sc, len(sel), span, bpm


def evaluate(sc_hist: Sequence[Tuple[float, int, np.ndarray]],
             mean_amps: Optional[Sequence[Tuple[float, float]]] = None,
             now: Optional[float] = None) -> PresenceResult:
    """Combine the motion and breathing paths into one presence decision.

    ``mean_amps`` is accepted for call-site compatibility but no longer used:
    the wideband mean-amplitude series is dominated by AGC gain steps.
    """
    now = now if now is not None else (sc_hist[-1][0] if sc_hist else 0.0)
    mscore = _motion_score(sc_hist, now)
    pstd, p_nsc, p_frames, p_span = _phase_std(sc_hist, now)
    if BREATHING_ENABLED:
        share, n_sc, n_frames, span, bpm = _breathing(sc_hist, now)
    else:
        share, n_sc, n_frames, span, bpm = 0.0, p_nsc, p_frames, p_span, None

    motion_active = mscore >= MOTION_STD_TH
    # Amplitude breathing share is inverted on this hardware (empty 0.74 vs
    # seated 0.62), so presence rides on motion + phase.
    breathing_present = bool(BREATHING_ENABLED and share >= SC_SHARE_TH)
    phase_active = pstd >= PHASE_STD_TH
    return PresenceResult(
        presence=bool(motion_active or breathing_present or phase_active),
        motion_active=bool(motion_active),
        breathing_present=breathing_present,
        motion_score=mscore,
        sc_share=share,
        n_sc=n_sc,
        n_frames=n_frames,
        window_s=span,
        breathing_bpm=bpm if breathing_present else None,
        phase_std=pstd,
    )

class PresenceSmoother:
    """Majority vote over the last N decisions.

    The 60 s breathing window is re-estimated 10x per second from overlapping
    data, so a single unlucky frame group can flip the verdict for a tick.
    Voting removes that flicker without adding perceptible latency.
    """

    def __init__(self, window: int = 7) -> None:
        from collections import deque
        self._buf = deque(maxlen=max(3, int(window)))

    def push(self, res: PresenceResult) -> PresenceResult:
        self._buf.append((bool(res.presence), bool(res.motion_active)))
        n = len(self._buf)
        need = n // 2 + 1
        presence = sum(1 for p, _ in self._buf if p) >= need
        motion = sum(1 for _, m in self._buf if m) >= need
        if presence == res.presence and motion == res.motion_active:
            return res
        return PresenceResult(
            presence=presence,
            motion_active=motion,
            breathing_present=bool(res.breathing_present and presence),
            motion_score=res.motion_score,
            sc_share=res.sc_share,
            n_sc=res.n_sc,
            n_frames=res.n_frames,
            window_s=res.window_s,
            breathing_bpm=res.breathing_bpm,
            phase_std=res.phase_std,
        )
