"""Single source for actually available tools; infinite agents do not expose finish_task."""
from importlib import import_module
from langchain_core.tools import BaseTool, tool
from .logged_tool import _LoggedTool, logged_tool
from . import tg_readonly_guard  # noqa: F401  (S3: TG read-only lock)

TOOL_NAMES = (
    'write_local_file', 'list_files_in_directory', 'read_local_file_chunked', 'save_progress',
    'run_python_script', 'rag_search', 'list_postgres_tables', 'get_table_schema', 'query_postgres_local',
    'embed_and_store', 'bulk_insert_to_postgres', 'execute_ssh_command', 'execute_ssh_command_home',  # f7d01ae1 (GO человека 07.11)
    'ssh_execute_on_all_servers', 'download_file_from_server', 'upload_file_to_server',
    'tg_set_role','log_chunk_analyzer','batch_file_analyzer',
    'sleep',          # fe455999 (GO 07.11): инструмент спячки
    'request_restart',   # 85c9e66a: controlled restart (human GO d7581a4c)
    'mailbox_send',   # fd82f08f (GO человека 08.11 a64205c4): письмо напарнику (БД agent_mailbox)
    'mailbox_read',   # fd82f08f: входящие письма (подтверждение чтения)
)
TOOLS_REGISTRY = {name: name for name in TOOL_NAMES}

def get_tools():
    result = []
    for name in TOOL_NAMES:
        function = getattr(import_module(f'alfa.tools.{name}'), name)
        result.append(logged_tool(function if isinstance(function, BaseTool) else tool(function)))
    return result
