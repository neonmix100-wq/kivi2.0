import numpy as np, sounddevice as sd

DEV, SR = 24, 48000
print("Говори в микрофон и крути ручки. Ctrl+C - стоп.")
print("ЦЕЛЬ: PEAK 0.2-0.7 на обычной речи")
with sd.InputStream(samplerate=SR, channels=1, dtype='int16',
                    device=DEV, blocksize=int(SR*0.05)) as st:
    while True:
        d, _ = st.read(int(SR * 0.5))
        a = d[:, 0].astype(np.float32) / 32768.0
        print(f"PEAK {np.max(np.abs(a)):.3f}   RMS {np.sqrt(np.mean(a**2)):.4f}")