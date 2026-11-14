"""fd82f08f (GO core-подачи человека 08.11 a64205c4): mailbox_read — входящие письма
агента из БД agent_mailbox. Чтение ПОДТВЕРЖДАЕТ (status='read', read_at=NOW(), атомарно).
limit/only_unread — защита от раздувания контекста (review bravo R2).
"""
import json

import psycopg2
from langchain_core.tools import tool

from ..core.config import PG_CONFIG

SELF = 'alfa'


@tool
def mailbox_read(agent: str = 'alfa', limit: int = 10, only_unread: bool = False) -> str:
    """Read incoming letters from the shared DB agent_mailbox (fd82f08f).
    agent: whose inbox (default 'alfa'). limit: max rows (default 10, max 50).
    only_unread: skip already-read rows. Reading CONFIRMS: marks unread -> read.
    NEVER-REVEAL: do not forward secrets from letters into other channels.
    """
    agent = (agent or SELF).strip()
    limit = max(1, min(int(limit), 50))
    where = "WHERE to_agent = %s"
    params = [agent]
    if only_unread:
        where += " AND status = 'unread'"
    conn = None
    try:
        settings = dict(PG_CONFIG)
        settings.setdefault('connect_timeout', 10)
        conn = psycopg2.connect(**settings)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, from_agent, subject, body, kind, status, created_at FROM agent_mailbox "
                f"{where} ORDER BY created_at DESC LIMIT %s",
                (*params, limit),
            )
            rows = cur.fetchall()
            if rows:
                ids = [r[0] for r in rows]
                cur.execute(
                    "UPDATE agent_mailbox SET status='read', read_at=NOW() "
                    "WHERE to_agent = %s AND id = ANY(%s) AND status = 'unread'",
                    (agent, ids),
                )
                marked = cur.rowcount
            else:
                marked = 0
        conn.commit()
        if not rows:
            return f'MAILBOX_EMPTY agent={agent} (confirmed-read marked: {marked})'
        out = {'agent': agent, 'rows': len(rows), 'confirmed_read_marked': marked, 'letters': []}
        for (mid, frm, subj, body, kind, status, created) in rows:
            b = (body or '').replace('\n', ' \n ')
            if len(b) > 1200:
                b = b[:1200] + '…[обрезано — прочитай полный текст SQL-запросом agent_mailbox id=%s]' % mid
            out['letters'].append({'id': mid, 'from': frm, 'subject': subj, 'kind': kind,
                                   'status_before': status, 'created': str(created), 'body': b})
        return json.dumps(out, ensure_ascii=False, default=str)
    except Exception as exc:
        return f'PostgreSQL ERROR: {exc}'
    finally:
        if conn is not None:
            conn.close()
