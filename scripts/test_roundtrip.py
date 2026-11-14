import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from engine.engine import VoiceEngine

engine = VoiceEngine(config)

ref = Path("voices/reference/friend.wav")
if not ref.exists():
    raise SystemExit(f"Missing reference clip: {ref}")

print("load_voice('friend')...")
t0 = time.time()
engine.load_voice("friend", ref)
print(f"  {time.time() - t0:.2f}s")

Path("outputs").mkdir(exist_ok=True)

for i, line in enumerate(["First line.", "Second line."], start=1):
    t0 = time.time()
    wav = engine.speak(line, "friend")
    dt = time.time() - t0
    out = Path(f"outputs/roundtrip{i}.wav")
    out.write_bytes(wav)
    print(f"  speak #{i}: {dt:.2f}s -> {out} ({len(wav):,} bytes)")
