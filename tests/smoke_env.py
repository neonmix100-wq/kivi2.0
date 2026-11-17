import sys, platform, psutil, sounddevice as sd
ok = True
def bad(m):
    global ok; ok = False; print("  X", m)

print(f"Python {sys.version.split()[0]} {platform.architecture()[0]}")
if sys.version_info[:2] not in ((3, 11), (3, 12)): bad("нужен Python 3.11 или 3.12")

try:
    import pynvml; pynvml.nvmlInit()
    h = pynvml.nvmlDeviceGetHandleByIndex(0)
    m = pynvml.nvmlDeviceGetMemoryInfo(h)
    print(f"GPU: {pynvml.nvmlDeviceGetName(h)} | VRAM {m.total/2**30:.1f} ГБ, свободно {m.free/2**30:.1f}")
    if m.total / 2**30 < 15: bad("ожидалось ~16 ГБ VRAM")
except Exception as e:
    bad(f"GPU не виден: {e}")

print(f"RAM: {psutil.virtual_memory().total/2**30:.1f} ГБ")
print("\nХост-API звука:")
for i, a in enumerate(sd.query_hostapis()): print(f"  [{i}] {a['name']}")
print("\nУстройства — выпиши номер Fifine и номер наушников:")
for i, d in enumerate(sd.query_devices()):
    kind = ("IN " if d['max_input_channels'] else "   ") + ("OUT" if d['max_output_channels'] else "")
    print(f"  [{i:2}] {kind}  {d['name']}")

print("\n" + ("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат"))