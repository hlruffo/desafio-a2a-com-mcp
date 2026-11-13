# A Ponte: agente A2A com servidor MCP

Implementação determinística do desafio da Hill Valley Tech. Há dois processos independentes: `servidor-mcp`, que publica as ferramentas e o resource por MCP Streamable HTTP, e `agente`, que consome esse endpoint por HTTP e publica Tasks A2A por JSON-RPC 2.0. Não há LLM nem persistência de reservas em disco.

## Como rodar

Requisitos: Python 3.11 ou superior e [uv](https://docs.astral.sh/uv/). A partir da raiz de um clone limpo, instale as versões travadas:

```bash
uv sync --locked
```

Gere uma chave aleatória com pelo menos 32 bytes e mantenha-a no ambiente do terminal do servidor MCP. O valor é secreto e não deve ser salvo no repositório:

```bash
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

Em um terminal, suba o servidor MCP:

```bash
uv run python -m servidor-mcp
```

Em outro terminal, suba o agente:

```bash
uv run python -m agente
```

O servidor MCP atende em `http://localhost:7301/mcp`. O agente atende em `http://localhost:7300/a2a` e publica o card em `http://localhost:7300/.well-known/agent-card.json`. Em um terceiro terminal, com os dois processos recém-iniciados, rode o validador:

```bash
uv run python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

As reservas são carregadas de `dados/reservas.json` no início do servidor MCP e as novas reservas ficam em memória até ele ser reiniciado. Reinicie os dois processos antes de repetir o validador para começar com o mesmo conjunto de dados. Para que um retry pendente sobreviva a uma reinicialização, inicie novamente o servidor MCP com o mesmo `REQUEST_STATE_SECRET`.

## Onde a ponte acontece

Em `agente/__main__.py`, `call_reservation()` interpreta `resultType=input_required`, conserva em `task["bridge"]` o token opaco e a chave de resposta e converte o schema da elicitation na mensagem `alternativas: ...` da Task. `process_continuation()` valida a escolha contra as alternativas anunciadas e chama `call_retry()`, que envia um novo `tools/call` com id novo, os argumentos originais, `inputResponses` e o `requestState` intacto. O token nunca entra no histórico, na resposta pública da Task ou no artifact. O servidor MCP reconstrói o pedido no handler `reservar_sala()`.

## Decisões técnicas

- O servidor MCP usa `mcp==2.0.0`, o SDK oficial Python para MCP v2 e revisão `2026-07-28`, com `MCPServer.streamable_http_app()` em `/mcp`. O SDK fornece `RequestStateBoundary` e `RequestStateSecurity`: o token é protegido com AES-256-GCM e HKDF-SHA256, com audiência vinculada a `central-de-salas`. A validade é de 600 segundos (10 minutos), dentro do intervalo exigido. O segredo vem exclusivamente de `REQUEST_STATE_SECRET`.
- O corpo de estado interno contém o pedido inicial, a chave da elicitation e as alternativas. O SDK cifra e autentica esse conteúdo, verifica expiração e vincula o retry ao método e aos argumentos da chamada inicial. O handler usa o pedido selado e rejeita respostas fora das alternativas.
- O agente descobre as tools por `tools/list`, descobre o resource por `resources/list` e lê `politica://uso` antes das chamadas. Ele envia `_meta` e os headers MCP espelhados em cada requisição. O `trace-id` A2A é preservado durante toda a Task e registrado pelo servidor MCP no stderr com método e id.
- As Tasks e o vínculo privado da ponte ficam em memória no processo A2A. Estados `COMPLETED`, `CANCELED` e `FAILED` são terminais. As reservas ficam em memória no processo MCP, conforme permitido pelo desafio.
- As regras de sala, disponibilidade, política e alternativas são aplicadas somente no servidor MCP. O agente interpreta o formato fixo de entrada e traduz a pausa/resposta entre os protocolos.

## Saída do validador

Última execução: `uv run python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301`.

```text
trace-id desta execucao: 4e085f958b80fa96d8796306c12bf8af
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
