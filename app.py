"""
k1b: web interface for the Booster K1.

Runs ON THE ROBOT. Start it with:
    cd ~/Workspace/k1b && source .venv/bin/activate
    uvicorn app:app --host 0.0.0.0 --port 8000

Then open http://192.168.0.73:8000 in any browser on the same network.

Features (v1):
    - Live status: battery, mode, robot info
    - Speak: type text, the robot says it (Piper voice, espeak-ng fallback)
    - Play audio: upload a sound file, the robot plays it
    - Volume: set the speaker level from the page

No motion commands in this version.
"""
import dataclasses
import enum
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from boosteros.robots.booster import BoosterRobot

BASE_DIR = Path(__file__).resolve().parent
VOICES_DIR = BASE_DIR / "voices"
DEFAULT_VOICE = "en_US-lessac-medium"
MAX_TEXT = 300
MAX_UPLOAD_MB = 50
# paplay reads these directly. Anything else (mp3, m4a, ...) needs ffmpeg.
NATIVE_AUDIO = {".wav", ".flac", ".ogg", ".oga"}

# Only one BoosterRobot instance may exist per robot (Booster SDK rule).
robot = None
robot_info = {}

sdk_lock = threading.Lock()      # serialises SDK calls
audio_lock = threading.Lock()    # guards `player`
player = None                    # what the speaker is doing right now, or None


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


def remove(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def speaker_busy():
    return player is not None and player["proc"].poll() is None


def start_playback(path, label, kind):
    """
    Start paplay in the background and return the process.
    Only one sound plays at a time: speech and uploaded files share the speaker.
    `path` is deleted once playback ends or is stopped.
    """
    global player
    with audio_lock:
        if speaker_busy():
            remove(path)
            raise HTTPException(409, f"The robot is already playing: {player['label']}")
        proc = subprocess.Popen(["paplay", path], stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, env=audio_env())
        player = {"proc": proc, "label": label, "kind": kind, "started": time.time()}

    def cleanup():
        proc.wait()
        remove(path)

    threading.Thread(target=cleanup, daemon=True).start()
    return proc


def stop_playback():
    """Stop whatever is playing. Returns the label of what was stopped, or None."""
    with audio_lock:
        if not speaker_busy():
            return None
        player["proc"].terminate()
        try:
            player["proc"].wait(timeout=2)
        except subprocess.TimeoutExpired:
            player["proc"].kill()
        return player["label"]


def audio_state():
    if not speaker_busy():
        return {"playing": False}
    return {"playing": True, "label": player["label"], "kind": player["kind"],
            "elapsed": round(time.time() - player["started"], 1)}


def pactl(*args):
    result = subprocess.run(["pactl", *args], capture_output=True, text=True,
                            env=audio_env(), timeout=5)
    if result.returncode != 0:
        raise HTTPException(500, f"pactl failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def get_volume():
    """Speaker volume in percent, read from the default PulseAudio output."""
    out = pactl("get-sink-volume", "@DEFAULT_SINK@")
    # e.g. "Volume: front-left: 32768 /  50% / -18.06 dB,   front-right: ..."
    levels = [int(p.strip().rstrip("%")) for p in out.split("/") if p.strip().endswith("%")]
    if not levels:
        raise HTTPException(500, f"Couldn't read the volume from: {out.strip()[:200]}")
    return round(sum(levels) / len(levels))


def set_volume(percent):
    pactl("set-sink-mute", "@DEFAULT_SINK@", "0")
    pactl("set-sink-volume", "@DEFAULT_SINK@", f"{percent}%")


def to_wav(src, suffix):
    """
    Return a path paplay can play. Files paplay understands are used as they
    are; anything else is converted with ffmpeg. The caller deletes both paths.
    """
    if suffix in NATIVE_AUDIO:
        return src
    if not shutil.which("ffmpeg"):
        raise HTTPException(415, f"Can't play {suffix or 'this'} files without ffmpeg. "
                                 "Upload WAV, FLAC or OGG, or install ffmpeg on the robot.")
    dst = src + ".wav"
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src,
         "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", dst],
        capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        remove(dst)
        raise HTTPException(400, "Couldn't read that file as audio: "
                                 + (result.stderr.strip()[-300:] or "unknown error"))
    return dst


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
        "audio": audio_state(),
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

    if speaker_busy():  # fail fast, before spending time on synthesis
        raise HTTPException(409, f"The robot is already playing: {player['label']}")

    start = time.time()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
    try:
        engine = synthesize(text, req.voice, wav_path)
    except RuntimeError as e:
        remove(wav_path)
        raise HTTPException(500, str(e))

    # Speech is short, so wait for it to finish before replying.
    proc = start_playback(wav_path, f'"{text[:40]}"', "speech")
    proc.wait()
    if proc.returncode > 0:  # negative means it was stopped on purpose
        raise HTTPException(500, f"paplay failed: {proc.stderr.read().strip()}")
    return {"ok": True, "engine": engine, "seconds": round(time.time() - start, 2)}


@app.post("/api/play")
def play_file(file: UploadFile = File(...)):
    """
    Upload an audio file and play it on the robot's speaker.
    Returns as soon as playback starts. Use /api/stop to cut it off.
    """
    if speaker_busy():
        raise HTTPException(409, f"The robot is already playing: {player['label']}")

    name = Path(file.filename or "upload").name
    suffix = Path(name).suffix.lower()
    limit = MAX_UPLOAD_MB * 1024 * 1024

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        upload_path = f.name
        size = 0
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                f.close()
                remove(upload_path)
                raise HTTPException(413, f"File is larger than {MAX_UPLOAD_MB} MB")
            f.write(chunk)
    if size == 0:
        remove(upload_path)
        raise HTTPException(400, "File is empty")

    try:
        path = to_wav(upload_path, suffix)
    except HTTPException:
        remove(upload_path)
        raise
    if path != upload_path:
        remove(upload_path)

    proc = start_playback(path, name, "file")
    time.sleep(0.3)  # catch files paplay rejects straight away
    if proc.poll() not in (None, 0):
        raise HTTPException(400, f"paplay couldn't play {name}: {proc.stderr.read().strip()}")
    return {"ok": True, "playing": name, "size_mb": round(size / 1024 / 1024, 2)}


@app.post("/api/stop")
def stop():
    """Stop whatever the speaker is playing (speech or a file)."""
    stopped = stop_playback()
    return {"ok": True, "stopped": stopped}


@app.get("/api/volume")
def volume():
    return {"volume": get_volume()}


class VolumeRequest(BaseModel):
    volume: int = Field(ge=0, le=100)


@app.post("/api/volume")
def change_volume(req: VolumeRequest):
    """
    Set the speaker volume (0-100). This is the system output level, so it
    applies to speech and files alike and changes a sound that's already playing.
    """
    set_volume(req.volume)
    return {"ok": True, "volume": get_volume()}