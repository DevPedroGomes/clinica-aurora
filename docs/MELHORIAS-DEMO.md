# 09 — Aurora demo: plano de melhorias pro funil Upwork

> Criado em 18/08/2026 a pedido do Pedro, a partir do padrão encontrado em 20 job posts lidos ao vivo no feed do Upwork (17/08). O repo da demo não está neste Mac (provavelmente na VPS) — este doc é a fonte do plano até a sessão que for implementar. Regra D-022 vale para tudo aqui: só entra na página o que roda de verdade.

## Estado atual verificado (18/08, pela página ao vivo)

Já existe e é bom — não refazer:
- Split-screen: chat à esquerda, "Schedule in the ERP online" atualizando ao vivo; ERP publicado ao lado (aurora-erp.pgdev.com.br/docs) — "the write really happens, over HTTP".
- **Decision log**: toda tool call, decisão de política e escrita aparece com o código da regra que decidiu.
- Botão **"Break the system"**.
- Demo limits por sessão anônima: agenda isolada, máx. 3 escritas/conversa, cap diário + kill switch, nada persiste.
- Copy em inglês, multi-idioma na resposta ("write in any language").

Persona atual: **front-desk assistant** — o usuário do chat é o funcionário da recepção ("check the schedule, reschedule, cancel and open slots").

## Decisão de escopo (avaliação 18/08)

**Adotar persona dupla, com PACIENTE como default.** Racional (dos 20 jobs):
- A maioria dos jobs compra agente voltado ao **cliente final** (candidatos no WhatsApp, ligações de clientes, inbound de clientes) — o comprador imagina o cliente DELE na conversa.
- Role-play de paciente tem fricção zero (todo visitante sabe ser paciente; ser recepcionista exige contexto).
- Os momentos de venda só fazem sentido contra usuário não-confiável: recusa de desconto por política, prompt injection ("input é dado, não instrução") e **recusa por privacidade** (perguntar do horário de outro paciente → BLOCK com regra no log) não são demonstráveis com persona de funcionário.
- Manter o modo front-desk como toggle ("Play as: Patient | Front desk") — prova também a capacidade de copiloto interno (jobs de agência pedem isso) sem custo extra de backend.

## Gaps → melhorias (mapeadas ao padrão dos jobs)

### Tier 1 — antes de intensificar propostas (horas)
1. **4 botões de cenário guiado** (o visitante não digita nada): Book / recusa por política (desconto) / fora de escopo / Break the ERP → escalada. "Break the system" já existe; faltam os outros 3 + o roteiro.
2. **Custo e latência por turno no Decision log** (meta <US$0,05/conversa do `make cost`) — responde "cost control per outcome" pedido nos jobs.
3. **Barra de provas verificáveis**: "123 tests · 100% policy coverage · avg cost/conversation · last deploy" com números reais.
4. **Loom 2-3 min** seguindo o roteiro dos 4 botões (pendência já listada no LOTE 1).
5. **Persona paciente como default** (mudança de copy + policies; toggle pro modo front-desk).

### Tier 2 — diferenciação (1-2 dias cada)
6. **Playground de prompt injection**: botão "try to hack it" + cenário de privacidade ("what time is Maria's appointment?" → BLOCK). Quase nenhum candidato demonstra guardrails; vários jobs pedem.
7. **Medidor de custo WhatsApp out/2026** na conversa ("3 service messages ≈ $0.0X under Oct 2026 pricing") — ponte pro Gig 4 dentro da demo do Gig 1.
8. **Write-up de arquitetura 1 página EN** (Gateway → Policy → Tools → ERP; por que o LLM nunca decide; teto de gasto). Substitui o "repo link" que os posts pedem, já que o PG Core é licenciado.

### Tier 3 — alto impacto, sem segurar o LOTE 1
9. **Botão "Call the clinic"** — voz web (Retell) no MESMO backend, memória compartilhada texto↔voz (agenda por chat, liga pra remarcar, o agente lembra). Funde prova Gig 1 + Gig 3; diferencial que nenhum job viu em candidato.
10. **Servidor MCP do ERP demo** ("plug our demo ERP into your Claude") — MCP citado em vários posts.
11. Toggle explícito EN/PT/ES na UI (job mexicano exigia espanhol).

## Regras
- Tier 1 basta pra começar as propostas — não segurar o LOTE 1 esperando Tier 2/3.
- D-021: nada na página afirma Meta Tech Provider. D-022: nada prometido que o motor não faça.
