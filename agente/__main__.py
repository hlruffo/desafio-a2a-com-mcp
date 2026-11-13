from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:7301/mcp")
AGENT_HOST = os.getenv("AGENT_HOST", "0.0.0.0")
AGENT_PORT = int(os.getenv("AGENT_PORT", "7300"))
MCP_PROTOCOL = "2026-07-28"
CAPABILITIES = {"elicitation": {"form": {}}}
LOCK = threading.RLock()
TASKS: dict[str, dict[str, Any]] = {}
RPC_COUNTER = 0

CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [{"url": f"http://localhost:{AGENT_PORT}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [{
        "id": "reservar-sala",
        "name": "Reservar sala",
        "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
        "tags": ["salas", "agenda"],
        "inputModes": ["text/plain"],
        "outputModes": ["text/plain"],
        "examples": ["reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"],
    }],
}


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def trace_context(header: str | None) -> str:
    if header:
        return header
    return f"00-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01"


def mcp_request(method: str, params: dict[str, Any], traceparent: str, name: str | None = None) -> dict[str, Any]:
    global RPC_COUNTER
    RPC_COUNTER += 1
    request_id = f"agent-{RPC_COUNTER}-{uuid.uuid4().hex[:8]}"
    wire = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {**params, "_meta": {
            "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL,
            "io.modelcontextprotocol/clientInfo": {"name": "agente-central-de-salas", "version": "1.0.0"},
            "io.modelcontextprotocol/clientCapabilities": CAPABILITIES,
            "traceparent": traceparent,
        }},
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_PROTOCOL,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    req = urllib.request.Request(MCP_URL, data=json.dumps(wire).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            parsed = json.loads(response.read())
    except urllib.error.HTTPError as error:
        parsed = json.loads(error.read())
    if parsed.get("error"):
        raise RuntimeError(parsed["error"].get("message", "Erro de protocolo MCP"))
    return parsed.get("result") or {}


def mcp_tool_index(traceparent: str) -> dict[str, dict[str, Any]]:
    result = mcp_request("tools/list", {}, traceparent)
    return {tool["name"]: tool for tool in result.get("tools", [])}


def read_policy_version(traceparent: str) -> str:
    resource_list = mcp_request("resources/list", {}, traceparent)
    if not any(resource.get("uri") == "politica://uso" for resource in resource_list.get("resources", [])):
        raise RuntimeError("O servidor MCP nao anunciou o resource politica://uso")
    result = mcp_request("resources/read", {"uri": "politica://uso"}, traceparent, "politica://uso")
    contents = result.get("contents", [])
    if not contents:
        raise RuntimeError("O servidor MCP nao devolveu o resource politica://uso")
    first_line = contents[0].get("text", "").splitlines()[0]
    match = re.fullmatch(r"versao:\s*(\S+)", first_line)
    if not match:
        raise RuntimeError("Versao da politica ausente na primeira linha do resource")
    return match.group(1)


def call_reservation(arguments: dict[str, str], traceparent: str, retry: dict[str, Any] | None = None) -> dict[str, Any]:
    # Runtime discovery: use the current server's advertised tool contract.
    tools = mcp_tool_index(traceparent)
    if "reservar_sala" not in tools:
        raise RuntimeError("O servidor MCP nao anunciou a tool reservar_sala")
    policy_version = read_policy_version(traceparent)
    params: dict[str, Any] = {"name": "reservar_sala", "arguments": arguments}
    if retry is not None:
        params["inputResponses"] = retry["inputResponses"]
        params["requestState"] = retry["requestState"]
    result = mcp_request("tools/call", params, traceparent, "reservar_sala")
    if result.get("resultType") == "input_required":
        requests = result.get("inputRequests") or {}
        if len(requests) != 1 or not result.get("requestState"):
            raise RuntimeError("Resposta input_required MCP invalida")
        key, request = next(iter(requests.items()))
        schema = request.get("params", {}).get("requestedSchema", {})
        property_schema = schema.get("properties", {}).get("sala", {})
        alternatives = property_schema.get("enum")
        if alternatives is None and "const" in property_schema:
            alternatives = [property_schema["const"]]
        alternatives = alternatives or []
        return {
            "input_required": True,
            "alternatives": alternatives,
            "key": key,
            "requestState": result["requestState"],
            "arguments": arguments,
            "policy": policy_version,
        }
    if result.get("isError"):
        text = " ".join(block.get("text", "") for block in result.get("content", []))
        raise ToolExecutionError(text)
    data = result.get("structuredContent") or {}
    if data.get("reservado") is True:
        data = {**data, "politica": policy_version}
    return {"input_required": False, "data": data}


class ToolExecutionError(Exception):
    pass


def call_retry(task: dict[str, Any], choice: str, traceparent: str) -> dict[str, Any]:
    bridge = task["bridge"]
    action = "decline" if choice == "recusar" else "accept"
    response: dict[str, Any] = {"action": action}
    if action == "accept":
        response["content"] = {"sala": choice}
    params = {
        "name": "reservar_sala",
        "arguments": bridge["arguments"],
        "inputResponses": {bridge["key"]: response},
        "requestState": bridge["requestState"],
    }
    result = mcp_request("tools/call", params, traceparent, "reservar_sala")
    if result.get("resultType") == "input_required":
        raise RuntimeError("O servidor MCP pediu input novamente no retry")
    if result.get("isError"):
        text = " ".join(block.get("text", "") for block in result.get("content", []))
        raise ToolExecutionError(text)
    return {"data": result.get("structuredContent") or {}}


def text_of(message: dict[str, Any]) -> str:
    return " ".join(part.get("text", "") for part in message.get("parts", []) if isinstance(part, dict))


def user_message(message: dict[str, Any]) -> dict[str, Any]:
    result = {k: v for k, v in message.items() if k in ("messageId", "role", "parts", "taskId", "contextId", "metadata")}
    return result


def agent_message(text: str, task: dict[str, Any]) -> dict[str, Any]:
    message = {
        "messageId": new_id("msg"),
        "role": "ROLE_AGENT",
        "parts": [{"text": text}],
        "taskId": task["id"],
        "contextId": task["contextId"],
    }
    task["history"].append(message)
    task["status"]["message"] = message
    return message


def finish_from_result(task: dict[str, Any], result: dict[str, Any]) -> None:
    data = result["data"]
    if data.get("reservado") is False:
        task["status"]["state"] = "TASK_STATE_CANCELED"
        agent_message("Reserva recusada.", task)
        task.pop("bridge", None)
        return
    task["status"]["state"] = "TASK_STATE_COMPLETED"
    artifact_data = {k: data[k] for k in ("reserva", "sala", "inicio", "fim", "responsavel", "politica") if k in data}
    task["artifacts"] = [{
        "artifactId": new_id("art"),
        "name": "reserva",
        "parts": [{"text": json.dumps(artifact_data, ensure_ascii=False)}],
    }]
    agent_message(f"Reserva {data.get('reserva')} confirmada na {data.get('sala')}.", task)
    task.pop("bridge", None)


def process_new_task(message: dict[str, Any], traceparent: str) -> dict[str, Any]:
    task = {
        "id": new_id("task"),
        "contextId": new_id("ctx"),
        "trace_id": traceparent.split("-")[1],
        "status": {"state": "TASK_STATE_SUBMITTED"},
        "history": [user_message(message)],
        "artifacts": [],
    }
    TASKS[task["id"]] = task
    task["status"]["state"] = "TASK_STATE_WORKING"
    text = text_of(message)
    match = re.fullmatch(r"reservar sala=(\S+) inicio=(\S+) fim=(\S+) responsavel=(.+)", text)
    if not match:
        task["status"]["state"] = "TASK_STATE_FAILED"
        agent_message("Formato esperado: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>", task)
        return task
    arguments = dict(zip(("sala", "inicio", "fim", "responsavel"), match.groups()))
    try:
        result = call_reservation(arguments, traceparent)
        if result["input_required"]:
            task["bridge"] = {
                "arguments": result["arguments"],
                "key": result["key"],
                "requestState": result["requestState"],
                "alternatives": result["alternatives"],
            }
            task["status"]["state"] = "TASK_STATE_INPUT_REQUIRED"
            agent_message("alternativas: " + ", ".join(result["alternatives"]), task)
        else:
            finish_from_result(task, result)
    except ToolExecutionError as error:
        task["status"]["state"] = "TASK_STATE_FAILED"
        agent_message(str(error), task)
    except Exception as error:
        task["status"]["state"] = "TASK_STATE_FAILED"
        agent_message(str(error), task)
    return task


def process_continuation(task: dict[str, Any], message: dict[str, Any], traceparent: str) -> dict[str, Any]:
    task["history"].append(user_message(message))
    choice_text = text_of(message)
    match = re.fullmatch(r"escolha=(.+)", choice_text)
    choice = match.group(1) if match else ""
    bridge = task.get("bridge") or {}
    if choice != "recusar" and choice not in bridge.get("alternatives", []):
        agent_message("alternativas: " + ", ".join(bridge.get("alternatives", [])), task)
        task["status"]["state"] = "TASK_STATE_INPUT_REQUIRED"
        return task
    task["status"]["state"] = "TASK_STATE_WORKING"
    # Keep the Task's trace identity across A2A continuation requests, including
    # continuations that arrive without a traceparent header.
    traceparent = f"00-{task['trace_id']}-{uuid.uuid4().hex[:16]}-01"
    try:
        result = call_retry(task, choice, traceparent)
        finish_from_result(task, result)
    except ToolExecutionError as error:
        task["status"]["state"] = "TASK_STATE_FAILED"
        task.pop("bridge", None)
        agent_message(str(error), task)
    except Exception as error:
        task["status"]["state"] = "TASK_STATE_FAILED"
        task.pop("bridge", None)
        agent_message(str(error), task)
    return task


def public_task(task: dict[str, Any]) -> dict[str, Any]:
    # Internal bridge state (including the opaque requestState) never crosses A2A.
    return {k: v for k, v in task.items() if k not in {"bridge", "trace_id"}}


def rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class Handler(BaseHTTPRequestHandler):
    server_version = "A2A-CentralDeSalas/1.0"

    def do_GET(self):
        if urlparse(self.path).path == "/.well-known/agent-card.json":
            return self.send_json(200, CARD)
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/a2a":
            return self.send_json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            return self.send_json(400, rpc_error(None, -32700, "Parse error"))
        request_id = body.get("id")
        method = body.get("method")
        params = body.get("params") or {}
        traceparent = trace_context(self.headers.get("traceparent"))
        with LOCK:
            if method == "SendMessage":
                message = params.get("message") or {}
                task_id = message.get("taskId")
                if task_id:
                    task = TASKS.get(task_id)
                    if task is None:
                        return self.send_json(200, rpc_error(request_id, -32001, "Task not found"))
                    if task["status"]["state"] in {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED"}:
                        return self.send_json(200, rpc_error(request_id, -32001, "Task is in a terminal state"))
                    task = process_continuation(task, message, traceparent)
                else:
                    task = process_new_task(message, traceparent)
                return self.send_json(200, {"jsonrpc": "2.0", "id": request_id, "result": {"task": public_task(task)}})
            if method == "GetTask":
                task = TASKS.get(params.get("id"))
                if task is None:
                    return self.send_json(200, rpc_error(request_id, -32001, "Task not found"))
                return self.send_json(200, {"jsonrpc": "2.0", "id": request_id, "result": {"task": public_task(task)}})
            return self.send_json(200, rpc_error(request_id, -32601, f"Method not found: {method}"))

    def send_json(self, status: int, value: Any):
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args):
        return


def main() -> None:
    ThreadingHTTPServer((AGENT_HOST, AGENT_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
