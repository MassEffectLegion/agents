"""Standard-library-only recovery console. Works without LangChain, PostgreSQL, or llama."""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from omega.core.instance_lock import InstanceLock

ROOT=Path(__file__).resolve().parent

def database_path():
    config_path=Path(os.environ.get('OMEGA_CONFIG',str(ROOT/'config.json')))
    config=json.loads(config_path.read_text(encoding='utf-8'))
    return (ROOT/config['sqlite_path']).resolve()

def integrity(path):
    conn=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        return [row[0] for row in conn.execute('PRAGMA integrity_check')]
    finally:
        conn.close()

def online_backup(source,destination):
    source,destination=Path(source).resolve(),Path(destination).resolve()
    if source==destination or destination.exists():
        raise ValueError('Destination must be a new file')
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_name(destination.name+'.tmp-'+uuid.uuid4().hex)
    src=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    try:
        dst=sqlite3.connect(str(temporary))
        try:
            src.backup(dst)
        finally:
            dst.close()
        if integrity(temporary)!=['ok']:
            raise RuntimeError('Backup failed integrity check')
        temporary.replace(destination)
    finally:
        src.close()
        temporary.unlink(missing_ok=True)
    return str(destination)

def restore(source,database):
    source,database=Path(source).resolve(),Path(database).resolve()
    if source==database:
        raise ValueError('Backup and target must differ')
    # Validate and stage a coherent SQLite backup BEFORE touching the current database.
    if integrity(source)!=['ok']:
        raise ValueError('Source backup is corrupt')
    probe=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    try:
        required={'tasks','messages','checkpoints','human_requests','agent_settings'}
        present={r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required<=present:
            raise ValueError('Source is not an omega memory database')
    finally:
        probe.close()
    with InstanceLock(str(database)+'.lock'):
        stage=database.with_name(database.name+'.restore-'+uuid.uuid4().hex)
        online_backup(source,stage)
        preserved=database.parent/'before_restore'/uuid.uuid4().hex
        preserved.mkdir(parents=True,exist_ok=True)
        moved=[]
        try:
            for original in (database,Path(str(database)+'-wal'),Path(str(database)+'-shm')):
                if original.exists():
                    saved=preserved/original.name
                    original.replace(saved)
                    moved.append((original,saved))
            stage.replace(database)
        except BaseException:
            for original,saved in reversed(moved):
                if not original.exists():
                    saved.replace(original)
            raise
        finally:
            stage.unlink(missing_ok=True)
    return {'restored':str(database),'previous_files':str(preserved)}

def manifest_check(directory):
    directory=Path(directory)
    manifest=directory/'SHA256SUMS.json'
    hashes=json.loads(manifest.read_text(encoding='utf-8'))
    return {name:hashlib.sha256((directory/name).read_bytes()).hexdigest()==expected for name,expected in hashes.items()}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('check')
    backup=commands.add_parser('backup');backup.add_argument('destination')
    restore_cmd=commands.add_parser('restore');restore_cmd.add_argument('source')
    export=commands.add_parser('export');export.add_argument('destination')
    commands.add_parser('verify-offline')
    commands.add_parser('init')
    args=parser.parse_args()
    path=database_path()
    if args.command=='check':
        result={'python':sys.version,'executable':sys.executable,'database':str(path),
                'integrity':integrity(path) if path.exists() else ['not initialized'],
                'file_checkpoint':str(ROOT/'data/latest_checkpoint.json'),
                'postgres_required':False,'llama_required':False}
        print(json.dumps(result,ensure_ascii=False,indent=2))
    elif args.command=='backup':
        print(online_backup(path,args.destination))
    elif args.command=='restore':
        print(json.dumps(restore(args.source,path),ensure_ascii=False,indent=2))
    elif args.command=='init':
        from omega.core.storage import SQLiteStore
        SQLiteStore(path)
        print('SQLite memory ready: '+str(path))
    elif args.command=='export':
        destination=Path(args.destination)
        if destination.exists():
            raise ValueError('Export destination already exists')
        conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        try:
            with destination.open('x',encoding='utf-8') as handle:
                for statement in conn.iterdump():
                    handle.write(statement+'\n')
        finally:
            conn.close()
        print(destination)
    elif args.command=='verify-offline':
        results=manifest_check(ROOT/'offline/wheels')
        print(json.dumps(results,ensure_ascii=False,indent=2))
        if not results or not all(results.values()):
            raise SystemExit(1)

if __name__=='__main__':
    main()
