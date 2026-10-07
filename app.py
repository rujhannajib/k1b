"""
k1b: web interface for the Booster K1.

Runs ON THE ROBOT. Start it with:
    cd ~/Workspace/k1b && source .venv/bin/activate
    uvicorn app:app --host 0.0.0.0 --port 8000

Then open http://192.168.0.73:8000 in any browser on the same network.

Features (v1):
    - Live status: battery, mode, robot info
    - Speak: type text, the robot says it (Piper voice, espeak-ng fallback)

No motion commands in this version.
"""
import dataclasses
import enum
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from boosteros.robots.booster import BoosterRobot

BASE_DIR = Path(__file__).resolve().parent
VOICES_DIR = BASE_DIR / "voices"
DEFAULT_VOICE = "en_US-lessac-medium"
MAX_TEXT = 300

# Only one BoosterRobot instance may exist per robot (Booster SDK rule).
robot = None
robot_info = {}

sdk_lock = threading.Lock()      # serialises SDK calls
speech_lock = threading.Lock()   # one sentence at a time


# ---------- helpers ----------

def public_attributes(value):
    """Plain data attributes of an object, including C++-bound and slotted classes."""
    attrs = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(value, name)
        except Exception:
            continue
        if callable(attr):
            continue
        attrs[name] = attr
    return attrs


def to_jsonable(value, depth=0):
    """Turn SDK objects into plain dicts/lists so FastAPI can send them as JSON."""
    if depth > 6:
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "tolist"):  # numpy arrays and numbers
        return value.tolist()
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, enum.Enum):
        return value.name
    if dataclasses.is_dataclass(value):
        return {f.name: to_jsonable(getattr(value, f.name), depth + 1)
                for f in dataclasses.fields(value)}
    attrs = public_attributes(value)
    if attrs:
        return {k: to_jsonable(v, depth + 1) for k, v in attrs.items()}
    return str(value)


def read(fn):
    """Call an SDK getter safely. Returns {'error': ...} instead of crashing."""
    try:
        with sdk_lock:
            return to_jsonable(fn())
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


BATTERY_KEYS = ("percentage", "percent", "soc", "state_of_charge", "battery_level",
                "level", "capacity", "remaining")


def battery_percent(battery):
    """Find the battery percentage in whatever shape the SDK returns."""
    if not isinstance(battery, dict):
        return None
    # Exact key names first, then any key containing one of them.
    for key in BATTERY_KEYS:
        value = battery.get(key)
        if number(value):
            return round(value * 100 if value <= 1 else value, 1)
    for key, value in battery.items():
        if number(value) and any(k in key.lower() for k in BATTERY_KEYS):
            return round(value * 100 if value <= 1 else value, 1)
    return None


def is_temp_key(key):
    return "temp" in str(key).lower()


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def battery_temperature(battery):
    """Battery temperature in °C, if the SDK reports one."""
    if isinstance(battery, dict):
        for key, value in battery.items():
            if is_temp_key(key) and number(value):
                return round(value, 1)
    return None


def system_temperatures():
    """Read the Jetson's thermal sensors (CPU, GPU, SoC...) from Linux."""
    temps = []
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        try:
            name = (zone / "type").read_text().strip()
            value = int((zone / "temp").read_text().strip()) / 1000
        except (OSError, ValueError):
            continue
        if 0 < value < 150:  # skip disabled or bogus sensors
            temps.append({"name": name.replace("-thermal", ""), "temp": round(value, 1)})
    return temps


def motor_temperatures(joint_states):
    """
    Find per-joint temperatures in whatever shape the SDK returns.
    The documented JointState has no temperature field, so we look in
    `extra` and in any key containing 'temp'. Returns [] if none found.
    """
    found = []

    def walk(obj):
        if isinstance(obj, dict):
            # Shape 1: parallel lists, e.g. {"names": [...], "temperatures": [...]}
            names = obj.get("names")
            if isinstance(names, list):
                for key, value in obj.items():
                    if is_temp_key(key) and isinstance(value, list) and len(value) == len(names):
                        found.extend({"joint": n, "temp": round(t, 1)}
                                     for n, t in zip(names, value) if number(t))
                        return
            # Shape 2: one dict per joint, temperature directly or inside "extra"
            name = obj.get("name")
            if isinstance(name, str):
                candidates = list(obj.items())
                if isinstance(obj.get("extra"), dict):
                    candidates += list(obj["extra"].items())
                for key, value in candidates:
                    if is_temp_key(key) and number(value):
                        found.append({"joint": name, "temp": round(value, 1)})
                        return
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(joint_states)
    return sorted(found, key=lambda m: m["temp"], reverse=True)


def audio_env():
    """paplay needs to find the user's PulseAudio session."""
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def available_voices():
    piper = sorted(p.stem for p in VOICES_DIR.glob("*.onnx")) if VOICES_DIR.exists() else []
    return piper + ["espeak"]


def synthesize(text, voice, wav_path):
    """Write speech for `text` to `wav_path`. Returns the engine used."""
    model = VOICES_DIR / f"{voice}.onnx"
    if voice != "espeak" and model.exists():
        cmd = [sys.executable, "-m", "piper", "-m", str(model), "-f", wav_path, "--", text]
        engine = f"piper ({voice})"
    else:
        cmd = ["espeak-ng", "-v", "en-us", "-s", "140", "-w", wav_path, text]
        engine = "espeak-ng"
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"{engine} failed: {result.stderr.strip()[-300:]}")
    return engine


def play(wav_path):
    result = subprocess.run(["paplay", wav_path], capture_output=True, text=True,
                            env=audio_env(), timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"paplay failed: {result.stderr.strip()}")


# ---------- app ----------

@asynccontextmanager
async def lifespan(app):
    global robot, robot_info
    robot = BoosterRobot(timeout=30)
    robot_info = read(lambda: robot.robot_info)
    yield


app = FastAPI(title="k1b", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/status")
def status():
    global robot_info
    if not isinstance(robot_info, dict) or not robot_info or "error" in robot_info:
        robot_info = read(lambda: robot.robot_info)  # retry until it works
    battery = read(robot.get_battery)
    joints = read(robot.get_joint_states)
    return {
        "ts": time.time(),
        "battery": battery,
        "battery_percent": battery_percent(battery),
        "mode": read(robot.get_mode),
        "info": robot_info,
        "temperatures": {
            "system": system_temperatures(),
            "battery": battery_temperature(battery),
            "motors": motor_temperatures(joints),
        },
    }


@app.get("/api/debug/joints")
def debug_joints():
    """Raw joint state, to see where (if anywhere) motor temperatures live."""
    return read(robot.get_joint_states)


@app.get("/api/debug/raw")
def debug_raw():
    """What the SDK actually returns, for fixing field names."""
    def raw(fn):
        try:
            with sdk_lock:
                value = fn()
            return {"type": type(value).__name__, "repr": repr(value)[:2000],
                    "converted": to_jsonable(value)}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
    return {
        "robot_info": raw(lambda: robot.robot_info),
        "battery": raw(robot.get_battery),
        "mode": raw(robot.get_mode),
    }


@app.get("/api/voices")
def voices():
    return {"voices": available_voices(), "default": DEFAULT_VOICE}


class SayRequest(BaseModel):
    text: str
    voice: str = DEFAULT_VOICE


@app.post("/api/say")
def say(req: SayRequest):
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "Text is empty")
    if len(text) > MAX_TEXT:
        raise HTTPException(400, f"Text is longer than {MAX_TEXT} characters")

    if not speech_lock.acquire(blocking=False):
        raise HTTPException(409, "The robot is already speaking")
    try:
        start = time.time()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = f.name
        try:
            engine = synthesize(text, req.voice, wav_path)
            play(wav_path)
        finally:
            os.unlink(wav_path)
        return {"ok": True, "engine": engine, "seconds": round(time.time() - start, 2)}
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    finally:
        speech_lock.release()