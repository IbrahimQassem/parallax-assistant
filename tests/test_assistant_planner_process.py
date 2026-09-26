"""CLI process lifecycle tests with local synthetic executables, no provider call."""
import asyncio
import json
import sys

import pytest

from parallax.assistant.actions import Action
from parallax.assistant.planner import CliPlanner, parse_output


@pytest.mark.parametrize("exit_code", [0, 3])
def test_antigravity_waits_for_eof_and_final_schema_output(exit_code):
    async def run():
        action = Action("navigate", value="https://example.com")
        result = {"event": "result", "result": {"status": "SUCCESS", "structured_output": action.to_dict()}}
        code = (
            "import json,sys\n"
            "print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'interim prose'}}),flush=True)\n"
            "message=json.loads(sys.stdin.buffer.read())\n"
            "assert message['event']=='user'\n"
            f"print({json.dumps(result)!r},flush=True)\n"
            f"sys.exit({exit_code})\n"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            call = CliPlanner._read_antigravity_turn(process, b'{"event":"user","message":{"content":"synthetic"}}\n')
            if exit_code:
                with pytest.raises(RuntimeError, match="exit 3"):
                    await asyncio.wait_for(call, 3)
            else:
                output = await asyncio.wait_for(call, 3)
                assert parse_output("antigravity", output) == action
                assert process.returncode == 0
        finally:
            await CliPlanner._stop_process(process)
    asyncio.run(run())
