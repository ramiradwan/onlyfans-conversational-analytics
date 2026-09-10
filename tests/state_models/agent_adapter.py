"""Persistent JSON-lines adapter for the real Agent qualification harness."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "extension" / "qualification" / "ingestion-model-harness.mjs"


class AgentHarness:
    """One real Node process per state-machine history, with replayable records."""

    def __init__(self) -> None:
        self.trace: list[dict[str, Any]] = []
        self.request_id = 0
        self.process = subprocess.Popen(
            ["node", str(HARNESS)],
            cwd=ROOT,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def command(self, operation: str, **payload: Any) -> dict[str, Any]:
        self.request_id += 1
        command = {"request_id": self.request_id, "operation": operation, **payload}
        before = self.trace[-1].get("after") if self.trace else None
        if self.process.poll() is not None:
            raise RuntimeError(f"Agent harness exited: {self._stderr()}")
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(f"{json.dumps(command, sort_keys=True)}\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"Agent harness produced no response: {self._stderr()}")
        reply = json.loads(line)
        if not isinstance(reply.get("state"), dict) or not isinstance(reply.get("ok"), bool):
            raise RuntimeError(f"Agent harness protocol mismatch: {reply!r}")
        self.trace.append({
            "request_id": self.request_id,
            "operation": operation,
            "command": command,
            "before": before,
            "after": reply["state"],
            "result": reply.get("result"),
            "error": reply.get("error"),
        })
        return reply

    def replay(self, trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replay and compare every saved semantic transition, not just its tail."""
        replies: list[dict[str, Any]] = []
        for item in trace:
            if item.get("before") is not None and self.trace:
                assert self._semantic(self.trace[-1]["after"]) == self._semantic(item["before"])
            reply = self.command(
                item["command"]["operation"],
                **{key: value for key, value in item["command"].items() if key not in {"operation", "request_id"}},
            )
            assert self._semantic(reply["state"]) == self._semantic(item["after"])
            assert self._model_semantic(reply["state"]) == self._model_semantic(item["model"]["state"])
            assert reply.get("result") == item.get("result")
            assert reply.get("error") == item.get("error")
            context = item.get("context")
            if context is not None:
                assert context["stream"] == reply["state"]["identity"]["agent_stream_id"]
                assert context["session"] == {
                    "connected": reply["state"]["transport"]["connected"],
                    "fence": reply["state"]["transport"]["fence"],
                }
            assert item.get("requirements"), "saved trace omitted AG requirement mapping"
            replies.append(reply)
        return replies

    @staticmethod
    def _semantic(state: dict[str, Any]) -> dict[str, Any]:
        value = json.loads(json.dumps(state))
        for item in value["outbox"]:
            item.pop("event_id", None)
        for manifest in value["manifests"]:
            manifest.pop("scan_kind_index", None)
            manifest.pop("scan_after_key", None)
            manifest.pop("next_chunk_index", None)
        for chunk in value["chunks"]:
            chunk.pop("key", None)
        transport = value["transport"]
        transport.pop("connection_id", None)
        transport.pop("scheduled_callbacks", None)
        for frame in transport.get("frames", []):
            for opaque in ("connection_id", "fencing_token", "creator_account_id", "agent_installation_id", "agent_stream_id"):
                frame["payload"].pop(opaque, None)
        for name, key in (("outbox", "source_seq"), ("chats", "chat_id"), ("messages", "message_id"), ("coverage", "evidence_key"), ("manifests", "snapshot_id"), ("chunks", "chunk_index"), ("overrides", "key")):
            value[name].sort(key=lambda item: item[key])
        for chunk in value["chunks"]:
            chunk["records"].sort(key=lambda item: json.dumps(item, sort_keys=True))
        return value

    @classmethod
    def _model_semantic(cls, state: dict[str, Any]) -> dict[str, Any]:
        value = cls._semantic(state)
        value["transport"].pop("frames", None)
        return value

    def _stderr(self) -> str:
        return self.process.stderr.read() if self.process.stderr is not None else ""

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        if self.process.stdin is not None:
            self.process.stdin.close()
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
