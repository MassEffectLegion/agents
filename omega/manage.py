"""Local operator console: SQLite requests, status, settings, health and backups."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from omega.core.config import AGENT_ROOT, CONFIG_PATH, SQLITE_OVERRIDES, get_runtime_settings, validate
from omega.core.storage import SQLiteStore

def main():
    parser=argparse.ArgumentParser(description='Omega local management; no model generation')
    commands=parser.add_subparsers(dest='command',required=True)
    ask=commands.add_parser('ask'); ask.add_argument('request')
    commands.add_parser('status')
    commands.add_parser('check')
    backup=commands.add_parser('backup'); backup.add_argument('destination')
    setting=commands.add_parser('set'); setting.add_argument('key'); setting.add_argument('json_value')
    args=parser.parse_args()
    store=SQLiteStore()
    if args.command=='ask':
        print(store.enqueue(args.request))
    elif args.command=='status':
        print(json.dumps(store.query('SELECT request_id,status,request,response FROM human_requests ORDER BY created_at DESC LIMIT 20'),ensure_ascii=False,indent=2))
        print(json.dumps(store.query('SELECT job_id,pid,status,log_path FROM process_jobs ORDER BY started_at DESC LIMIT 10'),ensure_ascii=False,indent=2))
    elif args.command=='check':
        health=store.query('PRAGMA integrity_check')['rows']
        settings=get_runtime_settings()
        print(json.dumps({'sqlite':str(store.path),'integrity':health,'config':str(CONFIG_PATH),
            'enabled_models':[s['name'] for s in settings['servers'] if s['enabled']],
            'postgres_required':False},ensure_ascii=False,indent=2))
        if health != [['ok']]:
            raise SystemExit(1)
    elif args.command=='backup':
        print(store.backup(args.destination))
    elif args.command=='set':
        if args.key not in SQLITE_OVERRIDES:
            parser.error('This key must be configured in config.json')
        value=json.loads(args.json_value)
        validate({**get_runtime_settings(),**store.settings(),args.key:value})
        store.set_setting(args.key,value)
        print('Saved SQLite override: '+args.key)

if __name__=='__main__':
    main()
