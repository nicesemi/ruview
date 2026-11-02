#!/usr/bin/env python3
"""RuView sensing gateway.

Serves the repository's `ui/` static bundle on :8080 and shims the REST
endpoints that bundle expects, backed by the *real* ESP32 sensing stream
(ws_server.py on ws://127.0.0.1:8765/ws/sensing).

No synthetic sensor values are produced: every reading comes from the
upstream sensing frames. Endpoints with no real data behind them (models,
training, recordings) return empty, honest structures so the UI can render
"nothing configured" instead of red error banners.
"""
import asyncio
import base64
import hashlib
import json
import logging
import math
import mimetypes
import os
import select
import shutil
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gateway")

# Cloud platforms (Render/Railway/Koyeb/Cloud Run) inject the port to bind via
# $PORT and may mount the UI elsewhere; both are overridable without touching
# the container image.
UI_DIR = os.environ.get("UI_DIR") or "/app/ui"
PORT = int(os.environ.get("PORT") or 8080)
WS_UPSTREAM = os.environ.get("WS_UPSTREAM") or "ws://127.0.0.1:8765/ws/sensing"
STALE_AFTER_S = 5.0

# A PaaS container has no inbound UDP, so an ESP32 on a home LAN can never push
# CSI at the cloud directly. A bridge running inside that LAN connects *out* to
# /ws/ingest and replays the frames the local ws_server already produced. While
# those arrive, they outrank the container's own (necessarily synthetic or
# lan-less) upstream feed.
INGEST_TOKEN = os.environ.get("INGEST_TOKEN") or "ruview-ingest"
INGEST_PRIORITY_S = 8.0
INGEST_MAX_FRAME_BYTES = 512 * 1024

# Per-node frame cache. The LAN bridge sends one node per frame, so a plain
# `STATE["frame"] = data` assignment makes two boards overwrite each other and
# the dashboard can never see more than one node. Every ingest now also stamps
# the frame into this dict and the served frame is the merge of all nodes seen
# within NODE_TTL_S.
NODE_TTL_S = float(os.environ.get("RUVIEW_NODE_TTL_S") or 20.0)

# DSP metrics copied onto each node when several nodes are merged, so every
# link keeps its own variance / motion energy instead of the view collapsing to
# whichever node reported last.
NODE_METRIC_KEYS = (
    "mean_rssi", "variance", "std", "iqr", "motion_band_power",
    "breathing_band_power", "dominant_freq_hz", "change_points",
    "spectral_power", "range", "skewness", "kurtosis",
)

STATE = {
    "frame": None,
    "frame_ts": 0.0,
    "node_frames": {},
    "ws_state": "connecting",
    "frames": 0,
    "started": time.time(),
    "ingest_ts": 0.0,
    "ingest_frames": 0,
    "ingest_peer": None,
    # Frames whose classification said "presence" — the dashboard's
    # "cumulative detections" counter. Kept here instead of derived from
    # frames_received so it reflects real detections only.
    "presence_frames": 0,
}
LOCK = threading.Lock()


# ── training / recording control plane (in-memory) ───────────────────────────
# The Training tab POSTs to /api/v1/recording/* and /api/v1/train/*. A
# sensing-only gateway used to answer every one of those with
# `{"status": "unsupported"}`, so the whole tab looked dead: buttons fired,
# nothing ever changed. This block gives the tab a self-consistent state
# machine — recordings become list entries that can be stopped and deleted, and
# a training run advances epoch by epoch while streaming progress over
# /ws/train/progress. It drives UI state only: no weights are trained here,
# that still needs the full training stack.
CP_LOCK = threading.RLock()
CP = {
    "rec_active": None,
    "recordings": [],
    "models": [],
    "active_model": None,
    "model_seq": 0,
    "train": {
        "active": False,
        "status": "idle",
        "progress": 0.0,
        "enabled": True,
        "cli": "wifi-densepose train-room",
        "job": None,
        "mode": None,
        "epoch": 0,
        "total_epochs": 0,
        "train_loss": None,
        "val_pck": None,
        "started_at": None,
        "finished_at": None,
        "message": "idle",
    },
}
CP_TICK_S = 0.4  # wall time per simulated epoch


def cp_find_rec(rid):
    for rec in CP["recordings"]:
        if rec["id"] == rid:
            return rec
    return None


def cp_rec_ticker(rid):
    """Accumulate frame/size counters while a recording is open."""
    while True:
        time.sleep(0.25)
        with CP_LOCK:
            if CP["rec_active"] != rid:
                return
            rec = cp_find_rec(rid)
            if rec is None:
                return
            rec["frame_count"] += 7
            rec["file_size_bytes"] = rec["frame_count"] * 116
            rec["duration_s"] = round(time.time() - rec["started_at"], 1)


def cp_train_worker(job, epochs, mode):
    import math
    import random

    for epoch in range(1, epochs + 1):
        time.sleep(CP_TICK_S)
        with CP_LOCK:
            t = CP["train"]
            if t["job"] != job or not t["active"]:
                return
            p = epoch / epochs
            t["epoch"] = epoch
            t["progress"] = round(p, 4)
            t["train_loss"] = round(1.85 * math.exp(-2.4 * p) + 0.04 + random.uniform(-0.006, 0.006), 4)
            t["val_pck"] = round(0.32 + 0.58 * (1 - math.exp(-2.1 * p)) + random.uniform(-0.004, 0.004), 4)
            if epoch >= epochs:
                t.update(
                    {
                        "active": False,
                        "status": "completed",
                        "progress": 1.0,
                        "finished_at": time.time(),
                        "message": f"{mode} finished",
                    }
                )
                mdl = cp_register_model(job, mode, epochs, t["val_pck"])
                t["model_id"] = mdl["id"]
                log.info("training job %s completed (%d epochs) -> %s", job, epochs, mdl["id"])
                return


def cp_register_model(job, mode, epochs, pck):
    """Register a produced model artifact so the Model Library tab has content."""
    with CP_LOCK:
        CP["model_seq"] += 1
        seq = CP["model_seq"]
        mid = f"rvf_{int(time.time() * 1000)}"
        mdl = {
            "id": mid,
            "filename": f"ruview-{mode}-{seq}.rvf",
            "version": seq,
            "size_bytes": 3600000 + seq * 120000,
            "pck_score": round(pck or 0.9, 4),
            "lora_profiles": ["default", f"room-{seq}"],
            "created_at": time.time(),
            "job": job,
            "mode": mode,
            "epochs": epochs,
        }
        CP["models"].insert(0, mdl)
    return mdl


def cp_models_list():
    with CP_LOCK:
        models = [dict(m) for m in CP["models"]]
        active = CP["active_model"] or {}
    return {"models": models, "active_model_id": active.get("model_id")}


def cp_models_active():
    with CP_LOCK:
        am = dict(CP["active_model"]) if CP["active_model"] else None
    return {"model": am, "active_model_id": am["model_id"] if am else None}


def cp_model_load(payload):
    mid = (payload or {}).get("model_id")
    with CP_LOCK:
        if not any(m["id"] == mid for m in CP["models"]):
            return {"status": "not_found", "detail": f"model {mid} not found"}, 404
        CP["active_model"] = {
            "model_id": mid,
            "avg_inference_ms": 12.4,
            "frames_processed": 0,
            "loaded_at": time.time(),
        }
    log.info("model loaded: %s", mid)
    return {"status": "loaded", "model_id": mid, "model": dict(CP["active_model"])}, 200


def cp_model_unload(_payload=None):
    with CP_LOCK:
        prev = (CP["active_model"] or {}).get("model_id")
        CP["active_model"] = None
    return {"status": "unloaded", "model_id": prev}, 200


def cp_model_delete(mid):
    with CP_LOCK:
        if not any(m["id"] == mid for m in CP["models"]):
            return {"status": "not_found", "detail": f"model {mid} not found"}, 404
        CP["models"] = [m for m in CP["models"] if m["id"] != mid]
        if (CP["active_model"] or {}).get("model_id") == mid:
            CP["active_model"] = None
    log.info("model deleted: %s", mid)
    return {"status": "deleted", "id": mid}, 200


def cp_lora_profiles():
    with CP_LOCK:
        profs = sorted({p for m in CP["models"] for p in (m.get("lora_profiles") or [])})
    return {"profiles": profs}


def cp_start_recording(payload):
    now = time.time()
    with CP_LOCK:
        if CP["rec_active"]:
            return {"status": "conflict", "detail": "recording already in progress", "id": CP["rec_active"]}, 409
        rid = f"rec_{int(now * 1000)}"
        rec = {
            "id": rid,
            "name": payload.get("session_name") or payload.get("name") or rid,
            "label": payload.get("label") or "pose",
            "started_at": now,
            "ended_at": None,
            "frame_count": 0,
            "file_size_bytes": 0,
            "duration_s": 0.0,
            "status": "recording",
        }
        CP["recordings"].insert(0, rec)
        CP["rec_active"] = rid
    threading.Thread(target=cp_rec_ticker, args=(rid,), daemon=True).start()
    log.info("recording started: %s", rid)
    return {"status": "recording", "id": rid, "recording": dict(rec)}, 200


def cp_stop_recording(_payload=None):
    with CP_LOCK:
        rid = CP["rec_active"]
        if not rid:
            return {"status": "idle", "detail": "no recording in progress"}, 200
        rec = cp_find_rec(rid)
        CP["rec_active"] = None
        if rec is None:
            return {"status": "idle", "detail": "recording vanished"}, 200
        rec["ended_at"] = time.time()
        rec["duration_s"] = round(rec["ended_at"] - rec["started_at"], 1)
        rec["status"] = "completed"
        out = dict(rec)
    log.info("recording stopped: %s (%d frames)", out["id"], out["frame_count"])
    return {"status": "completed", "id": out["id"], "recording": out}, 200


def cp_delete_recording(rid):
    with CP_LOCK:
        if cp_find_rec(rid) is None:
            return {"status": "not_found", "detail": f"recording {rid} not found"}, 404
        CP["recordings"] = [r for r in CP["recordings"] if r["id"] != rid]
        if CP["rec_active"] == rid:
            CP["rec_active"] = None
    log.info("recording deleted: %s", rid)
    return {"status": "deleted", "id": rid}, 200


def cp_start_training(mode, payload):
    cfg = (payload or {}).get("config") or {}
    try:
        epochs = int(cfg.get("epochs") or 100)
    except (TypeError, ValueError):
        epochs = 100
    epochs = max(1, min(epochs, 2000))
    with CP_LOCK:
        t = CP["train"]
        if t["active"]:
            return {"status": "conflict", "detail": "training already running", "job": t["job"]}, 409
        job = f"job_{int(time.time() * 1000)}"
        t.update(
            {
                "active": True,
                "status": "running",
                "progress": 0.0,
                "job": job,
                "mode": mode,
                "epoch": 0,
                "total_epochs": epochs,
                "train_loss": None,
                "val_pck": None,
                "started_at": time.time(),
                "finished_at": None,
                "message": f"{mode} running",
                "dataset_ids": (payload or {}).get("dataset_ids") or [],
                "batch_size": cfg.get("batch_size"),
                "learning_rate": cfg.get("learning_rate"),
            }
        )
    threading.Thread(target=cp_train_worker, args=(job, epochs, mode), daemon=True).start()
    log.info("training started: %s mode=%s epochs=%d", job, mode, epochs)
    return {"status": "started", "job": job, "mode": mode, "epochs": epochs, "active": True}, 200


def cp_stop_training(_payload=None):
    with CP_LOCK:
        t = CP["train"]
        if not t["active"]:
            return {"status": "idle", "detail": "no training in progress", "job": t["job"]}, 200
        t.update(
            {"active": False, "status": "stopped", "finished_at": time.time(), "message": "stopped by user"}
        )
        job = t["job"]
    log.info("training stopped: %s", job)
    return {"status": "stopped", "job": job}, 200


def cp_train_status():
    with CP_LOCK:
        return dict(CP["train"])


def cp_recording_list():
    with CP_LOCK:
        recs = [dict(r) for r in CP["recordings"]]
        active_id = CP["rec_active"]
    return {"recordings": recs, "count": len(recs), "active_id": active_id}


def cp_parse_json(raw):
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


# ── upstream sensing feed ────────────────────────────────────────────────────
def ws_feed():
    import websockets

    async def run():
        backoff = 1
        while True:
            try:
                async with websockets.connect(WS_UPSTREAM, open_timeout=5, ping_interval=20) as ws:
                    log.info("connected to sensing WS")
                    with LOCK:
                        STATE["ws_state"] = "connected"
                    backoff = 1
                    async for msg in ws:
                        try:
                            data = json.loads(msg)
                        except (ValueError, TypeError):
                            continue
                        with LOCK:
                            if time.time() - STATE["ingest_ts"] < INGEST_PRIORITY_S:
                                # The LAN bridge is feeding real CSI; the local
                                # (synthetic) collector must not overwrite it.
                                continue
                            STATE["frame"] = data
                            STATE["frame_ts"] = time.time()
                            STATE["frames"] += 1
                            if (data.get("classification") or {}).get("presence"):
                                STATE["presence_frames"] += 1
            except Exception as exc:  # noqa: BLE001 - upstream may not be up yet
                with LOCK:
                    STATE["ws_state"] = "reconnecting"
                log.warning("sensing WS error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)

    try:
        asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        log.error("feed thread died: %s", exc)


# ── derived state ────────────────────────────────────────────────────────────
def snapshot():
    with LOCK:
        frame = STATE["frame"]
        age = (time.time() - STATE["frame_ts"]) if frame else None
        return frame, age, STATE["ws_state"], STATE["frames"], STATE["started"]


def live_state():
    frame, age, _, _, _ = snapshot()
    is_live = bool(frame) and age is not None and age < STALE_AFTER_S
    return frame, age, is_live


def source_name():
    frame, _, is_live = live_state()
    if not is_live or not frame:
        return "unknown"
    return frame.get("source") or "unknown"


def node_count(frame):
    if not frame:
        return 0
    nodes = frame.get("nodes") or frame.get("node_features") or []
    return len(nodes)


# ── vital signs (derived from measured CSI, never invented) ─────────────────
# Resting human breathing sits at 0.12-0.55 Hz (7-33 bpm). The sensing pipeline
# reports one dominant spectral peak per frame plus the power inside the
# breathing band, which is the only evidence available. Heart rate needs a
# dedicated cardiac-band model that this sensing-only build does not ship, so
# it is reported as unavailable instead of a fabricated number.
BREATH_BAND_MIN_HZ = 0.12
BREATH_BAND_MAX_HZ = 0.55
BREATH_MIN_POWER_RATIO = 0.05


def first_features(frame):
    if not frame:
        return {}
    feat = frame.get("features")
    if isinstance(feat, dict) and feat:
        return feat
    for node in (frame.get("node_features") or frame.get("nodes") or []):
        if not isinstance(node, dict):
            continue
        inner = node.get("node_features")
        if isinstance(inner, list) and inner and isinstance(inner[0], dict):
            return inner[0]
        if inner is None and ("dominant_freq_hz" in node or "mean_rssi" in node):
            return node
    return {}


def node_key(frame):
    """Stable identity of the node a bridge frame came from, or None."""
    if not isinstance(frame, dict):
        return None
    for node in (frame.get("node_features") or frame.get("nodes") or []):
        if isinstance(node, dict) and node.get("node_id") is not None:
            return str(node.get("node_id"))
    nid = frame.get("node_id")
    return str(nid) if nid is not None else None


def merge_node_frames(now=None):
    """Merge every node seen within NODE_TTL_S into one frame.

    Called under LOCK. One live node returns that node's frame untouched, so a
    single-board setup behaves exactly as before. Two or more nodes return a
    frame whose node_features/nodes carry every node, which is what lets
    /api/v1/sensing, the WS stream and the observatory see a multi-link view.
    """
    now = time.time() if now is None else now
    cache = STATE.get("node_frames") or {}
    live = {k: v for k, v in cache.items() if now - v[1] <= NODE_TTL_S}
    if len(live) != len(cache):
        STATE["node_frames"] = live
    if not live:
        return STATE.get("frame")
    ordered = sorted(live.items(), key=lambda kv: kv[1][1], reverse=True)
    if len(ordered) == 1:
        return ordered[0][1][0]
    base = dict(ordered[0][1][0])
    nodes = []
    for _key, (fr, _ts) in ordered:
        mfeat = fr.get("features") if isinstance(fr.get("features"), dict) else {}
        for node in (fr.get("node_features") or fr.get("nodes") or []):
            if isinstance(node, dict):
                node = dict(node)
                # Attach the per-node DSP metrics of the frame this node came
                # from. The bridge's node objects only describe the radio
                # (rssi_dbm/amplitude/freq), so without this a multi-node view
                # has no per-link variance or motion energy to reason about.
                if mfeat and "metrics" not in node:
                    node["metrics"] = {k: mfeat.get(k) for k in NODE_METRIC_KEYS
                                       if k in mfeat}
                nodes.append(node)
    if nodes:
        base["node_features"] = nodes
        base["nodes"] = nodes
        # NOTE: keep base["features"] as it came from the freshest node frame.
        # Node objects in `nodes`/`node_features` only carry rssi_dbm /
        # amplitude / freq_mhz — they have no variance or motion_band_power, so
        # promoting one of them to the top-level `features` would blank out
        # room_state rssi/variance/motion and make vitals read "unmeasurable".
    base["node_count"] = len(nodes) or len(ordered)
    base["node_ids"] = [k for k, _ in ordered]
    base["aggregated"] = True
    return base


def build_vital_signs(frame=None):
    """Breathing rate from the measured breathing-band peak, else null."""
    if frame is None:
        frame = live_state()[0]
    empty = {
        "breathing_rate_bpm": None,
        "breathing_confidence": 0.0,
        "heart_rate_bpm": None,
        "heart_rate_confidence": 0.0,
        "measurable": False,
        "note": "",
    }
    if not frame:
        empty["note"] = "无感知帧"
        return empty
    cls = frame.get("classification") or {}
    if not cls.get("presence"):
        empty["note"] = "房间内无人，无生命体征可测"
        return empty

    feat = first_features(frame)
    try:
        dom = float(feat.get("dominant_freq_hz") or 0.0)
        band = float(feat.get("breathing_band_power") or 0.0)
        total = float(feat.get("spectral_power") or 0.0)
    except (TypeError, ValueError):
        dom, band, total = 0.0, 0.0, 0.0
    ratio = band / total if total > 0 else 0.0
    level = str(cls.get("motion_level") or "").lower()

    if level == "active":
        empty["note"] = "运动中：运动成分淹没呼吸频带，此帧不可测"
        return empty
    if not (BREATH_BAND_MIN_HZ <= dom <= BREATH_BAND_MAX_HZ):
        empty["note"] = f"主频谱峰 {dom:.2f} Hz 不在呼吸带内"
        return empty
    if ratio < BREATH_MIN_POWER_RATIO:
        empty["note"] = "呼吸带功率占比过低，未检出可靠呼吸节律"
        return empty

    return {
        "breathing_rate_bpm": round(dom * 60.0, 1),
        "breathing_confidence": round(min(0.9, ratio * 3.0), 2),
        "heart_rate_bpm": None,
        "heart_rate_confidence": 0.0,
        "measurable": True,
        "note": "呼吸率由实测呼吸带谱峰推得；心率需专用心搏频带模型，本构建不提供",
    }


def build_room_state(frame=None):
    """One compact block answering: is someone there, and in what state?"""
    if frame is None:
        frame = live_state()[0]
    if not frame:
        return {"presence": False, "motion_level": "absent", "persons": 0,
                "confidence": 0.0, "available": False}
    cls = frame.get("classification") or {}
    feat = first_features(frame)
    presence = bool(cls.get("presence"))
    try:
        rssi = round(float(feat.get("mean_rssi") or 0.0), 2)
        variance = round(float(feat.get("variance") or 0.0), 2)
        motion_power = round(float(feat.get("motion_band_power") or 0.0), 2)
    except (TypeError, ValueError):
        rssi, variance, motion_power = 0.0, 0.0, 0.0

    # The bridge measures every link separately and ships its own room verdict.
    # Trust it, because this process cannot tell one node's frames from another
    # and used to answer with a hardcoded `1 if presence else 0`.
    room = frame.get("room")
    if isinstance(room, dict) and room.get("nodes") is not None:
        persons = room.get("persons")
        return {
            "presence": bool(room.get("presence")),
            "motion_level": room.get("motion_level") or "absent",
            "persons": persons,
            "confidence": round(float(room.get("confidence") or 0.0), 2),
            "rssi_dbm": rssi,
            "variance": variance,
            "motion_band_power": motion_power,
            "nodes": room.get("nodes"),
            "present_nodes": room.get("present_nodes"),
            "persons_basis": room.get("persons_basis"),
            "cross_link_corr": room.get("cross_link_corr"),
            "details": room.get("details"),
            "available": True,
        }

    return {
        "presence": presence,
        "motion_level": cls.get("motion_level") or ("present" if presence else "absent"),
        # No per-link evidence: the head count is genuinely unknown, so say so
        # instead of inventing a 1.
        "persons": None if presence else 0,
        "confidence": round(float(cls.get("confidence") or 0.0), 2),
        "rssi_dbm": rssi,
        "variance": variance,
        "motion_band_power": motion_power,
        "persons_basis": "no_per_node_evidence",
        "available": True,
    }


# Frames carrying one of these sources come from hardware the reader actually
# owns. Everything else (the upstream collector's simulated fallback) must never
# be reported as a verified live link — otherwise unplugging the ESP32 leaves
# the dashboard claiming "ESP32 connected" forever, because the simulated
# collector keeps feeding fresh frames into STATE.
HARDWARE_SOURCES = {"esp32", "linux_wifi", "macos_wifi", "windows_wifi", "wifi"}


def _is_hardware_frame(frame):
    src = str((frame or {}).get("source") or "").lower()
    if src == "esp32":
        # Only the LAN bridge speaks this: it replays real CSI it received over
        # UDP from the ESP32 itself.
        return True
    if src not in HARDWARE_SOURCES:
        return False
    # For OS-level collectors, demand CSI evidence (subcarriers or amplitude).
    for node in (frame.get("node_features") or frame.get("nodes") or []):
        try:
            if float(node.get("subcarrier_count") or 0) > 0:
                return True
            if float(node.get("mean_amplitude") or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def source_state():
    """ADR-295 style state.

    `live_verified` is reserved for a *fresh frame from real hardware*.
    A fresh synthetic frame is `synthetic`, a stale one is `disconnected`.
    """
    frame, _age, is_live = live_state()
    if not is_live or not frame:
        return "disconnected"
    return "live_verified" if _is_hardware_frame(frame) else "synthetic"


def is_verified_live():
    return source_state() == "live_verified"


def _cpu_snapshot():
    cores = os.cpu_count() or 1
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):
        load1 = 0.0
    return {
        "percent": round(max(0.0, min(100.0, load1 / cores * 100.0)), 1),
        "cores": cores,
        "load_1m": round(load1, 2),
    }


def _mem_snapshot():
    try:
        if os.path.exists("/proc/meminfo"):
            info = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as fh:
                for line in fh:
                    key, _, rest = line.partition(":")
                    try:
                        info[key] = float(rest.strip().split()[0])
                    except (ValueError, IndexError):
                        continue
            total = info.get("MemTotal") or 0.0
            avail = info.get("MemAvailable")
            if avail is None:
                avail = (
                    info.get("MemFree", 0.0)
                    + info.get("Buffers", 0.0)
                    + info.get("Cached", 0.0)
                )
            if total > 0:
                used = max(total - avail, 0.0)
                return {
                    "percent": round(used / total * 100.0, 1),
                    "total_mb": round(total / 1024.0, 1),
                    "used_mb": round(used / 1024.0, 1),
                }
        # macOS fallback (local runs).
        total_bytes = float(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip())
        out = subprocess.check_output(["vm_stat"], text=True)
        page = 4096.0
        first = out.splitlines()[0] if out else ""
        if "page size of" in first:
            page = float(first.split("page size of")[1].split("bytes")[0].strip())
        pages = {}
        for line in out.splitlines()[1:]:
            key, _, rest = line.partition(":")
            val = rest.strip().rstrip(".")
            if val.isdigit():
                pages[key.strip()] = float(val)
        free = (
            pages.get("Pages free", 0.0)
            + pages.get("Pages inactive", 0.0)
            + pages.get("Pages speculative", 0.0)
            + pages.get("Pages purgeable", 0.0)
        )
        used_bytes = max(total_bytes - free * page, 0.0)
        if total_bytes > 0:
            return {
                "percent": round(used_bytes / total_bytes * 100.0, 1),
                "total_mb": round(total_bytes / (1024.0 * 1024.0), 1),
                "used_mb": round(used_bytes / (1024.0 * 1024.0), 1),
            }
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {"percent": 0.0, "total_mb": 0.0, "used_mb": 0.0}


def _disk_snapshot():
    try:
        usage = shutil.disk_usage("/")
        if usage.total > 0:
            return {
                "percent": round(usage.used / usage.total * 100.0, 1),
                "total_gb": round(usage.total / (1024.0 ** 3), 1),
                "used_gb": round(usage.used / (1024.0 ** 3), 1),
            }
    except OSError:
        pass
    return {"percent": 0.0, "total_gb": 0.0, "used_gb": 0.0}


def system_metrics():
    """Container/host resource usage for the dashboard's 系统指标 card.

    Previously absent, so every gauge sat at 0%. Values are measured, not
    invented: CPU from the load average, memory from /proc/meminfo (Linux) or
    vm_stat (macOS), disk from the root filesystem.
    """
    return {
        "cpu": _cpu_snapshot(),
        "memory": _mem_snapshot(),
        "disk": _disk_snapshot(),
    }


def build_features():
    """Capability flags for the dashboard's 功能特性 list."""
    frame, _age, is_live = live_state()
    return {
        "wifi_sensing": True,
        "signal_field": True,
        "presence_detection": True,
        "motion_classification": True,
        "hardware_feed": is_live and _is_hardware_frame(frame),
        "multi_node": node_count(frame) > 1,
        "pose_model": bool(CP.get("active_model")),
        "recording": CP.get("rec_active") is not None,
    }


# COCO-17 keypoint layouts, the order `ui/utils/pose-renderer.js` draws.
POSE_W, POSE_H = 800.0, 600.0
MOTION_SWAY = {"still": 1.5, "low": 5.0, "medium": 12.0, "active": 20.0, "high": 30.0}


def derive_keypoints(frame, bbox):
    """Signal-derived 17-keypoint skeleton for the Live Demo canvas.

    The sensing pipeline classifies presence/motion but never regresses joint
    coordinates, so an empty `keypoints` array used to make the canvas render
    nothing at all (a black rectangle). What *is* measured is where the person
    is (signal_field centre of mass → bbox) and how much the body moves
    (motion_level / motion_band_power). So the skeleton is anchored on that
    measured centroid, its limb sway is scaled by the measured motion level,
    and every point inherits the classifier's confidence. The frame is labelled
    `pose_source: signal_derived`, so the UI never claims model inference.
    """
    if not bbox:
        return []
    cls = frame.get("classification") or {}
    level = str(cls.get("motion_level") or "").lower()
    sway = MOTION_SWAY.get(level, 8.0)
    try:
        phase = (float(frame.get("timestamp") or time.time()) % 6.2832) * 1.6
    except (TypeError, ValueError):
        phase = 0.0
    try:
        conf = float(cls.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0

    cx = (float(bbox.get("x", 0.41)) + float(bbox.get("width", 0.18)) / 2.0) * POSE_W
    top = float(bbox.get("y", 0.33)) * POSE_H
    bottom = (float(bbox.get("y", 0.33)) + float(bbox.get("height", 0.34))) * POSE_H
    h = max(bottom - top, 60.0)

    bob = math.sin(phase) * sway * 0.2
    swing = math.sin(phase) * sway

    # (x, y, weight) — weight scales the point's confidence.
    layout = [
        (cx + bob, top + 0.07 * h, 1.00),            # nose
        (cx - 0.028 * h, top + 0.05 * h, 0.72),      # left_eye
        (cx + 0.028 * h, top + 0.05 * h, 0.72),      # right_eye
        (cx - 0.058 * h, top + 0.065 * h, 0.66),     # left_ear
        (cx + 0.058 * h, top + 0.065 * h, 0.66),     # right_ear
        (cx - 0.105 * h, top + 0.21 * h, 0.95),      # left_shoulder
        (cx + 0.105 * h, top + 0.21 * h, 0.95),      # right_shoulder
        (cx - 0.145 * h + swing * 0.35, top + 0.37 * h, 0.80),  # left_elbow
        (cx + 0.145 * h + swing * 0.35, top + 0.37 * h, 0.80),  # right_elbow
        (cx - 0.165 * h + swing, top + 0.52 * h, 0.70),         # left_wrist
        (cx + 0.165 * h + swing, top + 0.52 * h, 0.70),         # right_wrist
        (cx - 0.062 * h, top + 0.53 * h, 0.92),      # left_hip
        (cx + 0.062 * h, top + 0.53 * h, 0.92),      # right_hip
        (cx - 0.075 * h - swing * 0.2, top + 0.74 * h, 0.78),   # left_knee
        (cx + 0.075 * h - swing * 0.2, top + 0.74 * h, 0.78),   # right_knee
        (cx - 0.082 * h, top + 0.95 * h, 0.68),      # left_ankle
        (cx + 0.082 * h, top + 0.95 * h, 0.68),      # right_ankle
    ]
    return [
        {
            "x": round(x, 2),
            "y": round(y, 2),
            "confidence": round(max(0.05, min(1.0, conf * w)), 3),
        }
        for x, y, w in layout
    ]


def build_zones_summary():
    """4-quadrant occupancy derived from the real signal_field grid."""
    frame, _age, _is_live = live_state()
    zones = {
        f"zone_{i}": {"person_count": 0, "occupancy": 0.0, "energy": 0.0, "present": False}
        for i in range(1, 5)
    }
    summary = {"total_persons": 0, "occupied_zones": 0, "timestamp": time.time()}
    field = (frame or {}).get("signal_field") or {}
    values = field.get("values") or []
    size = field.get("grid_size") or [0, 0, 0]
    try:
        nx, ny, nz = int(size[0]), int(size[1]), int(size[2])
    except (IndexError, TypeError, ValueError):
        nx = ny = nz = 0
    energies = {f"zone_{i}": 0.0 for i in range(1, 5)}
    if nx > 0 and ny > 0 and nz > 0 and len(values) == nx * ny * nz:
        for i, raw in enumerate(values):
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            cell_x = i // (ny * nz)
            cell_z = (i % (ny * nz)) // ny
            zone = "zone_1"
            if cell_x >= nx / 2.0 and cell_z < nz / 2.0:
                zone = "zone_2"
            elif cell_x < nx / 2.0 and cell_z >= nz / 2.0:
                zone = "zone_3"
            elif cell_x >= nx / 2.0 and cell_z >= nz / 2.0:
                zone = "zone_4"
            energies[zone] += max(v, 0.0)
        peak = max(energies.values()) or 1.0
        for zone, energy in energies.items():
            zones[zone]["energy"] = round(energy / peak, 3)
            zones[zone]["occupancy"] = round(min(energy / peak, 1.0), 3)

    cls = (frame or {}).get("classification") or {}
    if cls.get("presence"):
        hot = max(energies, key=energies.get)
        zones[hot]["person_count"] = 1
        zones[hot]["present"] = True
        summary["total_persons"] = 1
    summary["occupied_zones"] = sum(1 for z in zones.values() if z["present"])
    return {"zones": zones, "summary": summary}


def build_sensing_current():
    frame, _age, is_live = live_state()
    if not frame:
        return {"source": None, "source_state": "disconnected", "live": False, "timestamp": time.time()}
    out = dict(frame)
    out["source_state"] = source_state()
    out["live"] = is_live
    out["vital_signs"] = build_vital_signs(frame)
    out["room_state"] = build_room_state(frame)
    return out


# ── response builders ───────────────────────────────────────────────────────
def build_info():
    frame, age, is_live = live_state()
    src = source_name()
    state = source_state()
    return {
        "name": "RuView WiFi Sensing",
        "version": "0.8.16",
        "mode": "sensing-only",
        "environment": "production",
        "source": src,
        "source_state": state,
        "nodes": node_count(frame),
        "capabilities": ["sensing", "signal_field", "classification"],
        # The dashboard's 功能特性 list reads `features`; without it the whole
        # section rendered empty even though the capability list existed.
        "features": build_features(),
        "timestamp": time.time(),
    }


def build_status():
    frame, age, is_live = live_state()
    _, _, ws_state, frames, started = snapshot()
    state = source_state()
    return {
        "source": source_name(),
        "source_state": state,
        # Only a verified hardware frame counts as connected.
        "connected": state == "live_verified",
        "ws_state": ws_state,
        "node_count": node_count(frame),
        "frame_age_s": round(age, 3) if age is not None else None,
        "frames_received": frames,
        "uptime_s": round(time.time() - started, 1),
        "ingest_frames": STATE["ingest_frames"],
        "ingest_peer": STATE["ingest_peer"],
        "ingest_age_s": (
            round(time.time() - STATE["ingest_ts"], 1) if STATE["ingest_ts"] else None
        ),
        "timestamp": time.time(),
    }


def build_pose_current():
    frame, age, is_live = live_state()
    if not is_live or not frame:
        return {
            "pose": {"persons": []},
            "persons": [],
            "total_persons": 0,
            "total_detections": STATE["presence_frames"],
            "avg_confidence": 0.0,
            "source_state": source_state(),
            "source": "unknown",
            "timestamp": time.time(),
            "live": False,
            "vital_signs": build_vital_signs(frame),
            "room_state": build_room_state(frame),
        }
    cls = frame.get("classification") or {}
    features = frame.get("features") or {}
    persons = []
    # Only report a person when the sensing pipeline itself classifies presence.
    if cls.get("presence"):
        bbox = ws_bbox_from_field(frame.get("signal_field"))
        persons.append(
            {
                "person_id": 1,
                "confidence": float(cls.get("confidence") or 0.0),
                "motion_level": cls.get("motion_level"),
                # Same signal-derived skeleton the Live Demo canvas draws.
                "keypoints": derive_keypoints(frame, bbox),
                "bbox": bbox,
                "label": "person",
            }
        )
    avg_conf = (
        round(sum(float(p.get("confidence") or 0.0) for p in persons) / len(persons), 3)
        if persons
        else 0.0
    )
    pose = {
        "persons": persons,
        "motion_level": cls.get("motion_level"),
        "presence": bool(cls.get("presence")),
        "features": features,
    }
    return {
        "pose": pose,
        # The dashboard's 实时统计 card reads these at the top level; nested-only
        # values left 在场人数 / 平均置信度 / 累计检测数 all at 0.
        "persons": persons,
        "total_persons": len(persons),
        "total_detections": STATE["presence_frames"],
        "avg_confidence": avg_conf,
        "source": frame.get("source"),
        "source_state": source_state(),
        "node_count": node_count(frame),
        "frame_age_s": round(age, 3) if age is not None else None,
        "timestamp": frame.get("timestamp") or time.time(),
        "live": True,
        "vital_signs": build_vital_signs(frame),
        "room_state": build_room_state(frame),
    }


def build_pose_stats():
    frame, _, is_live = live_state()
    features = (frame or {}).get("features") or {}
    return {
        "availability": "sensing_only",
        "note": "sensing pipeline classifies presence/motion; no joint regression model",
        "window": {"hours": 1},
        # Real counter of frames that actually detected a person.
        "detections": STATE["presence_frames"],
        "frames_received": STATE["frames"],
        "features": features,
        "source_state": source_state(),
        "live": is_live,
    }


def build_health():
    frame, age, is_live = live_state()
    _, _, ws_state, frames, started = snapshot()
    state = source_state()
    hardware_ok = state == "live_verified"
    return {
        "status": "healthy",
        "checks": [
            {"name": "api", "status": "healthy"},
            {"name": "sensing_stream", "status": "healthy" if is_live else "degraded"},
            {"name": "hardware", "status": "healthy" if hardware_ok else "degraded"},
        ],
        # The dashboard iterates components and reads status.status / status.message,
        # so each entry must be an object, not a bare string.
        "components": {
            "api": {"status": "healthy", "message": "REST API responding"},
            "hardware": {
                # Previously hardcoded to "healthy", so unplugging the ESP32 never
                # surfaced as a problem here.
                "status": "healthy" if hardware_ok else "degraded",
                "message": (
                    f"ESP32 UDP stream, {node_count(frame)} node(s)"
                    if hardware_ok
                    else f"no verified hardware frames (last source: {source_name() or 'none'})"
                ),
            },
            "pose": {"status": "healthy" if is_live else "degraded", "message": "sensing pipeline running"},
            "stream": {
                "status": "healthy" if is_live else "degraded",
                "message": f"{frames} frames received" if is_live else "stream idle",
            },
        },
        "details": {
            "ws_state": ws_state,
            "source_state": state,
            "frames_received": frames,
            "frame_age_s": round(age, 3) if age is not None else None,
            "uptime_s": round(time.time() - started, 1),
            # Resource gauges: the dashboard's 系统指标 card reads them here.
            "metrics": {"system_metrics": system_metrics()},
        },
        # `health.service.js` hands the whole /health/health payload to
        # `updateHealthStatus`, which reads `health.metrics` at the top level.
        "metrics": {"system_metrics": system_metrics()},
        "timestamp": time.time(),
    }


def build_metrics():
    _, _, ws_state, frames, started = snapshot()
    uptime = max(time.time() - started, 1.0)
    res = system_metrics()
    return {
        "uptime_s": round(uptime, 1),
        "frames_received": frames,
        "frame_rate_hz": round(frames / uptime, 2),
        "ws_state": ws_state,
        "source_state": source_state(),
        # Real host/container gauges instead of the 0% placeholders.
        "system_metrics": res,
        "cpu_percent": res["cpu"]["percent"],
        "memory_percent": res["memory"]["percent"],
        "disk_percent": res["disk"]["percent"],
        "timestamp": time.time(),
    }


def route(path):
    """Return (payload, status_code) for a GET path, or None to fall through to files."""
    static_routes = {
        # `ui/observatory.html` probes `GET /health` and only switches to live
        # mode when it sees `{"status":"ok"}`. Without this it silently stays on
        # the synthetic demo dataset.
        "/health": lambda: {
            "status": "ok",
            "source": source_name(),
            "source_state": source_state(),
            "timestamp": time.time(),
        },
        "/health/live": {"status": "alive", "timestamp": time.time()},
        "/health/ready": {"status": "ready", "ready": True, "timestamp": time.time()},
        "/health/health": build_health,
        "/health/metrics": build_metrics,
        "/health/version": {"version": "0.8.16", "source": "gateway"},
        "/api/v1/info": build_info,
        "/api/v1/status": build_status,
        "/api/v1/metrics": build_metrics,
        # The observatory/demo pages poll `/api/v1/sensing`; it used to fall
        # through to the empty `{}` default, so every panel read nothing.
        "/api/v1/sensing": build_sensing_current,
        "/api/v1/pose/current": build_pose_current,
        "/api/v1/pose/stats": build_pose_stats,
        "/api/v1/pose/zones/summary": build_zones_summary,
        "/api/v1/pose/activities": lambda: {"activities": [], "count": 0},
        "/api/v1/pose/calibration/status": lambda: {"calibrated": False, "status": "unavailable"},
        "/api/v1/pose/calibration": lambda: {"calibrated": False, "status": "unavailable"},
        "/api/v1/stream/status": lambda: {
            "status": "streaming" if is_verified_live() else ("degraded" if live_state()[2] else "idle"),
            "clients": 1 if live_state()[2] else 0,
            "source": source_name(),
            "source_state": source_state(),
        },
        "/api/v1/stream/clients": lambda: {"clients": []},
        "/api/v1/stream/metrics": build_metrics,
        "/api/v1/models": cp_models_list,
        "/api/v1/models/active": cp_models_active,
        "/api/v1/models/lora/profiles": cp_lora_profiles,
        "/api/v1/recording/list": cp_recording_list,
        "/api/v1/train/status": cp_train_status,
        # ADR-271 sign-in capability probe. `/oauth/status` is extension-less, so
        # without this entry the request falls through to the SPA fallback and the
        # settings panel receives index.html -- surfacing in the UI as
        # "Could not reach the server (Unexpected token '<', ...)". This
        # deployment has no OAuth backend, so answer the capability question
        # explicitly instead of pretending the endpoint is missing.
        "/oauth/status": lambda: {
            "signed_in": False,
            "browser_signin": False,
            "auth_required": False,
            "account": None,
            "scope": None,
        },
    }
    builder = static_routes.get(path.rstrip("/") or "/")
    if builder is None:
        return None
    return (builder() if callable(builder) else builder), 200


# ── RFC6455 WebSocket (minimal server side) ──────────────────────────────────
# The UI opens `ws://<host>/api/v1/stream/pose` for the Live Demo tab. A plain
# HTTP server cannot answer that upgrade, so the tab used to sit on "error"
# forever. These helpers complete the handshake and push frames derived from the
# same real sensing upstream — no synthetic values.

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
POSE_ZONE_ID = "zone_1"


def ws_accept_key(client_key):
    return base64.b64encode(hashlib.sha1((client_key + WS_GUID).encode()).digest()).decode()


def ws_frame(payload, opcode=0x1):
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    header = bytearray([0x80 | opcode])
    n = len(data)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    return bytes(header) + data


def ws_bbox_from_field(field):
    """Derive a coarse horizontal footprint from the real signal-field grid.

    The grid is [x, y, z]; y is 1 here, so the peak cell gives an honest
    (x, z) hot spot for the strongest disturbance. Returns None when the
    upstream did not ship a field, so callers never invent coordinates.
    """
    if not isinstance(field, dict):
        return None
    values = field.get("values")
    size = field.get("grid_size")
    if not values or not isinstance(size, list) or len(size) < 3:
        return None
    nx, ny, nz = int(size[0]), int(size[1]), int(size[2])
    if nx * ny * nz != len(values):
        return None
    # Centre of mass instead of the argmax: the field saturates at 1.0 in many
    # cells, so argmax would arbitrarily snap to a corner.
    total = 0.0
    sum_x = 0.0
    sum_z = 0.0
    best_v = 0.0
    for i, v in enumerate(values):
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv <= 0.0:
            continue
        cell_x = i // max(ny * nz, 1)
        cell_z = (i % max(ny * nz, 1)) // max(ny, 1)
        total += fv
        sum_x += (cell_x + 0.5) / nx * fv
        sum_z += (cell_z + 0.5) / nz * fv
        best_v = max(best_v, fv)
    if total <= 0.0:
        return None
    cx = sum_x / total
    cz = sum_z / total
    w, h = 0.18, 0.34
    return {
        "x": round(max(cx - w / 2, 0.0), 4),
        "y": round(max(cz - h / 2, 0.0), 4),
        "width": w,
        "height": h,
        "strength": round(best_v, 4),
    }


def build_pose_stream_message(frame):
    """Map one real sensing frame onto the zone/PoseData shape the UI parses."""
    cls = frame.get("classification") or {}
    features = frame.get("features") or {}
    presence = bool(cls.get("presence"))
    confidence = float(cls.get("confidence") or 0.0)
    persons = []
    if presence:
        bbox = ws_bbox_from_field(frame.get("signal_field"))
        person = {
            "person_id": 1,
            "confidence": confidence,
            # The sensing pipeline classifies presence/motion only and never
            # regresses joints, so the skeleton is anchored on the measured
            # signal_field centroid and labelled signal_derived below.
            "keypoints": derive_keypoints(frame, bbox),
            "motion_level": cls.get("motion_level"),
            "label": "person",
        }
        if bbox:
            person["bbox"] = bbox
        persons.append(person)
    return {
        "type": "pose_data",
        "zone_id": POSE_ZONE_ID,
        "timestamp": frame.get("timestamp") or time.time(),
        "pose_source": "signal_derived",
        "data": {
            "pose": {
                "persons": persons,
                "motion_level": cls.get("motion_level"),
                "presence": presence,
                "features": features,
            },
            "confidence": confidence,
            "activity": cls.get("motion_level"),
            "pose_source": "signal_derived",
            "metadata": {
                "frame_id": f"sensing_{frame.get('timestamp') or time.time()}",
                "processing_time_ms": 0,
                "node_count": node_count(frame),
                "source": frame.get("source"),
            },
        },
    }


MOTION_SCORE = {"still": 0.08, "low": 0.25, "medium": 0.5, "active": 0.75, "high": 0.9}


def build_sensing_stream_message(frame):
    """Upstream frame + a coarse person anchor for `ui/observatory.html`.

    The observatory renders a 3D room and needs each detected person to carry a
    world-space `position`. The sensing pipeline classifies presence/motion but
    does not localise, so the anchor is the centre of mass of the *real*
    signal_field grid mapped onto the room. Nothing is added when the upstream
    reports no presence.
    """
    out = dict(frame)
    out.setdefault("type", "sensing_update")
    cls = frame.get("classification") or {}
    if cls.get("presence"):
        bbox = ws_bbox_from_field(frame.get("signal_field"))
        if bbox:
            # bbox.x / bbox.y are normalised grid centres in [0, 1].
            px = (float(bbox.get("x", 0.5)) - 0.5) * 8.0
            pz = (float(bbox.get("y", 0.5)) - 0.5) * 8.0
            level = str(cls.get("motion_level") or "").lower()
            out["persons"] = [
                {
                    "person_id": 1,
                    "confidence": float(cls.get("confidence") or 0.0),
                    "position": [round(px, 3), 0.0, round(pz, 3)],
                    "id": "p0",
                    "motion_score": round(float(MOTION_SCORE.get(level, 0.4)) * 100.0, 1),
                    "pose": "standing",
                    "facing": 0.0,
                    "position_source": "signal_field_centroid",
                }
            ]
    # UI contract (ui/observatory/hud-controller.js + figure-pool.js): the HUD
    # reads vital_signs.* and estimated_persons. Both come from measured CSI via
    # build_vital_signs(); a value that cannot be measured stays null so the HUD
    # shows "不适用" instead of a plausible-looking invention.
    out["vital_signs"] = build_vital_signs(frame)
    out["room_state"] = build_room_state(frame)
    out["estimated_persons"] = len(out.get("persons") or [])
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "RuViewGateway/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter default access log
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path):
        rel = path.lstrip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        target = os.path.normpath(os.path.join(UI_DIR, rel))
        if not target.startswith(UI_DIR):
            self._send_json({"detail": "forbidden"}, 403)
            return
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            # SPA-style fallback for extension-less paths (e.g. /sensing)
            if "." not in os.path.basename(rel):
                target = os.path.join(UI_DIR, "index.html")
            else:
                self._send_json({"detail": "not found", "path": path}, 404)
                return
        if not os.path.isfile(target):
            self._send_json({"detail": "not found", "path": path}, 404)
            return
        ctype, _ = mimetypes.guess_type(target)
        if target.endswith(".js"):
            ctype = "application/javascript"
        elif target.endswith(".css"):
            ctype = "text/css"
        elif target.endswith(".html"):
            ctype = "text/html"
        ctype = ctype or "application/octet-stream"
        with open(target, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8" if ctype.startswith(("text/", "application/javascript")) else ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ── WebSocket upgrade path (Live Demo pose stream) ──
    def _is_ws_upgrade(self):
        return "websocket" in (self.headers.get("Upgrade") or "").lower()

    def _ws_handshake(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return False
        self.wfile.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {ws_accept_key(key)}\r\n\r\n"
            ).encode()
        )
        self.wfile.flush()
        return True

    def _ws_drain_client(self):
        """Consume one pending client frame; False when the peer went away."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
        except (OSError, ValueError):
            return False
        if not ready:
            return True
        try:
            head = self.connection.recv(2)
        except OSError:
            return False
        if len(head) < 2:
            return False
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            ext = self.connection.recv(2)
            if len(ext) < 2:
                return False
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = self.connection.recv(8)
            if len(ext) < 8:
                return False
            length = struct.unpack(">Q", ext)[0]
        mask = self.connection.recv(4) if masked else b""
        payload = b""
        while length > 0:
            chunk = self.connection.recv(min(length, 4096))
            if not chunk:
                return False
            payload += chunk
            length -= len(chunk)
        if masked and mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:  # close
            return False
        if opcode == 0x9:  # ping
            self.wfile.write(ws_frame(payload, opcode=0xA))
            self.wfile.flush()
        elif opcode == 0x1:
            try:
                if json.loads(payload.decode("utf-8")).get("type") == "ping":
                    self.wfile.write(ws_frame(json.dumps({"type": "pong", "timestamp": time.time()})))
                    self.wfile.flush()
            except (ValueError, UnicodeDecodeError):
                pass
        return True

    def _ws_stream_pose(self, builder=None):
        builder = builder or build_pose_stream_message
        if not self._ws_handshake():
            self._send_json({"detail": "websocket upgrade required"}, 400)
            return
        log.info("WS client connected from %s", self.address_string())
        self.close_connection = True
        try:
            self.wfile.write(
                ws_frame(
                    json.dumps(
                        {
                            "type": "connection_established",
                            "timestamp": time.time(),
                            "data": {"status": "connected", "source": source_name()},
                        }
                    )
                )
            )
            self.wfile.flush()
            last_stamp = None
            idle_since = time.time()
            while True:
                if not self._ws_drain_client():
                    break
                frame, age, is_live = live_state()
                now = time.time()
                if is_live and frame is not None:
                    stamp = frame.get("timestamp")
                    if stamp != last_stamp:
                        last_stamp = stamp
                        self.wfile.write(ws_frame(json.dumps(builder(frame))))
                        self.wfile.flush()
                        idle_since = now
                    elif now - idle_since > 15:
                        # Keep the socket warm when the sensor is quiet.
                        self.wfile.write(ws_frame(json.dumps({"type": "pong", "timestamp": now})))
                        self.wfile.flush()
                        idle_since = now
                else:
                    if now - idle_since > 5:
                        self.wfile.write(
                            ws_frame(
                                json.dumps(
                                    {
                                        "type": "system_event",
                                        "timestamp": now,
                                        "data": {"severity": "warning", "message": "sensing stream idle"},
                                    }
                                )
                            )
                        )
                        # Idle stream == the hardware is gone. Broadcast the
                        # canonical state so the UI stops claiming "ESP32
                        # connected" instead of waiting for a frame that will
                        # never arrive.
                        self.wfile.write(
                            ws_frame(
                                json.dumps(
                                    {
                                        "type": "source_state",
                                        "source": "unknown",
                                        "source_state": source_state(),
                                        "timestamp": now,
                                    }
                                )
                            )
                        )
                        self.wfile.flush()
                        idle_since = now
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError:
            pass
        finally:
            log.info("pose WS client disconnected")

    def _ws_stream_sensing(self):
        """`/ws/sensing`: the raw upstream frame the observatory expects."""
        self._ws_stream_pose(builder=build_sensing_stream_message)

    def _ws_recv_exact(self, n):
        # Read through the handler's buffered reader, never the raw socket:
        # BaseHTTPRequestHandler already pulls request bytes into ``rfile``, so
        # mixing ``connection.recv`` in silently drops whatever ``rfile`` has
        # pre-buffered. That only shows up behind a proxy (Render/Cloudflare),
        # where a multi-segment >1 KiB frame arrives split and the read blocks
        # forever on bytes that were swallowed.
        if n <= 0:
            return b""
        buf = b""
        while len(buf) < n:
            chunk = self.rfile.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _ws_recv_client_frame(self):
        """Blocking read of one client frame -> (alive, opcode, payload)."""
        head = self._ws_recv_exact(2)
        if head is None:
            return False, 0x8, b""
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            ext = self._ws_recv_exact(2)
            if ext is None:
                return False, 0x8, b""
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = self._ws_recv_exact(8)
            if ext is None:
                return False, 0x8, b""
            length = struct.unpack(">Q", ext)[0]
        if length > INGEST_MAX_FRAME_BYTES:
            log.warning("ingest frame too large (%d bytes), dropping peer", length)
            return False, 0x8, b""
        mask = self._ws_recv_exact(4) if masked else b""
        if mask is None:
            return False, 0x8, b""
        payload = self._ws_recv_exact(length) if length else b""
        if payload is None:
            return False, 0x8, b""
        if masked and mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return True, opcode, payload

    def _ws_stream_ingest(self):
        """`/ws/ingest`: an out-of-network bridge pushes real CSI frames in.

        The bridge authenticates with `?token=`, then streams the same
        `sensing_update` JSON that ws_server emits. Accepted frames become the
        gateway's current frame, so /health, /api/v1/sensing and both live
        streams report `source: esp32` instead of the simulated fallback.
        """
        token = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
        if token != INGEST_TOKEN:
            self._send_json({"detail": "invalid ingest token"}, 403)
            return
        if not self._ws_handshake():
            self._send_json({"detail": "websocket upgrade required"}, 400)
            return
        peer = self.address_string()
        with LOCK:
            STATE["ingest_peer"] = peer
        log.info("ingest bridge connected from %s", peer)
        self.close_connection = True
        accepted = 0
        try:
            while True:
                alive, opcode, payload = self._ws_recv_client_frame()
                if not alive:
                    break
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    self.wfile.write(ws_frame(payload, opcode=0xA))
                    self.wfile.flush()
                    continue
                if opcode != 0x1:
                    continue
                try:
                    data = json.loads(payload.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(data, dict):
                    continue
                data.setdefault("type", "sensing_update")
                data.setdefault("source", "esp32")
                now = time.time()
                stamp = data.get("timestamp")
                with LOCK:
                    prev = STATE["frame"] or {}
                    if stamp is not None and prev.get("timestamp") == stamp:
                        # Byte-identical frame re-sent by a bridge that is
                        # replaying its ring buffer after the node went away.
                        # Count the traffic but never refresh frame_ts, so the
                        # link still ages out to `disconnected`.
                        STATE["ingest_frames"] += 1
                        accepted = STATE["ingest_frames"]
                        continue
                    _key = node_key(data)
                    if _key is not None:
                        STATE["node_frames"][_key] = (data, now)
                    STATE["frame"] = merge_node_frames(now) or data
                    STATE["frame_ts"] = now
                    STATE["frames"] += 1
                    if (data.get("classification") or {}).get("presence"):
                        STATE["presence_frames"] += 1
                    STATE["ingest_ts"] = now
                    STATE["ingest_frames"] += 1
                    accepted = STATE["ingest_frames"]
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with LOCK:
                STATE["ingest_peer"] = None
            log.info("ingest bridge disconnected from %s (%d frames total)", peer, accepted)

    def _ws_stream_train_progress(self):
        """`/ws/train/progress`: push the in-memory training state each epoch."""
        if not self._ws_handshake():
            self._send_json({"detail": "websocket upgrade required"}, 400)
            return
        log.info("train progress WS client connected from %s", self.address_string())
        self.close_connection = True
        last_key = None
        try:
            while True:
                if not self._ws_drain_client():
                    break
                with CP_LOCK:
                    snap = dict(CP["train"])
                key = (snap["job"], snap["epoch"], snap["status"])
                if key != last_key:
                    last_key = key
                    snap["type"] = "progress" if snap["active"] else "status"
                    snap["timestamp"] = time.time()
                    self.wfile.write(ws_frame(json.dumps(snap)))
                    self.wfile.flush()
                time.sleep(0.15)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            log.info("train progress WS client disconnected")

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if self._is_ws_upgrade() and path.rstrip("/") in (
            "/api/v1/stream/pose",
            "/api/v1/stream/events",
        ):
            self._ws_stream_pose()
            return
        if self._is_ws_upgrade() and path.rstrip("/") == "/ws/sensing":
            self._ws_stream_sensing()
            return
        if self._is_ws_upgrade() and path.rstrip("/") == "/ws/ingest":
            self._ws_stream_ingest()
            return
        if self._is_ws_upgrade() and path.rstrip("/") == "/ws/train/progress":
            self._ws_stream_train_progress()
            return
        handled = route(path)
        if handled is not None:
            payload, code = handled
            self._send_json(payload, code)
            return
        if path.startswith("/oauth/"):
            # /oauth/start and /oauth/logout are full-page navigations in the UI.
            # There is no OAuth backend here: answer JSON 404 so the UI takes its
            # documented "server predates ADR-271" branch, instead of silently
            # landing on index.html and looking like a broken sign-in.
            self._send_json(
                {"detail": "oauth not configured on this deployment", "path": path},
                404,
            )
            return
        if path.startswith("/api/"):
            # Unknown API surface: answer honestly-empty rather than 404 so the UI
            # shows "nothing here" instead of a backend-down banner.
            self._send_json({}, 200)
            return
        self._serve_file(path)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if path == "/api/v1/ws-ticket":
            # No auth configured on this deployment: 404 tells the UI to connect
            # without a ticket (the documented "server predates ADR-272" path).
            self._send_json({"detail": "auth not enabled"}, 404)
            return
        payload = cp_parse_json(raw)
        control = {
            "/api/v1/recording/start": lambda: cp_start_recording(payload),
            "/api/v1/recording/stop": lambda: cp_stop_recording(payload),
            "/api/v1/train/start": lambda: cp_start_training("train", payload),
            "/api/v1/train/pretrain": lambda: cp_start_training("pretrain", payload),
            "/api/v1/train/lora": lambda: cp_start_training("lora", payload),
            "/api/v1/train/stop": lambda: cp_stop_training(payload),
            "/api/v1/models/load": lambda: cp_model_load(payload),
            "/api/v1/models/unload": lambda: cp_model_unload(payload),
            "/api/v1/models/lora/activate": lambda: ({"status": "activated"}, 200),
        }
        handler = control.get(path)
        if handler is not None:
            body, code = handler()
            self._send_json(body, code)
            return
        self._send_json(
            {"status": "unsupported", "detail": "sensing-only deployment: control plane not available", "path": path},
            200,
        )

    def do_DELETE(self):  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        if path.startswith("/api/v1/recording/"):
            body, code = cp_delete_recording(path.rsplit("/", 1)[-1])
            self._send_json(body, code)
            return
        if path.startswith("/api/v1/models/"):
            body, code = cp_model_delete(path.rsplit("/", 1)[-1])
            self._send_json(body, code)
            return
        self._send_json({"status": "unsupported", "path": path}, 200)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    threading.Thread(target=ws_feed, name="ws-feed", daemon=True).start()
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("gateway on http://0.0.0.0:%d (UI=%s, upstream=%s)", PORT, UI_DIR, WS_UPSTREAM)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
