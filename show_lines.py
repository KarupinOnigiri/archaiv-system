with open("routes/assets.py", "r", encoding="utf-8") as f:
    lines = f.readlines()
for i, line in enumerate(lines, start=1):
    if 433 <= i <= 458:
        print(f"{i}: {line}", end="")
