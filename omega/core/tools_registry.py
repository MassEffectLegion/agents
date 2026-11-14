"""Single executor's tool registry; no PostgreSQL dependencies."""
from importlib import import_module
from langchain_core.tools import tool
from omega.tools.local_operations import (
    query_sqlite, save_progress, complete_request, backup_sqlite, run_python_script,
    run_command, status_process, stop_process, consult_models,
)

def get_tools():
    result = [query_sqlite, save_progress, complete_request, backup_sqlite, run_python_script,
              run_command, status_process, stop_process, consult_models]
    for name in ('read_local_file_chunked','list_files_in_directory','write_local_file',
                 'execute_ssh_command','ssh_execute_on_all_servers','download_file_from_server','upload_file_to_server'):
        result.append(tool(getattr(import_module('omega.tools.'+name), name)))
    return result
