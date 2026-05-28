import sys
import paramiko
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

def run(client, cmd, timeout=60):
    stdin, stdout, stderr = client.exec_command(f"bash -lc '{cmd}'", timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return out + err

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("77.233.215.77", port=22, username="root",
               key_filename=r"C:\Users\Admin\.ssh\smm_bot", timeout=15)
print("Connected!\n")

VENV_PY = "/opt/smm_bot/venv/bin/python"

print("=== Деплой: WB-фикс + /review перезапись ===\n")

# 1. git pull
print("[1] git pull")
print(run(client, 'cd /opt/smm_bot && git pull origin main 2>&1', timeout=20))

# 2. Проверяем что сломанный импорт убран
print("\n[2] Проверка: WB_CATEGORIES в wb_partner_parser.py:")
print(run(client, 'grep -n "WB_CATEGORIES\|_categories_count" /opt/smm_bot/wb_partner_parser.py || echo "OK: нет сломанных импортов"'))

# 3. Проверяем новые обработчики /review в bot.py
print("\n[3] Проверка: handle_review_channel_select в bot.py:")
print(run(client, 'grep -n "handle_review_channel_select\|handle_review_next_page\|review_ch:\|review_page:" /opt/smm_bot/bot.py | head -10'))

# 4. Тест синтаксиса Python для всех изменённых файлов
print("\n[4] Синтаксис-чек:")
for f in ["bot.py", "wb_partner_parser.py", "content_generator.py"]:
    result = run(client, f'cd /opt/smm_bot && {VENV_PY} -m py_compile {f} && echo "OK: {f}" || echo "ERROR: {f}"')
    print(result.strip())

# 5. Перезапуск бота
print("\n[5] Перезапуск бота:")
print(run(client, 'systemctl restart smm_bot && sleep 3 && systemctl is-active smm_bot'))

# 6. Последние 10 строк логов
print("\n[6] Логи после рестарта:")
print(run(client, 'journalctl -u smm_bot -n 10 --no-pager'))

client.close()
print("\nДеплой завершён.")
