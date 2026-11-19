import os, sysconfig, time
from pathlib import Path
import numpy as np
import soundfile as sf

sp = Path(sysconfig.get_paths()["purelib"]) / "nvidia"
print("nvidia dir:", sp, "exists:", sp.exists())
if sp.exists():
    for pat in ("cublas64_12.dll", "cudnn_ops64_9.dll", "cudnn64_9.dll",
                "cudart64_12.dll", "nvrtc64*.dll"):
        for hit in sp.rglob(pat):
            d = str(hit.parent)
            os.add_dll_directory(d)
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            print("registered:", d)

audio, sr = sf.read("data/last_utterance.wav", dtype="float32")
if audio.ndim > 1:
    audio = audio.mean(axis=1)
print(f"audio: {len(audio)/sr:.2f}s @ {sr} Hz")

from faster_whisper import WhisperModel

t0 = time.time()
m = WhisperModel("large-v3-turbo", device="cuda", compute_type="int8_float16",
                 download_root="data/models")
print(f"model ready {time.time()-t0:.2f}s")

t0 = time.time()
segs, info = m.transcribe(audio, language="ru", beam_size=1)
text = "".join(s.text for s in segs).strip()
print("TEXT:", text)
print(f"TRANSCRIBE {time.time()-t0:.2f}s")
print("OK CUDA")