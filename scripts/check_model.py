import time
from chatterbox.mtl_tts import ChatterboxMultilingualTTS

print("Loading Chatterbox Multilingual...")
start = time.time()
model = ChatterboxMultilingualTTS.from_pretrained(device="cuda")
print(f"Loaded in {time.time() - start:.1f}s")
