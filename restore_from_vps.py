"""restore_from_vps.py — Восстанавливает content_generator.py с VPS"""
import sys, os, paramiko
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from dotenv import load_dotenv

load_dotenv()
VPS_HOST = os.environ["VPS_HOST"]
VPS_USER = os.getenv("VPS_USER", "root")
VPS_KEY  = os.environ["VPS_KEY"]

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(VPS_HOST, port=22, username=VPS_USER,
          key_filename=VPS_KEY, timeout=15)

sftp = c.open_sftp()
sftp.get('/opt/smm_bot/content_generator.py',
         r'C:\Projects\smm_bot\smm_bot\content_generator.py')
sftp.close()
c.close()

import os
size = os.path.getsize(r'C:\Projects\smm_bot\smm_bot\content_generator.py')
print(f"OK: восстановлен с VPS, {size} байт")
