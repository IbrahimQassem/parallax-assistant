"""CLI process lifecycle tests with local synthetic executables, no provider call."""
import asyncio
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from parallax.assistant.actions import Action
from parallax.assistant.planner import ANTIGRAVITY_TOOL_GUARD, CliPlanner, InvalidProposal, parse_output


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


@pytest.mark.parametrize("payload", [None, [], "{}", {"kind": "navigate"},
    {"kind": "navigate", "target": "", "value": "https://example.com", "reason": "", "extra": True}])
def test_antigravity_never_falls_back_to_response(payload):
    action = Action("navigate", value="https://example.com")
    event = {"event": "result", "result": {"status": "SUCCESS",
        "structured_output": payload, "response": json.dumps(action.to_dict())}}
    with pytest.raises(InvalidProposal):
        parse_output("antigravity", json.dumps(event))


def test_antigravity_uses_canonical_payload_not_response_metadata():
    action = Action("navigate", value="https://example.com")
    event = {"event": "result", "result": {"status": "SUCCESS",
        "structured_output": action.to_dict(),
        "response": json.dumps({**action.to_dict(), "toolAction": "Navigate"}) + "\nextra prose"}}
    assert parse_output("antigravity", json.dumps(event)) == action


@pytest.mark.parametrize("name", ["finish", "run_command", "view_file", "browser_click",
    "call_mcp_tool", "invoke_subagent", "write_to_file", "ask_permission", "unknown"])
def test_antigravity_guard_only_permits_completion(name):
    result = subprocess.run([sys.executable, "-c", ANTIGRAVITY_TOOL_GUARD],
        input=json.dumps({"toolCall": {"name": name, "args": {"private": "DO_NOT_LOG"}}}),
        text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)["decision"] == ("allow" if name == "finish" else "deny")
    assert "DO_NOT_LOG" not in result.stdout + result.stderr


@pytest.mark.parametrize("payload", ["not json", "null", "[]", "{}", '{"toolCall":null}'])
def test_antigravity_guard_denies_malformed_input(payload):
    result = subprocess.run([sys.executable, "-c", ANTIGRAVITY_TOOL_GUARD],
        input=payload, text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)["decision"] == "deny"


def test_antigravity_launch_uses_completion_transport_and_keeps_plan_sandbox(monkeypatch):
    from parallax.assistant import planner
    spawn = asyncio.create_subprocess_exec
    action = Action("navigate", value="https://example.com")

    async def capture(*args, **kwargs):
        assert args[args.index("--mode") + 1] == "plan"
        assert "--sandbox" in args
        assert "PRIVATE TASK" not in " ".join(args)
        directory = Path(kwargs["cwd"])
        agent = (directory / ".agents/agents/parallax-planner/agent.md").read_text()
        assert "tools: [finish]" in agent
        assert "Call ONLY the CLI finish tool once" in agent
        assert "Never call your own tools" not in agent
        hook = json.loads((directory / ".agents/hooks.json").read_text())
        gate = hook["parallax-planner-tools"]["PreToolUse"][0]
        assert gate["matcher"] == "*"
        command = shlex.split(gate["hooks"][0]["command"])
        assert command[0] == sys.executable
        assert Path(command[1]).read_text() == ANTIGRAVITY_TOOL_GUARD
        code = ("import json,sys\n"
            "prompt=json.loads(sys.stdin.read())['message']['content']\n"
            "assert 'PRIVATE TASK' in prompt\n"
            "assert 'Call ONLY the CLI finish tool once' in prompt\n"
            f"print({json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'structured_output': action.to_dict()}})!r})\n")
        return await spawn(sys.executable, "-c", code, **kwargs)

    monkeypatch.setattr(planner.shutil, "which", lambda _: "/synthetic/agy")
    monkeypatch.setattr(planner.asyncio, "create_subprocess_exec", capture)
    assert asyncio.run(CliPlanner("antigravity").propose("PRIVATE TASK", {}, [])) == action
