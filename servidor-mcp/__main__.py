from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from pydantic import BaseModel

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.request_state import RequestStateSecurity
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolResult,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
    Resource,
    TextContent,
)

ROOT = Path(__file__).resolve().parents[1]
SALAS = json.loads((ROOT / "dados/salas.json").read_text(encoding="utf-8"))
RESERVAS: list[dict[str, Any]] = json.loads((ROOT / "dados/reservas.json").read_text(encoding="utf-8"))
POLITICA_TEXTO = (ROOT / "dados/politica-de-uso.md").read_text(encoding="utf-8")
POLITICA = re.match(r"^versao:\s*(\S+)", POLITICA_TEXTO).group(1)  # type: ignore[union-attr]
SALA_POR_ID = {s["id"]: s for s in SALAS}
FUSO_SAO_PAULO = timezone(timedelta(hours=-3))


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaSalasOut(BaseModel):
    salas: list[SalaOut]


def _result(data: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))],
        structured_content=data,
        is_error=is_error,
    )


def _erro(mensagem: str) -> CallToolResult:
    return _result({"erro": mensagem}, is_error=True)


def _parse_intervalo(sala_id: str, inicio: str, fim: str) -> tuple[datetime, datetime] | str:
    if sala_id not in SALA_POR_ID:
        return f"Sala inexistente: {sala_id}"
    try:
        ini = datetime.fromisoformat(inicio)
        fin = datetime.fromisoformat(fim)
    except (TypeError, ValueError):
        return "Intervalo invalido: fim deve ser posterior a inicio"
    if ini.tzinfo is None or fin.tzinfo is None or fin <= ini:
        return "Intervalo invalido: fim deve ser posterior a inicio"
    ini_sp = ini.astimezone(FUSO_SAO_PAULO)
    fin_sp = fin.astimezone(FUSO_SAO_PAULO)
    if ini_sp.time() < time(8, 0) or ini_sp.time() >= time(20, 0) or fin_sp.time() < time(8, 0) or fin_sp.time() > time(20, 0):
        return "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
    if fin - ini > timedelta(hours=2):
        return "Duracao acima do limite: a politica permite no maximo 2 horas"
    return ini, fin


def _conflitos(sala_id: str, inicio: str, fim: str) -> list[dict[str, Any]]:
    ini, fin = datetime.fromisoformat(inicio), datetime.fromisoformat(fim)
    return [
        r for r in RESERVAS
        if r["sala"] == sala_id
        and ini < datetime.fromisoformat(r["fim"])
        and datetime.fromisoformat(r["inicio"]) < fin
    ]


def _alternativas(sala_id: str, inicio: str, fim: str) -> list[str]:
    capacidade = SALA_POR_ID[sala_id]["capacidade"]
    candidatas = [
        sala for sala in SALAS
        if sala["id"] != sala_id
        and sala["capacidade"] >= capacidade
        and not _conflitos(sala["id"], inicio, fim)
    ]
    candidatas.sort(key=lambda sala: (sala["capacidade"], sala["id"]))
    return [s["id"] for s in candidatas[:3]]


def _estado_selado(params: dict[str, Any]) -> dict[str, Any] | None:
    raw_state = params.get("requestState")
    if not isinstance(raw_state, str):
        return None
    try:
        state = json.loads(raw_state)
    except (TypeError, ValueError):
        return None
    if not isinstance(state, dict) or not isinstance(state.get("arguments"), dict):
        return None
    return state


SECRET = os.environ.get("REQUEST_STATE_SECRET", "")
if not SECRET:
    raise RuntimeError("Defina REQUEST_STATE_SECRET com ao menos 32 bytes aleatorios.")
try:
    SECRET_BYTES = bytes.fromhex(SECRET)
except ValueError:
    SECRET_BYTES = SECRET.encode("utf-8")
if len(SECRET_BYTES) < 32:
    raise RuntimeError("REQUEST_STATE_SECRET precisa conter ao menos 32 bytes aleatorios.")

server = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[SECRET_BYTES], ttl=600),
)


@server.tool(name="listar_salas", description="Lista todas as salas com capacidade e recursos.")
async def listar_salas() -> ListaSalasOut:
    """Lista as salas disponíveis."""
    return ListaSalasOut(salas=[SalaOut(**s) for s in SALAS])


@server.tool(name="consultar_disponibilidade", description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.", structured_output=False)
async def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> CallToolResult:
    """Consulta a disponibilidade de uma sala."""
    validado = _parse_intervalo(sala, inicio, fim)
    if isinstance(validado, str):
        return _erro(validado)
    conflitos = _conflitos(sala, inicio, fim)
    dados = {
        "sala": sala,
        "livre": not conflitos,
        "conflitos": [
            {k: r[k] for k in ("id", "inicio", "fim", "responsavel")}
            for r in conflitos
        ],
    }
    return _result(dados)


@server.tool(name="reservar_sala", description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.", structured_output=False)
async def reservar_sala(sala: str, inicio: str, fim: str, responsavel: str, ctx: Context) -> CallToolResult | InputRequiredResult:
    """Reserva uma sala ou pausa para escolha entre alternativas."""
    params = dict(ctx.request_context.params or {})
    request_state = ctx.request_state
    if request_state is not None:
        params["requestState"] = request_state
    sealed_state = _estado_selado(params)
    state_args = sealed_state["arguments"] if sealed_state else None
    if request_state is not None and sealed_state is None:
        raise MCPError(code=-32602, message="Invalid or expired requestState", data={"reason": "invalid_request_state"})
    if sealed_state is not None:
        sala, inicio, fim, responsavel = (state_args[k] for k in ("sala", "inicio", "fim", "responsavel"))
        input_responses = ctx.input_responses or {}
        response = input_responses.get(sealed_state.get("input_key"))
        action = getattr(response, "action", None)
        if action in ("decline", "cancel"):
            return _result({"reserva": None, "reservado": False, "sala": None, "inicio": None, "fim": None, "responsavel": None, "politica": None, "motivo": "recusado"})
        response_content = getattr(response, "content", None)
        chosen = (response_content or {}).get("sala") if action == "accept" else None
        if chosen not in sealed_state.get("alternativas", []):
            raise MCPError(code=-32602, message="Invalid elicitation response")
        sala = chosen

    validado = _parse_intervalo(sala, inicio, fim)
    if isinstance(validado, str):
        return _erro(validado)
    conflicts = _conflitos(sala, inicio, fim)
    if conflicts:
        choices = _alternativas(sala, inicio, fim)
        if not choices:
            return _erro("Sem alternativas disponiveis no intervalo")
        if state_args is None:
            caps = (ctx.request_context.params or {}).get("_meta", {}).get("io.modelcontextprotocol/clientCapabilities", {})
            if not isinstance(caps, dict) or not isinstance(caps.get("elicitation", {}).get("form"), dict):
                raise MCPError(
                    code=-32021,
                    message="Client did not declare the form elicitation capability required by resolver '__main__:escolha_de_sala'",
                    data={"requiredCapabilities": {"elicitation": {"form": {}}}},
                )
            input_key = "central-de-salas:escolha_de_sala"
            state = json.dumps({"arguments": {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}, "input_key": input_key, "alternativas": choices}, separators=(",", ":"))
            request = ElicitRequestFormParams(
                mode="form",
                message="A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
                requested_schema={
                    "type": "object",
                    "properties": {"sala": {"type": "string", "title": "Sala", "description": "Sala alternativa escolhida", "enum": choices}},
                    "required": ["sala"],
                },
            )
            return InputRequiredResult(input_requests={input_key: {"method": "elicitation/create", "params": request}}, request_state=state)
        return _erro("Sem alternativas disponiveis no intervalo")

    next_num = max((int(r["id"].split("-")[1]) for r in RESERVAS), default=0) + 1
    reserva_id = f"res-{next_num:04d}"
    reserva = {"id": reserva_id, "sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    RESERVAS.append(reserva)
    return _result({"reserva": reserva_id, "reservado": True, "sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel, "politica": POLITICA, "motivo": None})


@server.resource("politica://uso", name="Politica de uso", mime_type="text/markdown")
async def politica_de_uso() -> str:
    """Texto da política de uso das salas."""
    return POLITICA_TEXTO


def main() -> None:
    app = server.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True, host="127.0.0.1")
    wrapped = HttpContractMiddleware(app)
    uvicorn.run(wrapped, host=os.getenv("MCP_HOST", "0.0.0.0"), port=int(os.getenv("MCP_PORT", "7301")), log_level="warning")


class HttpContractMiddleware:
    """Enforce per-request wire metadata before dispatch and HTTP error status."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/mcp" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        body_parts = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body_parts.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
        body = b"".join(body_parts)
        try:
            request = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return await self.app(scope, replay(body), send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        method = request.get("method")
        params = request.get("params") or {}
        meta = params.get("_meta") or {}
        traceparent = meta.get("traceparent", "-") if isinstance(meta, dict) else "-"
        print(f"method={method} id={request.get('id')} traceparent={traceparent}", file=sys.stderr, flush=True)
        if not isinstance(meta, dict) or "io.modelcontextprotocol/protocolVersion" not in meta or "io.modelcontextprotocol/clientCapabilities" not in meta:
            return await json_error(send, request.get("id"), -32602, "Missing required request _meta", 400)
        mismatch = headers.get("mcp-method") != method
        mismatch = mismatch or headers.get("mcp-protocol-version") != meta.get("io.modelcontextprotocol/protocolVersion")
        if method in ("tools/call", "resources/read"):
            expected_name = params.get("name") if method == "tools/call" else params.get("uri")
            mismatch = mismatch or headers.get("mcp-name") != expected_name
        if mismatch:
            return await json_error(send, request.get("id"), -32020, "MCP headers do not match the request body", 400)
        response_messages = []
        async def capture_send(message):
            response_messages.append(message)
        await self.app(scope, replay(body), capture_send)
        starts = [m for m in response_messages if m["type"] == "http.response.start"]
        if not starts:
            return
        outgoing_start = dict(starts[0])
        response_body = b"".join(m.get("body", b"") for m in response_messages if m["type"] == "http.response.body")
        try:
            response_json = json.loads(response_body)
        except (ValueError, UnicodeDecodeError):
            response_json = {}
        if response_json.get("error", {}).get("code") == -32021:
            outgoing_start["status"] = 400
        await send(outgoing_start)
        for item in (m for m in response_messages if m["type"] == "http.response.body"):
            await send(item)


def replay(body: bytes):
    done = False
    async def receive():
        nonlocal done
        if done:
            return {"type": "http.disconnect"}
        done = True
        return {"type": "http.request", "body": body, "more_body": False}
    return receive


async def json_error(send, request_id, code: int, message: str, status: int):
    data = json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]})
    await send({"type": "http.response.body", "body": data})


if __name__ == "__main__":
    main()
