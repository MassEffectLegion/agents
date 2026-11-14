"""core/tools_registry.py — single source for tools."""
from importlib import import_module
from langchain_core.tools import BaseTool, tool
from .logged_tool import _LoggedTool, logged_tool

TOOL_NAMES = (
    'write_local_file', 'list_files_in_directory', 'read_local_file_chunked', 'save_progress',
    'run_python_script', 'rag_search', 'list_postgres_tables', 'get_table_schema', 'query_postgres_local',
    'db_api_query', 'db_api_health', 'db_api_write',  # c39a0bfa (01.10) reads; rest#29 (02.10) writes through db_service :8888
    'embed_and_store', 'bulk_insert_to_postgres', 'execute_ssh_command', 'execute_ssh_command_home',  # f7d01ae1 (GO человека 07.11)
    'ssh_execute_on_all_servers', 'download_file_from_server', 'upload_file_to_server',
    'ask_vision', 'web_search',  # 4c56f0a4: capabilities из agent_settings (vision, internet_search)
    'sleep',  # fe455999 (GO 099f9b40 07.11): спячка (agent_sleeps)
    'gemma_subagent',  # 63613c06 (rest#232, 06.10): саб-агент gemma 127.0.0.1:8181, экономия контекста
)
TOOLS_REGISTRY = {name: name for name in TOOL_NAMES}

def get_tools():
    result = []
    for name in TOOL_NAMES:
        function = getattr(import_module(f'bravo.tools.{name}'), name)
        result.append(logged_tool(function if isinstance(function, BaseTool) else tool(function)))
    return result
