import sys, os
import paramiko
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from dotenv import load_dotenv

load_dotenv()
VPS_HOST = os.environ["VPS_HOST"]
VPS_USER = os.getenv("VPS_USER", "root")
VPS_KEY  = os.environ["VPS_KEY"]

def run(client, cmd, timeout=60):
    stdin, stdout, stderr = client.exec_command(f"bash -lc '{cmd}'", timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return out + err

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(VPS_HOST, port=22, username=VPS_USER,
               key_filename=VPS_KEY, timeout=15)
print("Connected!\n")

VENV_PY = "/opt/smm_bot/venv/bin/python"

print("=== Статус сервиса ===")
print(run(client, "systemctl status smm_bot --no-pager | head -8"))

print("\n=== Последние 40 строк логов ===")
print(run(client, "journalctl -u smm_bot -n 40 --no-pager"))

print("\n=== Ошибки за последние 10 минут ===")
errors = run(client, 'journalctl -u smm_bot --since "10 minutes ago" --no-pager | grep -iE "error|traceback|exception|failed|ImportError" | head -20')
print(errors if errors.strip() else "  (нет ошибок)")

client.close()
print("\nDone.")
