"""Log tool names, outcomes and elapsed time without logging arguments/secrets."""
import json
import logging
import time
from langchain_core.tools import BaseTool


class _LoggedTool(BaseTool):
    inner: BaseTool

    def _run(self, **kwargs):
        started = time.perf_counter()
        logging.info('TOOL CALL %s', self.name)
        try:
            result = self.inner.invoke(kwargs)
        except BaseException:
            logging.error('TOOL ERROR %s dur_ms=%.1f', self.name, (time.perf_counter()-started)*1000)
            raise
        failed = isinstance(result, str) and (
            result.lstrip().upper().startswith(('ERROR:', 'TOOL ERROR:', 'POSTGRESQL ERROR:')))
        if isinstance(result, str) and result.startswith('{'):
            try:
                payload = json.loads(result)
                failed = failed or isinstance(payload, dict) and (
                    payload.get('timed_out', False) or payload.get('returncode', 0) not in (None, 0))
            except ValueError:
                pass
        logging.info('TOOL %s %s dur_ms=%.1f', 'ERROR' if failed else 'DONE', self.name,
                     (time.perf_counter()-started)*1000)
        return result


def logged_tool(inner):
    return _LoggedTool(name=inner.name, description=inner.description,
                       args_schema=inner.args_schema, return_direct=inner.return_direct, inner=inner)
