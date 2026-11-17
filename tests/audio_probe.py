import sys, numpy as np, sounddevice as sd, soundfile as sf

def list_devices():
    apis = sd.query_hostapis()
    din, dout = sd.default.device
    print(f"{'idx':>4} {'API':<22} {'I/O':<4} {'ch':>3} {'Hz':>7}  name")
    for i, d in enumerate(sd.query_devices()):
        api = apis[d['hostapi']]['name']
        io = ("IN" if d['max_input_channels'] else "") + ("OUT" if d['max_output_channels'] else "")
        ch = max(d['max_input_channels'], d['max_output_channels'])
        mark = ""
        if i == din: mark += "  <-DEFAULT IN"
        if i == dout: mark += "  <-DEFAULT OUT"
        print(f"{i:>4} {api:<22} {io:<4} {ch:>3} {d['default_samplerate']:>7.0f}  {d['name']}{mark}")

def record(idx, seconds=5):
    d = sd.query_devices(idx)
    sr = int(d['default_samplerate'])
    print(f"[{idx}] {d['name']} @ {sr} Hz")
    print(">>> ГОВОРИ СЕЙЧАС 5 СЕКУНД <<<")
    try:
        a = sd.rec(int(seconds * sr), samplerate=sr, channels=1,
                   dtype='float32', device=idx)
        sd.wait()
    except Exception as e:
        print("ОШИБКА:", e); return
    rms = float(np.sqrt(np.mean(a ** 2)))
    peak = float(np.max(np.abs(a)))
    path = f"data/mic_{idx}.wav"
    sf.write(path, a, sr)
    print(f"Сохранено: {path} | RMS {rms:.4f} | PEAK {peak:.4f}")
    if peak < 0.01:
        print("!! ПОЧТИ ТИШИНА — не тот микрофон или он заглушен")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        record(int(sys.argv[1]))
    else:
        list_devices()