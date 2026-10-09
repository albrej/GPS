import re, sys
chemin = sys.argv[1] if len(sys.argv) > 1 else "main.py"
d = open(chemin, "rb").read()
for _ in range(4):  # certaines lignes étaient encodées 2x voire 3x
    d2 = re.sub(b"\xc3\xa2\xc2\x80\xc2", b"\xe2\x80", d)
    d2 = d2.replace(b"\xc3\x83\xc2", b"\xc3")
    d2 = d2.replace(b"\xc3\x82\xc2", b"\xc2")
    d2 = re.sub(b"\xc3\xa2\xc2(.)\xc2(.)", lambda m: b"\xe2" + m.group(1) + m.group(2), d2)
    if d2 == d:
        break
    d = d2
open(chemin, "wb").write(d)
print("CORROMPU" if b"\xc3\x83\xc2" in d or b"\xc3\xa2\xc2" in d else "REPARTE : OK")