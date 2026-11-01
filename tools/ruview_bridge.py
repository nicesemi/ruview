#!/usr/bin/env python3
"""RuView LAN→cloud CSI bridge.

The PaaS container hosting ruview.tech has no inbound UDP, so the ESP32 on the
home LAN can never reach it directly. This bridge runs inside the LAN, listens
on UDP :5005 exactly like ws_server does, turns each ADR-018 CSI burst into the
same `sensing_update` JSON ws_server would emit, and pushes it out to
`wss://ruview.tech/ws/ingest`. The cloud gateway then reports `source: esp32`.

Usage:
    python ruview_bridge.py --target wss://ruview.tech --token ruview-ingest
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(HERE, "RuView", "archive")
if ARCHIVE not in sys.path:
    sys.path.insert(0, ARCHIVE)

from v1.src.sensing.ws_server import (  # noqa: E402
    ESP32_UDP_PORT,
    Esp32UdpCollector,
    SensingWebSocketServer,
)

import websockets  # noqa: E402

# Silence longer than this means the node is off/unplugged.
STALE_UDP_S = 3.0


def _compact(raw: str, decimals: int = 1) -> str:
    """Shrink a sensing_update so it survives the cloud proxy's frame ceiling.

    The 20x20 ``signal_field.values`` block alone is ~6 KB once Python's float
    repr is applied. Render's edge proxy silently stalls ingest frames above
    roughly 4-8 KB, so values are rounded and re-serialised without spaces.
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    field = data.get("signal_field")
    if isinstance(field, dict) and isinstance(field.get("values"), list):
        field["values"] = [
            round(v, decimals) if isinstance(v, float) else v for v in field["values"]
        ]
    return json.dumps(data, separators=(",", ":"))


class Bridge:
    """Local ESP32 UDP -> sensing_update JSON."""

    def __init__(self, port: int = ESP32_UDP_PORT, sample_rate_hz: float = 10.0,
                 compact: bool = True) -> None:
        self.compact = compact
        self.collector = Esp32UdpCollector(port=port, sample_rate_hz=sample_rate_hz)
        # Reuse the upstream implementation so the cloud sees byte-identical
        # frame semantics: same extractor window, same presence classifier.
        self.server = SensingWebSocketServer()
        self.server.collector = self.collector
        self.server.source = "esp32"
        self._last_seq = None
        self._udp_count = 0
        self._udp_ts = time.time()

    def start(self) -> None:
        self.collector.start()

    def stop(self) -> None:
        try:
            self.collector.stop()
        except Exception:  # noqa: BLE001 - shutdown must never raise
            pass

    def stats(self) -> dict:
        csi = self.collector.last_csi or {}
        return {
            "udp_frames": self.collector.frames_received,
            "node_id": csi.get("node_id"),
            "seq": csi.get("sequence"),
            "subcarriers": csi.get("n_subcarriers"),
            "rssi_dbm": csi.get("rssi_dbm"),
            "peer": csi.get("source_addr"),
        }

    def frame(self) -> str | None:
        """One sensing_update JSON, or None while the window is still filling."""
        # The collector keeps a 120 s ring buffer. Without this guard the
        # bridge keeps re-deriving frames from that buffer after the ESP32 is
        # unplugged, so the cloud keeps seeing `source: esp32` and the UI keeps
        # claiming "connected" forever.
        count = self.collector.frames_received
        now = time.time()
        if count != self._udp_count:
            self._udp_count = count
            self._udp_ts = now
        elif now - self._udp_ts > STALE_UDP_S:
            return None

        n_needed = int(self.server.extractor.window_seconds * self.collector.sample_rate_hz)
        samples = self.collector.get_samples(n=n_needed)
        if len(samples) < 4:
            return None
        features = self.server.extractor.extract(samples)
        result = self.server.classifier.classify(features)
        msg = self.server._build_message(features, result)
        return _compact(msg) if self.compact else msg


async def run(args: argparse.Namespace) -> None:
    bridge = Bridge(port=args.port, sample_rate_hz=args.sample_rate)
    bridge.start()
    url = f"{args.target.rstrip('/')}/ws/ingest?token={args.token}"
    print(f"[bridge] listening UDP :{args.port} -> {args.target}/ws/ingest", flush=True)
    print("[bridge] waiting for ESP32 CSI frames ...", flush=True)

    backoff = 2.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                          max_size=None, open_timeout=15) as ws:
                print(f"[bridge] connected to cloud: {args.target}", flush=True)
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
                        print(
                            f"[bridge] sent={sent} udp={s['udp_frames']} "
                            f"node={s['node_id']} seq={s['seq']} sc={s['subcarriers']} "
                            f"rssi={s['rssi_dbm']}",
                            flush=True,
                        )
                        last_report = time.time()
                    await asyncio.sleep(args.interval)
        except Exception as exc:  # noqa: BLE001 - cloud may sleep / redeploy
            print(f"[bridge] connection lost ({type(exc).__name__}: {exc}); "
                  f"retry in {backoff:.0f}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def main() -> None:
    p = argparse.ArgumentParser(description="RuView LAN-to-cloud CSI bridge")
    p.add_argument("--target", default="wss://ruview.tech", help="cloud base URL")
    p.add_argument("--token", default=os.environ.get("INGEST_TOKEN") or "ruview-ingest")
    p.add_argument("--port", type=int, default=ESP32_UDP_PORT, help="local ESP32 UDP port")
    p.add_argument("--sample-rate", type=float, default=10.0)
    p.add_argument("--interval", type=float, default=0.5, help="seconds between pushes")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[bridge] stopped", flush=True)


if __name__ == "__main__":
    main()
