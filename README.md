# Clínica Aurora — demo pública do arquétipo B

Um agente que **escreve** na agenda de uma clínica e **recusa** quando a regra do
negócio não deixa. A clínica é fictícia e o ERP dela é publicado junto, para que
qualquer pessoa possa inspecionar o outro lado da integração.

Escopo aceito: `pgtech-agents/clientes/clinica-aurora/docs/escopo.md`.

## O que a demo prova, em menos de um minuto

| # | Momento | O que aparece na tela |
|---|---|---|
| 1 | **Escreve** | "remarca a da Ana pras 16h" → a linha muda no painel do ERP, ao vivo |
| 2 | **Recusa por política** | cancelar uma consulta que começa em 2h → `cancel_window_requires_reason` visível |
| 3 | **Recusa fora de escopo** | "me escreve um poema" → o agente diz que só cuida da agenda |
| 4 | **Não fabrica quando quebra** | o visitante derruba o ERP → o agente escala em vez de inventar confirmação |

O momento 4 é o diferencial. Todo mundo demonstra o caminho feliz.

## Arquitetura

```
navegador ──▶ agente (FastAPI)  ──HTTP──▶ clinica-aurora-erp (FastAPI)
                  │                          OpenAPI público em /docs
                  ├── policy/   as 5 regras que o LLM não decide
                  ├── tools/    5 operações, domínio fechado
                  └── adapters/ timeout 5s · retry só em leitura · circuit breaker
```

Dois processos separados de propósito: é o que faz o botão "derrubar o sistema"
significar alguma coisa, e é como um cliente real seria integrado.

Um terceiro processo, `mcpsrv/`, expõe o MESMO motor de política como servidor
MCP em `https://aurora.gomio.com.br/mcp`. Quem pluga o próprio Claude ou Cursor
ali continua sujeito às regras da clínica, e recebe o código da regra quando é
recusado: o seu agente, as minhas regras, o meu log. Ver `mcpsrv/README.md`.

## Rodando

```bash
make setup     # venv + dependências
make test      # suíte
make cov       # cobertura, com policy/ travada em 100%
make erp       # sobe só o ERP em 127.0.0.1:8080 (docs em /docs)
```

## Regras da casa

- `policy/` exige **100% de cobertura** — é pouco código e é o que protege juridicamente.
- Nenhuma tool inventa resultado. Se o ERP não gravou, a tool devolve `ToolError`.
- Toda escrita passa por confirmação (`G-17`), e isso é enforcement no registry, não instrução de prompt.
  O gate aparece no log de decisão como `gate · held`: a chamada barrada é visível, e não se confunde
  com a que executou depois.
- `rules.yaml` é versionado e a versão vai em cada linha do audit log — que sai em JSON por linha no
  logger `aurora.audit`, no stdout do container. A lista em memória alimenta a tela; o log é o que
  sobrevive à conversa e permite provar, depois, sob qual versão da regra a decisão foi tomada.
- `pricing.yaml` é versionado pelo mesmo motivo, e a versão vai em cada linha de custo. Um número de
  dinheiro sem a tarifa que o produziu não dá para auditar: meses depois ninguém consegue dizer sob
  qual tabela aquele valor foi contado, nem por que a série tem um degrau no dia em que o preço de
  introdução do Sonnet 5 caiu.
- O teto do dia é em **dólar**, não em turno, e funciona por reserva e reconciliação: retém uma
  estimativa antes da chamada paga e ajusta pela diferença quando o `usage` volta. Contar turnos
  media a coisa errada — um turno de seis iterações custa seis vezes um de uma iteração, e o
  contador não via diferença entre os dois.
- Toda chamada paga vira uma linha `llm` no log de decisão, com custo, latência e `cache read`.
  O `cache read` está ali de propósito: é o número que denuncia invalidador silencioso. Foi ele que
  revelou que o gate de confirmação estava matando o próprio cache ao mandar `tools=[]` — as tools
  renderizam ANTES do system no prefixo, então esvaziá-las invalidava tudo atrás, inclusive o bloco
  que carrega o breakpoint. A trava correta é `tool_choice: none`, que segura a escrita sem tirar um
  byte do prefixo (`tests/test_agent.py::test_gate_de_confirmacao_nao_muda_o_prefixo_cacheado`).
- A agenda semeada respeita o expediente: nasce no próximo dia útil inteiro, nunca "amanhã" —
  `tests/test_seed.py` varre um ano de aberturas para garantir isso.

## Limites da demo

Sessão anônima com tenant isolado, agenda semeada na hora e despejada quando a
sessão expira. Nada aqui persiste, e nenhum dado de um visitante é visível para
outro. Teto de gasto diário em dólar, reservado antes de cada chamada paga e
reconciliado contra o `usage` real depois, mais kill switch por env var.

O ERP público não aceita sessão escolhida por quem chama. Sem `X-Tenant-Id`, ou
com um id que nenhum servidor emitiu, a resposta é 401, e não existe tenant
padrão. Quem explora o `/docs` pede um id em `POST /sessions` (teto por IP). O
agente e o servidor MCP sorteiam o deles e falam com o ERP pela rede interna. O
ERP reconhece essa rede pela ausência de `X-Real-Ip`, que o Traefik sempre
escreve em request de fora (`tests/test_erp_sessoes.py`). Cada origem (`demo`,
`mcp`, `api`) tem o próprio teto de tenants, e encher uma não despeja a outra.
A página também limita sessões novas por IP.

A sessão vive no processo (`_sessoes`), o que prende a demo a **um worker só**.
É decisão consciente para o escopo dela — nada persiste por definição — e é o
primeiro ponto a mudar se um dia ela precisar de mais de uma réplica.
