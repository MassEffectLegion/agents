import paramiko
import time
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from delta2.core.ssh_helper import _create_ssh_client
from delta2.core.config import LOG_DIR
from delta2.core import tg_readonly_guard as _tgro

def _format_output(results: dict, stats_block: str, raw_outputs: list,
                   output_mode: str, max_output_per_server: int,
                   total_max_output: int, servers: list, command: str,
                   filter_local_ips: bool, timeout_per_server: int) -> str:
    """Helper function for output formatting to avoid recursion issues."""
    if output_mode == "stats_only":
        return stats_block

    if output_mode == "errors_only":
        error_lines = []
        for host, result in results.items():
            if result["status"] in ["FAILED", "ERROR", "TIMEOUT"]:
                error_lines.append(f"[{host}] {result['status']}: {result.get('error', result.get('stderr', '')[:300])}")
        if not error_lines:
            return stats_block + "\n✅ No errors"
        return stats_block + "\nERRORS:\n" + "\n".join(error_lines)

    total_output_len = sum(r.get("output_len", 0) for r in results.values() if r["status"] == "OK")
    if output_mode == "save_to_file" or (output_mode == "smart" and total_output_len > total_max_output):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(LOG_DIR, f"ssh_batch_{timestamp}.txt")
        os.makedirs(os.path.dirname(filename) or ".", exist_ok=True)
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(stats_block + "\n")
            for status in ["OK", "FAILED", "ERROR", "TIMEOUT"]:
                servers_with_status = [(h, r) for h, r in results.items() if r["status"] == status]
                if servers_with_status:
                    f.write(f"\n{'='*40}\n[{status}] ({len(servers_with_status)} servers)\n{'='*40}\n")
                    for host, result in servers_with_status:
                        f.write(f"\n[{host}] (exit code: {result.get('exit_code', 'N/A')})\n")
                        if result["status"] == "OK":
                            f.write(result['stdout'] + "\n")
                        else:
                            f.write(f"ERROR: {result.get('error', result.get('stderr', ''))}\n")
        return f"{stats_block}\n💾 SAVED: {filename}"

    if output_mode == "smart":
        combined_output = "\n---\n".join(raw_outputs)
        if len(combined_output) > total_max_output:
            return _format_output(results, stats_block, raw_outputs, "save_to_file",
                                  max_output_per_server, total_max_output,
                                  servers, command, filter_local_ips, timeout_per_server)
        return stats_block + "\n" + combined_output

    if output_mode == "full":
        report_lines = []
        for host, result in results.items():
            if result["status"] == "OK":
                report_lines.append(f"[{host}] OK (exit {result['exit_code']})\n{result['stdout']}")
            elif result["status"] == "FAILED":
                report_lines.append(f"[{host}] FAILED (exit {result['exit_code']})\nSTDOUT: {result['stdout']}\nSTDERR: {result['stderr']}")
            else:
                report_lines.append(f"[{host}] {result['status']}: {result.get('error', '')}")
        return stats_block + "\n\n".join(report_lines)

    return f"ERROR: Unknown output_mode: {output_mode}"

def ssh_execute_on_all_servers(
    servers: list,
    command: str,
    output_mode: str = "smart",
    max_output_per_server: int = 2200,
    total_max_output: int = 15000,
    filter_local_ips: bool = False,
    timeout_per_server: int = 60
) -> str:
    """Execute command on multiple servers in parallel with smart output management.
    Args:
        servers: List of server dicts [{"host":"ip","port":22,"user":"root","password":"...","key_path":"/path/to/key"}, ...]
        command: Bash command to execute
        output_mode: Output mode:
            - "smart": auto — truncated if small, save_to_file if exceeds limit
            - "full": full output
            - "save_to_file": always save to file, return path
            - "errors_only": only errors
            - "stats_only": only statistics
        max_output_per_server: Max chars per server output
        total_max_output: Total output limit (auto save_to_file if exceeded)
        filter_local_ips: Filter out localhost IPs from output
        timeout_per_server: Timeout per server in seconds
    """
    _g = _tgro.guard_ssh(command)
    if _g:
        return _g
    if not servers:
        return "ERROR: Server list is empty"

    start_time = time.time()
    results = {}

    def execute_on_one(server: dict):
        host = server.get("host", "unknown")
        client = None
        try:
            client = _create_ssh_client(
                host,
                server.get("port", 22),
                server.get("user", "root"),
                password=server.get("password"),
                key_path=server.get("key_path"),
                timeout=10
            )
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout_per_server)
            out = stdout.read().decode('utf-8', errors='replace')
            err = stderr.read().decode('utf-8', errors='replace')
            exit_status = stdout.channel.recv_exit_status()
            return host, {
                "status": "OK" if exit_status == 0 else "FAILED",
                "exit_code": exit_status,
                "stdout": out,
                "stderr": err,
                "output_len": len(out)
            }
        except Exception as e:
            return host, {"status": "ERROR", "error": str(e)}
        finally:
            if client is not None:
                client.close()

    with ThreadPoolExecutor(max_workers=min(len(servers), 10)) as executor:
        futures = {executor.submit(execute_on_one, srv): srv for srv in servers}
        for future in as_completed(futures):
            host, result = future.result()
            results[host] = result

    elapsed = time.time() - start_time
    ok_count = sum(1 for r in results.values() if r["status"] == "OK")
    failed_count = sum(1 for r in results.values() if r["status"] == "FAILED")
    error_count = sum(1 for r in results.values() if r["status"] == "ERROR")
    timeout_count = sum(1 for r in results.values() if r["status"] == "TIMEOUT")

    stats_block = f"✅ {ok_count}/{len(servers)} | ❌{failed_count} ⚠️{error_count} ⏱{timeout_count} | {elapsed:.1f}s"

    raw_outputs = []
    for host, result in results.items():
        if result["status"] == "OK":
            out = result['stdout']
            if len(out) > max_output_per_server:
                half = max_output_per_server // 2
                out = out[:half] + f"\n...[TRUNCATED, total {len(result['stdout'])} chars]...\n" + out[-half:]
            raw_outputs.append(f"[{host}]:\n{out}")
        elif result["status"] != "OK":
            raw_outputs.append(f"[{host}] {result['status']}: {result.get('error', '')[:200]}")

    return _format_output(results, stats_block, raw_outputs, output_mode,
                          max_output_per_server, total_max_output,
                          servers, command, filter_local_ips, timeout_per_server)
