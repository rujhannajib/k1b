import audioop
import subprocess

from boosteros.brain import Speech
from boosteros.robots.booster import BoosterRobot

robot = BoosterRobot(timeout=30)
speech = Speech(robot)

tests = [
    ("default", "Hello, I am Booster K1."),
    ("vv_uranus", "Hello, I am Booster K1."),
    ("ruya_yichen", "Hello, I am Booster K1."),
    ("vv_uranus", "你好，我是Booster机器人。"),
]

for voice, text in tests:
    try:
        audio = speech.synthesize(text, voice=voice).result()
        peak = audioop.max(bytes(audio.data), 2)
        print(f"{voice:12s} peak={peak:6d} {audio.duration.seconds:.2f}s  {text}")
        if peak > 0:
            path = f"/tmp/k1b_{voice}.wav"
            audio.save(path)
            subprocess.run(["paplay", path])
    except Exception as e:
        print(f"{voice:12s} FAILED: {type(e).__name__}: {e}")
