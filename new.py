import toupcam

cams = toupcam.Toupcam.EnumV2()
if not cams:
    raise RuntimeError("No camera detected")

cam = toupcam.Toupcam.Open(cams[0].id)

count = cam.ResolutionNumber()
print("Supported resolutions:")

for i in range(count):
    w, h = cam.get_Resolution(i)
    print(f"{i}: {w} x {h}")

cam.Close()


