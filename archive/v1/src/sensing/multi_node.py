"""Per-node CSI sensing for the RuView LAN-to-cloud bridge.

Why this module exists
----------------------
``Esp32UdpCollector`` keeps exactly ONE ring buffer and ONE ``sc_hist`` for the
whole UDP socket.  With two ESP32 boards both streaming into :5005, every frame
from board 1 and board 2 lands in the same series, so:

* the RSSI "series" becomes two independent gain steps interleaved -- its
  variance pins at ~30 (std ~5.5) in *every* scenario: empty room, one seated
  person, two people.  That is exactly what the calibration run measured
  (30.85 / 31.08 / 29.06) and why presence was pinned to True;
* ``feature_extractor`` derives the sample rate from the timestamps of that
  interleaved stream (~30 fps instead of ~15 fps per board).  The FFT axis is
  therefore wrong: ``dominant_freq_hz`` comes back as 33 Hz, far above the
  Nyquist limit of the real per-node rate, and ``motion_band_power``
  (0.5-3 Hz) is aliasing noise rather than motion;
* ``presence_v2.evaluate`` groups frames by subcarrier count only, so the
  phase-difference std is computed across *two different physical links*.  In
  an empty room that reads far above the 0.07 rad threshold and presence is
  pinned to True again.

So the two-node setup silently destroyed the sensing signal.  Fix: one
``NodeTrack`` per node id -- own samples, own CSI history, own presence
smoother, own motion series -- and only then aggregate up to a room verdict.

Room verdict policy
-------------------
* ``presence``: OR over nodes (any link seeing perturbation counts).
* ``motion_level``: ACTIVE if any node is active, PRESENT_STILL if any node is
  present, ABSENT otherwise.
* ``persons``: 0 with no presence.  With >=2 links we compare their motion
  series: one body perturbs both links coherently, two bodies moving
  independently do not.  ``None`` (unknown) is returned whenever the evidence
  is insufficient -- never a fabricated 1.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import socket
import struct
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from v1.src.sensing.feature_extractor import RssiFeatureExtractor, RssiFeatures
from v1.src.sensing.classifier import MotionLevel, PresenceClassifier, SensingResult
from v1.src.sensing.rssi_collector import WifiSample
from v1.src.sensing import presence_v2

logger = logging.getLogger(__name__)

MAGIC = 0xC5110001
HEADER_SIZE = 20
HEADER_FMT = "<IBBHIIBB2x"

ESP32_UDP_PORT = int(os.environ.get("RUVIEW_UDP_PORT") or 5006)
# A link that has not delivered a frame for this long is dropped from the room
# verdict instead of being reported with stale numbers.
NODE_STALE_S = float(os.environ.get("RUVIEW_NODE_STALE_S") or 5.0)
# presence_v2 costs two FFTs over a 60 s x n_sc matrix; 1 Hz is plenty.
V2_RECOMPUTE_S = float(os.environ.get("RUVIEW_V2_RECOMPUTE_S") or 1.0)
PRESENCE_HOLD_S = float(os.environ.get("RUVIEW_PRESENCE_HOLD_S") or 45.0)
# Cross-link motion correlation below this means "the links disagree", i.e. more
# than one body.  PROVISIONAL: fitted before per-node separation existed, so the
# value is re-checked against the separated streams (see persons_basis).
PERSONS_CORR_TH = float(os.environ.get("RUVIEW_PERSONS_CORR_TH") or 0.45)
PERSONS_WIN_S = float(os.environ.get("RUVIEW_PERSONS_WIN_S") or 60.0)
PERSONS_MIN_POINTS = int(os.environ.get("RUVIEW_PERSONS_MIN_POINTS") or 12)


class NodeTrack:
    """Everything measured for one ESP32 node, kept strictly separate."""

    def __init__(self, node_id: int, sample_rate_hz: float = 10.0) -> None:
        self.node_id = int(node_id)
        self.sample_rate_hz = float(sample_rate_hz)
        # 120 s of RSSI-equivalent samples; the extractor trims to its own
        # 10 s window, the extra depth only costs memory.
        self.samples: deque = deque(maxlen=int(max(self.sample_rate_hz, 30.0) * 120))
        self.sc_hist: deque = deque(maxlen=int(max(self.sample_rate_hz, 30.0) * 120))
        self.last_csi: Optional[Dict] = None
        self.last_seen: float = 0.0
        self.first_seen: float = 0.0
        self.frames: int = 0
        self.sequence: int = -1
        # measured arrival rate per node, EMA -- the number the FFT axis needs
        self.fps: float = 0.0
        self.smoother = presence_v2.PresenceSmoother(window=21)
        self.v2: Optional[presence_v2.PresenceResult] = None
        self.v2_ts: float = 0.0
        self.hold_until: float = 0.0
        # 1 Hz motion history, used only for the cross-link correlation
        self.motion_hist: deque = deque(maxlen=int(PERSONS_WIN_S) + 30)

    # -- ingest ------------------------------------------------------------
    def add_sample(self, sample: WifiSample) -> None:
        now = sample.timestamp
        if self.last_seen:
            dt = now - self.last_seen
            if 0.0 < dt < 1.0:
                inst = 1.0 / dt
                self.fps = inst if self.fps <= 0 else (0.85 * self.fps + 0.15 * inst)
        else:
            self.first_seen = now
        self.last_seen = now
        self.samples.append(sample)
        self.frames += 1

    def add_iq(self, ts: float, n_sc: int, iq_pairs) -> None:
        self.sc_hist.append((ts, int(n_sc), iq_pairs))

    # -- derived -----------------------------------------------------------
    def is_stale(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        return (now - self.last_seen) > NODE_STALE_S if self.last_seen else True

    def features(self, extractor: RssiFeatureExtractor) -> RssiFeatures:
        return extractor.extract(list(self.samples))

    def refresh_presence(self, now: Optional[float] = None,
                         force: bool = False) -> Optional[presence_v2.PresenceResult]:
        """Re-evaluate presence_v2 for THIS node on ITS OWN CSI history."""
        now = now if now is not None else time.time()
        if self.v2 is not None and not force and (now - self.v2_ts) < V2_RECOMPUTE_S:
            return self.v2
        if not self.sc_hist:
            self.v2 = None
            return None
        try:
            res = presence_v2.evaluate(list(self.sc_hist), now=now)
        except Exception:  # noqa: BLE001 - a bad frame group must not kill the tick
            logger.exception("presence_v2 failed for node %s", self.node_id)
            return self.v2
        # Same guard as ws_server: the 60 s window must actually be filled
        # before its verdict is allowed to speak.
        if res.window_s >= presence_v2.BREATH_MIN_S:
            res = self.smoother.push(res)
        self.v2 = res
        self.v2_ts = now
        if res.presence:
            self.hold_until = now + PRESENCE_HOLD_S
        self.motion_hist.append((now, float(res.motion_score)))
        return res

    def presence_now(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        if self.v2 is None:
            return False
        return bool(self.v2.presence or now < self.hold_until)

    def motion_series(self, now: float, win_s: float = PERSONS_WIN_S
                      ) -> List[Tuple[int, float]]:
        """Per-second motion series, one value per wall-clock second."""
        buckets: Dict[int, List[float]] = {}
        for ts, val in self.motion_hist:
            if now - ts > win_s:
                continue
            buckets.setdefault(int(round(ts)), []).append(val)
        return sorted((k, float(np.mean(v))) for k, v in buckets.items())


class MultiNodeSensing:
    """Owns the UDP socket and routes every burst to its own node track."""

    def __init__(
        self,
        port: int = ESP32_UDP_PORT,
        sample_rate_hz: float = 10.0,
        extractor: Optional[RssiFeatureExtractor] = None,
        classifier: Optional[PresenceClassifier] = None,
        on_raw: Optional[Callable[[bytes, tuple], None]] = None,
    ) -> None:
        self.port = int(port)
        self.sample_rate_hz = float(sample_rate_hz)
        self.extractor = extractor or RssiFeatureExtractor()
        self.classifier = classifier or PresenceClassifier()
        self.on_raw = on_raw
        self.tracks: Dict[int, NodeTrack] = {}
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self.packets = 0
        self.parse_errors = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1.0)
        sock.bind(("0.0.0.0", self.port))
        self._sock = sock
        self._running = True
        self._thread = threading.Thread(target=self._recv_loop, daemon=True,
                                        name="multi-node-udp")
        self._thread.start()
        logger.info("MultiNodeSensing listening on :%d", self.port)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._sock:
            self._sock.close()
            self._sock = None

    def _recv_loop(self) -> None:
        while self._running:
            try:
                raw, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except Exception:  # noqa: BLE001
                if self._running:
                    logger.exception("UDP receive failed")
                continue
            try:
                self.ingest(raw, addr)
            except Exception:  # noqa: BLE001
                self.parse_errors += 1

    # -- parsing -----------------------------------------------------------
    def ingest(self, raw: bytes, addr: Tuple[str, int] = ("", 0)) -> None:
        if self.on_raw is not None:
            try:
                self.on_raw(raw, addr)
            except Exception:  # noqa: BLE001 - mirroring is best effort
                pass
        if len(raw) < HEADER_SIZE:
            return
        magic, node_id, n_ant, n_sc, freq_mhz, seq, rssi_u8, noise_u8 = \
            struct.unpack_from(HEADER_FMT, raw, 0)
        if magic != MAGIC:
            return
        rssi = rssi_u8 if rssi_u8 < 128 else rssi_u8 - 256
        noise = noise_u8 if noise_u8 < 128 else noise_u8 - 256
        iq_count = int(n_ant) * int(n_sc)
        need = HEADER_SIZE + iq_count * 2
        amp = None
        iq_pairs = None
        mean_amp = 0.0
        if iq_count > 0 and len(raw) >= need:
            iq = np.frombuffer(raw, dtype=np.int8, count=iq_count * 2,
                               offset=HEADER_SIZE).astype(np.float32)
            i_vals, q_vals = iq[0::2], iq[1::2]
            amp = np.sqrt(i_vals * i_vals + q_vals * q_vals)
            mean_amp = float(amp.mean())
            try:
                iq_pairs = iq.reshape(int(n_ant), int(n_sc), 2)[0]
            except Exception:  # noqa: BLE001
                iq_pairs = None

        effective_rssi = float(rssi)
        if rssi == -80 and mean_amp > 0:
            # firmware leaves the header RSSI at its -80 placeholder
            effective_rssi = -70.0 + min(mean_amp, 20.0) * 2.0

        now = time.time()
        sample = WifiSample(
            timestamp=now,
            rssi_dbm=effective_rssi,
            noise_dbm=float(noise),
            link_quality=max(0.0, min(1.0, (effective_rssi + 100.0) / 60.0)),
            tx_bytes=int(seq) * 1500,
            rx_bytes=int(seq) * 3000,
            retry_count=0,
            interface="esp32-node%d" % node_id,
        )
        with self._lock:
            track = self.tracks.get(int(node_id))
            if track is None:
                track = NodeTrack(int(node_id), self.sample_rate_hz)
                self.tracks[int(node_id)] = track
                logger.info("node %s appeared (%s)", node_id, addr[0] if addr else "?")
            track.sequence = int(seq)
            track.add_sample(sample)
            if iq_pairs is not None:
                track.add_iq(now, int(n_sc), iq_pairs)
            track.last_csi = {
                "node_id": int(node_id),
                "n_antennas": int(n_ant),
                "n_subcarriers": int(n_sc),
                "freq_mhz": int(freq_mhz),
                "sequence": int(seq),
                "rssi_dbm": int(rssi),
                "noise_floor_dbm": int(noise),
                "mean_amplitude": mean_amp,
                "amplitude": amp[:56].tolist() if amp is not None else [],
                "source_addr": "%s:%s" % (addr[0], addr[1]),
            }
        self.packets += 1

    # -- aggregation -------------------------------------------------------
    def live_tracks(self, now: Optional[float] = None) -> List[NodeTrack]:
        now = now if now is not None else time.time()
        with self._lock:
            return [t for t in self.tracks.values() if not t.is_stale(now)]

    def snapshot(self) -> Dict:
        """One fully-derived, per-node-correct sensing frame (JSON ready)."""
        now = time.time()
        tracks = self.live_tracks(now)
        node_frames: List[Dict] = []
        voters: List[NodeTrack] = []

        for track in tracks:
            feats = track.features(self.extractor)
            v2 = track.refresh_presence(now)
            cls = self.classifier.classify(feats)
            presence = track.presence_now(now)
            # The stock classifier keys presence off wideband variance; override
            # with this node's calibrated CSI verdict, keeping its label type.
            if presence:
                level = MotionLevel.ACTIVE if (v2 and v2.motion_active) else MotionLevel.PRESENT_STILL
            else:
                level = MotionLevel.ABSENT
            cls = dataclasses.replace(cls, presence_detected=presence,
                                      motion_level=level)
            voters.append(track)
            node_frames.append(self._node_frame(track, feats, cls, v2, presence, now))

        room = self._room_verdict(voters, now)
        primary = self._primary(voters)
        if primary is not None:
            feats = primary.features(self.extractor)
            cls = self.classifier.classify(feats)
            cls = dataclasses.replace(cls, presence_detected=room["presence"],
                                      motion_level=room["_level"],
                                      confidence=room["confidence"])
            csi = primary.last_csi
        else:
            feats = RssiFeatures()
            cls = self.classifier.classify(feats)
            cls = dataclasses.replace(cls, presence_detected=False,
                                      motion_level=MotionLevel.ABSENT,
                                      confidence=0.0)
            csi = None

        return {
            "type": "sensing_update",
            "timestamp": now,
            "source": "esp32",
            "nodes": [self._node_view(n) for n in node_frames],
            "node_features": node_frames,
            "features": self._features_view(feats),
            "classification": {
                "motion_level": room["motion_level"],
                "presence": room["presence"],
                "confidence": room["confidence"],
                "details": room["details"],
                "presence_evidence": node_frames[0].get("presence_evidence") if node_frames else None,
            },
            "room": {k: v for k, v in room.items() if not k.startswith("_")},
            "node_count": len(node_frames),
            "node_ids": [str(n["node_id"]) for n in node_frames],
            "aggregated": len(node_frames) > 1,
        }

    def _node_frame(self, track: NodeTrack, feats: RssiFeatures, cls: SensingResult,
                    v2: Optional[presence_v2.PresenceResult], presence: bool,
                    now: float) -> Dict:
        csi = track.last_csi or {}
        return {
            "node_id": track.node_id,
            "rssi_dbm": csi.get("rssi_dbm", round(feats.mean, 1)),
            "subcarrier_count": csi.get("n_subcarriers", 0),
            "mean_amplitude": round(float(csi.get("mean_amplitude") or 0.0), 3),
            "freq_mhz": csi.get("freq_mhz", 0),
            "sequence": track.sequence,
            "fps": round(track.fps, 1),
            "frames": track.frames,
            "age_s": round(now - track.last_seen, 2),
            "presence": presence,
            "motion_level": cls.motion_level.value,
            "metrics": self._features_view(feats),
            "presence_evidence": (v2.as_dict() if v2 is not None else None),
        }

    @staticmethod
    def _node_view(node: Dict) -> Dict:
        return {
            "node_id": node.get("node_id"),
            "rssi_dbm": node.get("rssi_dbm"),
            "subcarrier_count": node.get("subcarrier_count", 0),
            "mean_amplitude": node.get("mean_amplitude", 0),
            "freq_mhz": node.get("freq_mhz", 0),
            "sequence": node.get("sequence", 0),
            "fps": node.get("fps", 0),
        }

    @staticmethod
    def _features_view(f: RssiFeatures) -> Dict:
        return {
            "mean_rssi": f.mean,
            "variance": f.variance,
            "std": f.std,
            "motion_band_power": f.motion_band_power,
            "breathing_band_power": f.breathing_band_power,
            "dominant_freq_hz": f.dominant_freq_hz,
            "change_points": f.n_change_points,
            "spectral_power": f.total_spectral_power,
            "range": f.range,
            "iqr": f.iqr,
            "skewness": f.skewness,
            "kurtosis": f.kurtosis,
            "n_samples": f.n_samples,
            "sample_rate_hz": round(float(f.sample_rate_hz), 2),
        }

    @staticmethod
    def _primary(tracks: Sequence[NodeTrack]) -> Optional[NodeTrack]:
        """The link with the strongest presence evidence drives the visuals."""
        if not tracks:
            return None
        present = [t for t in tracks if t.presence_now()]
        pool = present or list(tracks)
        return max(pool, key=lambda t: (t.v2.phase_std if t.v2 else 0.0, t.frames))

    def _room_verdict(self, tracks: Sequence[NodeTrack], now: float) -> Dict:
        if not tracks:
            return {"presence": False, "motion_level": "absent", "persons": 0,
                    "confidence": 0.0, "details": "无活跃节点", "nodes": 0,
                    "_level": MotionLevel.ABSENT}

        present = [t for t in tracks if t.presence_now(now)]
        active = [t for t in tracks if t.v2 is not None and t.v2.motion_active
                  and t.presence_now(now)]
        presence = bool(present)
        if active:
            level, label = MotionLevel.ACTIVE, "active"
        elif presence:
            level, label = MotionLevel.PRESENT_STILL, "present_still"
        else:
            level, label = MotionLevel.ABSENT, "absent"

        persons, basis, corr = self._estimate_persons(tracks, present, now)

        # Confidence is the agreement between links, not a constant.
        votes = [t.v2.presence for t in tracks if t.v2 is not None]
        agree = (max(votes.count(True), votes.count(False)) / len(votes)) if votes else 0.0
        strength = 0.0
        if present:
            pstd = max((t.v2.phase_std if t.v2 else 0.0) for t in present)
            strength = min(1.0, pstd / max(presence_v2.PHASE_STD_TH, 1e-6))
        confidence = round(min(1.0, 0.5 * agree + 0.5 * strength) if presence
                           else (0.35 + 0.5 * agree), 2)

        details = "%d 个节点在线 / %d 个检出存在" % (len(tracks), len(present))
        if corr is not None:
            details += "；跨链路运动相关 %.2f" % corr
        return {
            "presence": presence,
            "motion_level": label,
            "persons": persons,
            "confidence": confidence,
            "details": details,
            "nodes": len(tracks),
            "present_nodes": [t.node_id for t in present],
            "persons_basis": basis,
            "cross_link_corr": None if corr is None else round(corr, 3),
            "_level": level,
        }

    def _estimate_persons(self, tracks: Sequence[NodeTrack], present: Sequence[NodeTrack],
                          now: float) -> Tuple[Optional[int], str, Optional[float]]:
        """Cross-link motion correlation -> head count, or None when unknowable."""
        if not tracks:
            return 0, "no_nodes", None
        if not present:
            return 0, "no_presence", None
        if len(tracks) < 2:
            # A single link cannot separate one body from two; report unknown
            # rather than the old hardcoded 1.
            return None, "single_node", None

        series = []
        for track in tracks:
            s = track.motion_series(now)
            if len(s) >= PERSONS_MIN_POINTS:
                series.append(dict(s))
        if len(series) < 2:
            return None, "insufficient_history", None

        common = set(series[0])
        for s in series[1:]:
            common &= set(s)
        common = sorted(common)
        if len(common) < PERSONS_MIN_POINTS:
            return None, "insufficient_overlap", None
        a = np.array([series[0][k] for k in common], dtype=np.float64)
        b = np.array([series[1][k] for k in common], dtype=np.float64)
        if a.std() < 1e-6 or b.std() < 1e-6:
            return None, "no_motion_variance", None
        corr = float(np.corrcoef(a, b)[0, 1])
        # Coherent perturbation on both links => one body. Independent motion on
        # each link => more than one body. Capped by the link count.
        persons = 1 if corr >= PERSONS_CORR_TH else min(2, len(tracks))
        return persons, "cross_link_motion_corr", corr

    # -- payload helpers ---------------------------------------------------
    def build_message(self) -> str:
        """Full sensing_update JSON, enriched with the signal field."""
        payload = self.snapshot()
        try:
            from v1.src.sensing.ws_server import generate_signal_field
            primary = self._primary(self.live_tracks())
            csi = primary.last_csi if primary else None
            feats = primary.features(self.extractor) if primary else RssiFeatures()
            cls = self.classifier.classify(feats)
            room = payload.get("room") or {}
            level = MotionLevel(room.get("motion_level") or "absent")
            cls = dataclasses.replace(cls, presence_detected=bool(room.get("presence")),
                                      motion_level=level,
                                      confidence=float(room.get("confidence") or 0.0))
            payload["signal_field"] = generate_signal_field(feats, cls, csi_data=csi)
        except Exception:  # noqa: BLE001 - visuals are optional
            logger.debug("signal field generation skipped", exc_info=True)
        return json.dumps(payload, separators=(",", ":"), default=float)

    def stats(self) -> Dict:
        with self._lock:
            tracks = list(self.tracks.values())
        return {
            "packets": self.packets,
            "nodes": [
                {
                    "node_id": t.node_id,
                    "frames": t.frames,
                    "fps": round(t.fps, 1),
                    "seq": t.sequence,
                    "subcarriers": (t.last_csi or {}).get("n_subcarriers"),
                    "rssi_dbm": (t.last_csi or {}).get("rssi_dbm"),
                    "stale": t.is_stale(),
                }
                for t in tracks
            ],
        }
