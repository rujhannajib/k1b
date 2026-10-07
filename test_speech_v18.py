from boosteros.brain import Speech
from boosteros.robots.booster import BoosterRobot

robot = BoosterRobot(timeout=30)
am = robot.audio_manager

try:
    print("system volume:", am.get_system_volume())
except Exception as e:
    print("get_system_volume failed:", type(e).__name__, e)

speech = Speech(robot)
audio = speech.synthesize("Hello, I am Booster K1. Nice to meet you.").result()
print(f"synthesized {audio.duration.seconds:.2f}s, {audio.sample_rate} Hz, {audio.channels} ch")

stream = am.play_stream(
    sample_rate=audio.sample_rate,
    channels=audio.channels,
    sample_format=audio.sample_format,
    volume=1.0,
)
try:
    chunk = 4096
    for start in range(0, len(audio.data), chunk):
        stream.write(audio.with_data(audio.data[start:start + chunk]))
finally:
    stream.close()

print("playback:", stream.wait(timeout=audio.duration.seconds + 10.0))
