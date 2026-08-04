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
                  ├── tools/    4 operações, domínio fechado
                  └── adapters/ timeout 5s · retry só em leitura · circuit breaker
```

Dois processos separados de propósito: é o que faz o botão "derrubar o sistema"
significar alguma coisa, e é como um cliente real seria integrado.

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
- `rules.yaml` é versionado e a versão vai em cada linha do audit log.

## Limites da demo

Sessão anônima com tenant isolado, agenda semeada na hora e despejada quando a
sessão expira. Nada aqui persiste, e nenhum dado de um visitante é visível para
outro.
