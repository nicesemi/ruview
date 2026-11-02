#!/usr/bin/env python3
"""RuView LAN->cloud CSI bridge.

The PaaS container hosting ruview.tech has no inbound UDP, so the ESP32 on the
home LAN can never reach it directly. This bridge runs inside the LAN, listens
on UDP :5005 exactly like ws_server does, and pushes a `sensing_update` frame to
`wss://ruview.tech/ws/ingest`. The cloud gateway then reports `source: esp32`.

Two-node correctness
--------------------
The bridge used to feed every board into the upstream `Esp32UdpCollector`, whose
single ring buffer / single `sc_hist` interleaves both links. That made variance
a constant ~30 regardless of occupancy, put the FFT axis at ~30 fps so
`dominant_freq_hz` read 33 Hz (above the real Nyquist limit), and made
presence_v2 measure phase spread across two different physical links -- so the
empty room read "occupied". `MultiNodeSensing` keeps one track per node and
aggregates afterwards; see `v1/src/sensing/multi_node.py`.

Usage:
    python ruview_bridge.py --target wss://ruview.tech --token ruview-ingest
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# Accept the three layouts this script is launched from: a standalone copy that
# ships its own RuView checkout, a repo root, or <repo>/tools.
_CANDIDATES = [
    os.path.join(HERE, "RuView", "archive"),
    os.path.join(HERE, "archive"),
    os.path.join(os.path.dirname(HERE), "archive"),
]
ARCHIVE = next((p for p in _CANDIDATES if os.path.isdir(p)), _CANDIDATES[0])
if ARCHIVE not in sys.path:
    sys.path.insert(0, ARCHIVE)

from v1.src.sensing.multi_node import (  # noqa: E402
    ESP32_UDP_PORT,
    MultiNodeSensing,
)

import websockets  # noqa: E402

# Silence longer than this means no node is delivering frames.
STALE_UDP_S = 3.0


# Render's edge proxy silently blackholes ingest frames that are too large:
# against ruview.tech a 4324-byte payload never arrives while 3458 bytes does,
# and the sender gets neither an error nor a close frame. 3400 keeps a margin
# under the last size observed to arrive.
FRAME_BUDGET_BYTES = 3400

# Set RUVIEW_BRIDGE_DEBUG=1 to log the wire size of every compacted frame.
DEBUG = os.environ.get("RUVIEW_BRIDGE_DEBUG") == "1"


def _debug(message: str) -> None:
    print("[bridge] %s" % message, flush=True)


def _round_vals(vals, decimals: int):
    return [round(v, decimals) if isinstance(v, float) else v for v in vals]


def _grid_dims(field: dict, count: int):
    """(width, height) of a flat row-major grid payload, or (0, 0)."""
    grid = field.get("grid_size")
    if isinstance(grid, (list, tuple)) and len(grid) >= 3:
        try:
            width, height = int(grid[0]), int(grid[2])
        except (TypeError, ValueError):
            width = height = 0
        if width > 0 and height > 0 and width * height == count:
            return width, height
    side = int(round(math.sqrt(count)))
    return (side, side) if side * side == count else (0, 0)


def _downsample_grid(values, width: int, max_side: int):
    """Box-average a square grid down to max_side cells per axis."""
    if width <= max_side or width * width != len(values):
        return values
    target = max_side
    out = []
    for ty in range(target):
        y0 = ty * width // target
        y1 = max(y0 + 1, (ty + 1) * width // target)
        for tx in range(target):
            x0 = tx * width // target
            x1 = max(x0 + 1, (tx + 1) * width // target)
            total = 0.0
            seen = 0
            for y in range(y0, y1):
                row = y * width
                for x in range(x0, x1):
                    value = values[row + x]
                    if isinstance(value, (int, float)):
                        total += float(value)
                        seen += 1
            out.append(total / seen if seen else 0.0)
    return out


def _round_floats(obj, ndigits: int):
    """Recursively round every float in the payload.

    Python's float repr is the single biggest cost in a sensing_update: values
    like 1278.3889834581 spend 15 bytes to say what 1278.389 says in 8. The UI
    paints at pixel resolution, so 3 decimals is lossless in practice.
    """
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {k: _round_floats(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, ndigits) for v in obj]
    return obj


def _shrink_field(field, decimals: int, max_side: int) -> None:
    if not isinstance(field, dict):
        return
    values = field.get("values")
    if not isinstance(values, list) or not values:
        return
    width, height = _grid_dims(field, len(values))
    if width and height and max_side and width > max_side:
        values = _downsample_grid(values, width, max_side)
        side = int(round(math.sqrt(len(values))))
        if side * side == len(values):
            field["grid_size"] = [side, 1, side]
    field["values"] = _round_vals(values, decimals)


def _shrink_nodes(data: dict, decimals: int, max_amp: int) -> None:
    nodes = data.get("node_features")
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        # The UI rebuilds its own history from successive frames, so shipping a
        # server-side history is pure payload bloat.
        node.pop("amplitude_history", None)
        amp = node.get("amplitude")
        if isinstance(amp, list) and amp:
            if max_amp <= 0:
                node.pop("amplitude", None)
            else:
                if len(amp) > max_amp:
                    amp = amp[::math.ceil(len(amp) / max_amp)]
                node["amplitude"] = _round_vals(amp, decimals)
        for key in ("mean_amplitude", "variance", "motion_band_power",
                    "breathing_band_power", "rssi_dbm", "confidence"):
            value = node.get(key)
            if isinstance(value, float):
                node[key] = round(value, 3)


def _compact(raw: str, decimals: int = 1,
             budget: int = FRAME_BUDGET_BYTES) -> str:
    """Shrink a sensing_update so it survives the cloud proxy's frame ceiling.

    The 20x20 ``signal_field.values`` block plus two per-node amplitude arrays
    used to serialise to ~4.3 KB, which the edge proxy drops on the floor. Each
    pass below is more aggressive than the last; the first one that fits the
    budget wins so fidelity degrades only as far as it has to.
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return raw

    passes = [
        # (field decimals, field max side, per-node amplitude points)
        # Keep one decimal on the field: the heatmap range is 0..1.2, so
        # rounding to integers would flatten it into a 0/1 bitmap.
        (1, 20, 28),
        (1, 16, 20),
        (1, 12, 16),
        (1, 10, 12),
        (1, 8, 0),    # last resort: drop per-node amplitude entirely
    ]
    smallest = raw
    for index, (field_decimals, max_side, max_amp) in enumerate(passes):
        trial = _round_floats(copy.deepcopy(data), 3)
        _shrink_field(trial.get("signal_field"), field_decimals, max_side)
        _shrink_nodes(trial, field_decimals, max_amp)
        encoded = json.dumps(trial, separators=(",", ":"), default=float)
        if len(encoded) < len(smallest):
            smallest = encoded
        if len(encoded) <= budget:
            if DEBUG:
                field = trial.get("signal_field") or {}
                _debug("frame %d->%d bytes (pass %d, grid %s)"
                       % (len(raw), len(encoded), index,
                          (field.get("grid_size") or ["?"])[0]))
            return encoded
    if DEBUG:
        _debug("frame %d->%d bytes (BUDGET MISS)" % (len(raw), len(smallest)))
    return smallest


class TcpMirror:
    """Fan raw bursts out over TCP for pullers that cannot receive UDP."""

    def __init__(self, port: int) -> None:
        self.port = int(port)
        self._clients: list = []
        self._lock = threading.Lock()

    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(4)

        def accept_loop() -> None:
            while True:
                try:
                    conn, addr = srv.accept()
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    with self._lock:
                        self._clients.append(conn)
                    print("[bridge] tcp mirror client %s:%s" % addr, flush=True)
                except Exception:  # noqa: BLE001
                    time.sleep(0.5)

        threading.Thread(target=accept_loop, daemon=True, name="tcp-mirror").start()
        print("[bridge] tcp mirror listening on :%d" % self.port, flush=True)

    def send(self, raw: bytes) -> None:
        if not self._clients:
            return
        packet = len(raw).to_bytes(4, "big") + raw
        dead = []
        with self._lock:
            for conn in self._clients:
                try:
                    conn.sendall(packet)
                except Exception:  # noqa: BLE001
                    dead.append(conn)
            for conn in dead:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                self._clients.remove(conn)


class Bridge:
    """Local multi-node CSI -> one aggregated sensing_update JSON."""

    def __init__(self, port: int = ESP32_UDP_PORT, sample_rate_hz: float = 10.0,
                 compact: bool = True, mirror: str = "", tcp_mirror: int = 0) -> None:
        self.compact = compact
        self.mirror = mirror
        self.tcp = TcpMirror(tcp_mirror) if tcp_mirror else None
        self._mirror_targets = self._parse_targets(mirror)
        self._mirror_sock = None
        if self._mirror_targets:
            self._mirror_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sensing = MultiNodeSensing(port=port, sample_rate_hz=sample_rate_hz,
                                        on_raw=self._on_raw)
        self._last_packets = 0
        self._last_packet_ts = time.time()
        self._dumped = False

    @staticmethod
    def _parse_targets(spec: str):
        targets = []
        for item in (spec or "").split(","):
            item = item.strip()
            if not item:
                continue
            host, _, port = item.rpartition(":")
            if not host:
                host, port = item, str(ESP32_UDP_PORT)
            try:
                targets.append((host, int(port)))
            except ValueError:
                print("[bridge] ignoring bad mirror target: %s" % item, flush=True)
        return targets

    def _on_raw(self, raw: bytes, addr) -> None:
        if self.tcp is not None:
            self.tcp.send(raw)
        if self._mirror_sock is not None:
            for host, port in self._mirror_targets:
                try:
                    self._mirror_sock.sendto(raw, (host, port))
                except OSError:
                    pass

    def start(self) -> None:
        if self.tcp is not None:
            self.tcp.start()
        self.sensing.start()
        if self._mirror_targets:
            print("[bridge] mirroring raw CSI to " +
                  ", ".join("%s:%d" % t for t in self._mirror_targets), flush=True)

    def stop(self) -> None:
        try:
            self.sensing.stop()
        except Exception:  # noqa: BLE001 - shutdown must never raise
            pass

    def stats(self) -> dict:
        return self.sensing.stats()

    def frame(self) -> str | None:
        """One aggregated sensing_update JSON, or None while no node is live."""
        packets = self.sensing.packets
        now = time.time()
        if packets != self._last_packets:
            self._last_packets = packets
            self._last_packet_ts = now
        elif now - self._last_packet_ts > STALE_UDP_S:
            # Never keep re-deriving frames from the buffer after the node is
            # unplugged, otherwise the cloud keeps claiming a live link.
            return None

        tracks = self.sensing.live_tracks()
        if not tracks:
            return None
        msg = self.sensing.build_message()
        dump = os.environ.get("RUVIEW_BRIDGE_DUMP")
        if dump and self._dumped < int(os.environ.get("RUVIEW_BRIDGE_DUMP_MAX") or 1):
            try:
                path = dump if self._dumped == 0 else "%s.%d" % (dump, self._dumped)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(msg)
                self._dumped += 1
                _debug("dumped frame %d to %s (%d bytes)" % (self._dumped, path, len(msg)))
            except OSError:
                pass
        return _compact(msg) if self.compact else msg


async def run(args: argparse.Namespace) -> None:
    bridge = Bridge(port=args.port, sample_rate_hz=args.sample_rate,
                    mirror=args.mirror, tcp_mirror=args.tcp_mirror)
    bridge.start()
    url = "%s/ws/ingest?token=%s" % (args.target.rstrip("/"), args.token)
    print("[bridge] listening UDP :%d -> %s/ws/ingest" % (args.port, args.target), flush=True)
    print("[bridge] waiting for ESP32 CSI frames ...", flush=True)

    backoff = 2.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                          max_size=None, open_timeout=15) as ws:
                print("[bridge] connected to cloud: %s" % args.target, flush=True)
                backoff = 2.0
                sent = 0
                idle_since = time.time()
                last_report = time.time()
                while True:
                    msg = bridge.frame()
                    if msg is not None:
                        await ws.send(msg)
                        sent += 1
                        idle_since = time.time()
                    elif time.time() - idle_since > 15 and time.time() - last_report > 15:
                        print("[bridge] no CSI samples yet (is the node powered on?)", flush=True)
                        last_report = time.time()
                    if args.verbose and time.time() - last_report >= 10:
                        s = bridge.stats()
                        desc = " ".join(
                            "node%s(seq=%s sc=%s rssi=%s fps=%s frames=%s%s)" % (
                                n["node_id"], n["seq"], n["subcarriers"],
                                n["rssi_dbm"], n["fps"], n["frames"],
                                " STALE" if n["stale"] else "")
                            for n in s["nodes"]) or "no nodes"
                        print("[bridge] sent=%d packets=%d %s" % (sent, s["packets"], desc),
                              flush=True)
                        last_report = time.time()
                    await asyncio.sleep(args.interval)
        except Exception as exc:  # noqa: BLE001 - cloud may sleep / redeploy
            print("[bridge] connection lost (%s: %s); retry in %.0fs"
                  % (type(exc).__name__, exc, backoff), flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def main() -> None:
    p = argparse.ArgumentParser(description="RuView LAN-to-cloud CSI bridge")
    p.add_argument("--target", default="wss://ruview.tech", help="cloud base URL")
    p.add_argument("--token", default=os.environ.get("INGEST_TOKEN") or "ruview-ingest")
    p.add_argument("--port", type=int, default=ESP32_UDP_PORT, help="local ESP32 UDP port")
    p.add_argument("--sample-rate", type=float, default=10.0)
    p.add_argument("--interval", type=float, default=0.5,
                   help="seconds between pushes")
    p.add_argument("--mirror", default="",
                   help="comma-separated host:port list that also gets every raw UDP frame")
    p.add_argument("--tcp-mirror", type=int, default=0,
                   help="TCP port that streams length-prefixed raw frames to pullers")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[bridge] stopped", flush=True)


if __name__ == "__main__":
    main()
