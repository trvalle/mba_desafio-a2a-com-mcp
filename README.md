# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio MBA. O repositório contém dois processos independentes: o
servidor MCP em `servidor-mcp/` e o agente A2A em `agente/`. O agente fala com
o MCP exclusivamente por HTTP/JSON-RPC; não importa as funções de domínio.

## Como rodar

Requisitos: Python 3.10 ou mais recente (a validação final foi feita com
Python 3.12.10). A implementação usa somente a biblioteca padrão, portanto não
há dependências de pacote para instalar.

Em Linux/macOS, a partir da raiz do clone:

```bash
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
python3 servidor-mcp/server.py
```

Em outro terminal, ainda na raiz do clone:

```bash
export REQUEST_STATE_SECRET="<o-mesmo-valor-do-terminal-do-MCP>"
python3 agente/server.py
```

No PowerShell:

```powershell
$env:REQUEST_STATE_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
python servidor-mcp/server.py
```

Em outro terminal, copie o valor gerado para a variável e suba o agente:

```powershell
$env:REQUEST_STATE_SECRET = "<o-mesmo-valor-do-terminal-do-MCP>"
python agente/server.py
```

O servidor MCP escuta `http://127.0.0.1:7301/mcp`; o agente A2A escuta
`http://127.0.0.1:7300`, com card em
`/.well-known/agent-card.json` e JSON-RPC em `/a2a`.

Com os dois processos recém-iniciados, rode:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

No Windows, o equivalente é:

```powershell
python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

O segredo deve ter pelo menos 32 bytes aleatórios. Nunca coloque o valor usado
no repositório.

## Onde a ponte acontece

Em `agente/server.py`, `MCPClient._call()` recebe a resposta HTTP crua e
`_complete_from_mcp()` reconhece `resultType=input_required`. Nesse ponto o
agente não responde à elicitation: guarda o `requestState` como string opaca,
a chave e o enum de alternativas na Task, muda o estado para
`TASK_STATE_INPUT_REQUIRED` e publica somente `alternativas: ...`. Em uma nova
mensagem `escolha=...`, `_process_continuation()` usa o token guardado,
mantém a chave original e envia um novo `tools/call` com `inputResponses`.
O servidor em `servidor-mcp/server.py` valida e abre o token, reconstrói os
argumentos selados, aplica `accept`, `decline` ou `cancel`, e retorna
`complete`. O agente transforma o resultado em artifact A2A sem expor o token.

## Decisões técnicas

- Python 3.10+ e biblioteca padrão para tornar o clone reproduzível sem
  dependência implícita. O transporte implementa diretamente o wire contract
  JSON-RPC/Streamable HTTP cobrado pelo validador, incluindo
  `MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name`, `_meta`, `tools/list`,
  `resources/read` e `input_required`.
- `requestState` é um envelope versionado `v1.<payload>.<assinatura>` com
  payload Base64URL, HMAC-SHA256 e chave exclusivamente em
  `REQUEST_STATE_SECRET`. O TTL é de 15 minutos. O payload contém tudo que o
  servidor precisa para o retry, então o retry continua válido depois de um
  restart do MCP; adulteração e expiração retornam `-32602`.
- Reservas e Tasks são mantidas em memória, como permitido pelo enunciado.
  `dados/reservas.json` é a carga inicial e não é alterado; um restart volta a
  essa carga inicial. A integridade do `requestState` não depende dessa memória.
- O agente descobre as tools com `tools/list` antes do primeiro `tools/call`,
  lê `politica://uso` e extrai `2026-11-01` da primeira linha. O servidor MCP
  calcula política, conflitos e alternativas; o agente apenas traduz protocolo
  e estados.
- O parser A2A é determinístico e aceita somente a gramática definida:
  `reservar sala=... inicio=... fim=... responsavel=...` e
  `escolha=...`. Não há LLM, streaming, sessão de protocolo ou autenticação.

## Saída do validador

Última execução, com os dois processos recém-iniciados:

```text
trace-id desta execucao: 60efd315c16772ec27aaa8d87004b526
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

## Restrições não negociáveis

`dados/`, `validador/` e `exemplos/` são mantidos intactos. Não há banco de
dados, container, frontend, callback síncrono de elicitation, callback de
servidor para o cliente, OAuth ou dependência de modelo de linguagem.
