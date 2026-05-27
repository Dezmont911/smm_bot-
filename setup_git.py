"""Инициализация git и первый пуш на GitHub"""
import subprocess
import os
import sys

GIT = r"C:\Program Files\Git\cmd\git.exe"
REPO_DIR = r"C:\Projects\smm_bot\smm_bot"
REMOTE = "https://github.com/Dezmont911/smm_bot-.git"


def run(args, **kwargs):
    cmd = [GIT] + args
    print(f"> git {' '.join(args)}")
    result = subprocess.run(cmd, cwd=REPO_DIR, capture_output=True, text=True, **kwargs)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    return result.returncode == 0


os.chdir(REPO_DIR)

print("=== Инициализация git репозитория ===\n")

run(["init"])
run(["config", "user.email", "dezmont911@gmail.com"])
run(["config", "user.name", "Dezmont911"])

print("\n=== Добавляем файлы ===\n")
run(["add", "."])
run(["status", "--short"])

print("\n=== Первый коммит ===\n")
run(["commit", "-m", "feat: initial commit — Content Factory Bot v1.0"])

print("\n=== Привязываем GitHub ===\n")
run(["branch", "-M", "main"])
run(["remote", "add", "origin", REMOTE])

print("\n=== Пуш на GitHub ===\n")
print("Внимание: GitHub попросит авторизацию в браузере.")
ok = run(["push", "-u", "origin", "main"])

if ok:
    print("\n✅ Готово! Код на GitHub: https://github.com/Dezmont911/smm_bot-")
else:
    print("\n⚠️  Пуш не удался — возможно нужна авторизация.")
    print("Запусти вручную в Git Bash:")
    print(f"  cd {REPO_DIR}")
    print(f"  git push -u origin main")
