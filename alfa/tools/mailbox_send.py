"""fd82f08f (GO core-подачи человека 08.11 a64205c4): mailbox_send — письмо напарнику в БД
agent_mailbox. Атомарно, unread; статусы: unread -> read (mailbox_read подтверждает).
НЕ содержит секретов (NEVER-REVEAL): body — только факты/пути/статусы.
"""
import json

import psycopg2
from langchain_core.tools import tool

from ..core.config import PG_CONFIG

SELF = 'alfa'


@tool
def mailbox_send(to_agent: str, subject: str, body: str, kind: str = 'note', refs: str = '{}') -> str:
    """Send a letter to a peer agent in the shared DB agent_mailbox (fd82f08f).
    to_agent: 'bravo'|'gemma'|'delta1'|'delta2'|'delta3'. kind: 'note'|'go'|'review'|'alert'.
    refs: optional JSON string of context (task_id, hr, file paths). Returns the row id.
    NEVER-REVEAL: never put passwords/tokens/keys/internal IP+port pairs in body.
    """
    if not to_agent or not subject:
        return 'ERROR: to_agent and subject are required'
    if kind not in ('note', 'go', 'review', 'alert'):
        kind = 'note'
    try:
        refs_json = json.loads(refs) if refs and refs.strip() else {}
    except Exception:
        refs_json = {'raw': refs[:200]}
    conn = None
    try:
        settings = dict(PG_CONFIG)
        settings.setdefault('connect_timeout', 10)
        conn = psycopg2.connect(**settings)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_mailbox (from_agent, to_agent, subject, body, kind, refs, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'unread') RETURNING id",
                (SELF, to_agent, subject, body, kind, json.dumps(refs_json, ensure_ascii=False)),
            )
            mid = cur.fetchone()[0]
        conn.commit()
        return f'MAILBOX_SENT id={mid} from={SELF} to={to_agent} subject={subject!r}'
    except Exception as exc:
        return f'PostgreSQL ERROR: {exc}'
    finally:
        if conn is not None:
            conn.close()
