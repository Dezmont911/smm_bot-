import subprocess, sys

GIT = r"C:\Program Files\Git\cmd\git.exe"
DIR = r"C:\Projects\smm_bot\smm_bot"

def run(args):
    r = subprocess.run([GIT]+args, cwd=DIR, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if out: print(out)
    return r.returncode == 0

run(["add", "."])
run(["status", "--short"])
run(["commit", "-m", "feat: add web_scraper, image_fetcher, git setup"])
run(["push"])
print("Done")
