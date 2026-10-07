# k1b

A web interface for the **Booster K1** humanoid robot. It runs on the robot and is opened from any browser on the same network.

Current version (v1) shows live robot status and makes the robot speak. It sends **no motion commands**.

| Feature | Status |
|---|---|
| Battery level, mode, serial, edition, firmware | Working |
| Temperatures (computer, battery, motors if reported) | Working, colour-coded |
| Text to speech (Piper or espeak-ng) | Working |
| Head movement, walking | Not started |

---

## How it works

```
Browser (laptop / phone)
        │  HTTP, port 8000
        ▼
app.py  (FastAPI, runs ON the robot)
        ├── boosteros SDK  ──►  battery, mode, robot info, joints
        ├── /sys/class/thermal  ──►  Jetson temperatures
        └── Piper / espeak-ng  ──►  WAV  ──►  paplay  ──►  USB speaker
```

The Booster SDK (`boosteros`) only runs on the robot itself or in Booster Studio's simulator, so the server must run on the K1. The SDK allows only one `BoosterRobot` instance per robot; `app.py` creates it once at startup and every request goes through it.

---

## Robot facts

| Item | Value |
|---|---|
| Model | Booster K1, K1 Education edition, single-board (NVIDIA Jetson Orin) |
| Firmware | **v1.8.0.9** (required: `boosteros` needs v1.7 or newer) |
| OS | Ubuntu 22.04, ROS 2 Humble, Python 3.10 |
| Login | `booster@<robot-ip>` |
| Project path | `/home/booster/Workspace/k1b` |
| Speaker | USB C-Media sound card (default PulseAudio output) |
| Microphone | iFlytek XFM-DP USB array |

Check the firmware with `cat /opt/booster/version.txt`.

### Finding the robot's IP address

Open the **Booster App** on a phone connected to the robot; it shows the robot's current IP address. The K1 gets its address from the Wi-Fi router, so it can change after a reboot. If you can't connect, check the app first and update the IP in `~/.ssh/config` and in the browser URL. This README uses `192.168.0.73` as an example.

To stop the address changing, ask whoever manages the router to set a DHCP reservation for the robot.

---

## Project layout

```
k1b/
├── app.py              # FastAPI server (status, speech, debug endpoints)
├── static/
│   └── index.html      # The web interface
├── requirements.txt
├── sync.sh             # Push code from laptop to robot
├── voices/             # Piper voice models (on the robot only, not synced)
└── .venv/              # Python environment (on the robot only, not synced)
```

---

## Setup

### 1. On the robot (once)

SSH in, then:

```bash
cd /home/booster/Workspace/k1b
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
pip install -r requirements.txt
sudo apt install -y espeak-ng
```

`--system-site-packages` is required so the venv can see Booster's preinstalled `boosteros` SDK and the ROS 2 libraries.

Download Piper voices (about 60 MB each):

```bash
mkdir -p voices && cd voices
python3 -m piper.download_voices en_US-lessac-medium
python3 -m piper.download_voices en_US-amy-medium
python3 -m piper.download_voices en_US-ryan-medium
python3 -m piper.download_voices en_GB-alba-medium
cd ..
```

Any `.onnx` model placed in `voices/` appears in the interface's voice dropdown automatically.

### 2. On the laptop (WSL, once)

Install tools and create an SSH key if you don't have one:

```bash
sudo apt install -y rsync openssh-client
ssh-keygen -t ed25519 -C "k1b-laptop"     # press Enter at every prompt
```

Add an SSH shortcut. Replace `192.168.0.73` with the IP shown in the Booster App:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
cat >> ~/.ssh/config << 'EOF'

Host k1b
    HostName 192.168.0.73
    User booster
EOF
chmod 600 ~/.ssh/config
ssh-copy-id k1b
```

Test with `ssh k1b "echo connected"`. It should not ask for a password.

Get the code onto the laptop and open it in VS Code (needs the **WSL** extension):

```bash
mkdir -p ~/k1b
rsync -av --exclude .venv --exclude __pycache__ --exclude voices \
  k1b:/home/booster/Workspace/k1b/ ~/k1b/
cd ~/k1b && chmod +x sync.sh && code .
```

---

## Daily workflow

Edit on the laptop, run on the robot. Use two terminals in VS Code:

**Terminal 1 (laptop):** push changes

```bash
./sync.sh
```

**Terminal 2 (robot):** run the server

```bash
ssh k1b
cd ~/Workspace/k1b && source .venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 8000
```

Wait for `Application startup complete`, then open **http://192.168.0.73:8000**.

After each sync, stop the server with Ctrl+C and start it again.

**Rules**

- Always edit on the laptop. `sync.sh` uses `--delete`, so a file changed only on the robot is overwritten on the next sync. If you edit on the robot, pull it back first with the `rsync` command from setup.
- `.venv`, `voices`, `.git` and `__pycache__` are never synced.

---

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Web interface |
| GET | `/api/status` | Battery, mode, robot info, temperatures |
| GET | `/api/voices` | Available voices |
| POST | `/api/say` | Speak text. Body: `{"text": "...", "voice": "en_US-lessac-medium"}` |
| GET | `/api/debug/raw` | Raw SDK output for robot info, battery, mode |
| GET | `/api/debug/joints` | Raw joint state (to find motor temperatures) |

`/api/say` limits text to 300 characters and returns `409` if the robot is already speaking.

---

## Temperature colours

Defined in `LIMITS` in `static/index.html` as `[warm, dangerous]` in °C:

| Source | Green below | Amber from | Red from |
|---|---|---|---|
| Computer (Jetson) | 70 | 70 | 85 |
| Battery | 45 | 45 | 55 |
| Motors | 60 | 60 | 75 |

These are rough defaults. Adjust them once the K1's normal operating range is known.

---

## Known issues

**Booster's own text-to-speech returns silence.** `Speech.synthesize()` produces audio of the right length but every sample is zero, for all voices and in English and Chinese. Likely a cloud or account issue behind Booster's LUI service (robot region is CN). This project uses Piper and espeak-ng instead.

**SDK audio playback times out.** `robot.audio_manager.play_stream()` returns `FAILED` with `RPC request timed out`. The interface plays audio with `paplay` directly, which works.

**NumPy version.** A NumPy 1.26 in `~/.local` conflicts with the system SciPy (which needs below 1.25). `requirements.txt` pins `numpy<1.25` inside the venv. Don't change the `~/.local` copy; Booster's face detection may depend on it.

**`booster-cli launch -c status` reports 400 for the perception machine.** Harmless. The K1 is single-board and the perception daemon doesn't support `status`.

**Motor temperatures** are not part of the SDK's documented joint state. The interface shows them only if they appear in the joint data's `extra` field. Check `/api/debug/joints` to see what's available.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Page won't load or `ssh k1b` fails | Is `uvicorn` running on the robot? Check the current IP in the Booster App; it may have changed |
| Battery shows `?` or info is blank | Open `/api/debug/raw` and look at the field names the SDK returns |
| `LocoClientInitError` on start | Robot still booting. Wait 30 to 60 seconds after power on |
| No sound | `paplay /usr/share/sounds/alsa/Front_Center.wav` should play. If not, check `pactl get-default-sink` is the USB device |
| Voice dropdown shows only `espeak` | No `.onnx` files in `~/Workspace/k1b/voices/` on the robot |
| `paplay: Connection refused` | `export XDG_RUNTIME_DIR=/run/user/1000` |

Robot service commands:

```bash
booster-cli launch -c restart -m perception   # restart camera/audio services
booster-cli log -st YYYYMMDD-HHMMSS -et YYYYMMDD-HHMMSS -o ~/Documents
```

---

## Safety

The K1 is a walking humanoid. Before adding any motion features:

- Test with the robot on its stand or seated, starting with head movement only.
- Always have someone at the remote controller (LT + BACK enters DAMP).
- Add a watchdog that stops the robot if the browser connection drops.
- Never walk the robot with the charging cable attached.