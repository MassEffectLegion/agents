"""Инструмент tg_set_role — установка контекстной роли для TG-обработки (b05f205e S3).



Перед обработкой сообщения из TG-очереди вызывается с ролью чата (из

agent_settings.tg_bots.chats). guest/project включают read-only замок

(core/tg_readonly_guard): write-инструменты и SQL-запись блокируются

на уровне инструмента. После обработки — role=None (возврат к полному доступу).

"""

from delta2.core import tg_readonly_guard as guard





def tg_set_role(role):

    """Set TG context role before handling a tg_message_queue row; reset to None after.



    role: 'owner' (full), 'project' (read-only), 'guest' (read-only, recommendation-only),

    or None to reset to full access. Must be called for each TG message with the chat role

    from agent_settings.tg_bots.chats, then reset to None when done.

    """

    guard.set_role(role)

    return f"tg_set_role: role={role!r} -> readonly={guard.is_readonly()}"

