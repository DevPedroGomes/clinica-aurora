# Servidor MCP da Clínica Aurora

Pluge o seu agente na agenda de uma clínica fictícia e tente quebrar as regras
dela. Você não vai conseguir, e o motivo é o ponto da demo.

```
https://aurora.pgdev.com.br/mcp
```

## O que isto é, e o que não é

Não é o ERP publicado como MCP. Se fosse, quem decidiria o que pode ser gravado
seria o modelo do outro lado, e isso não prova nada.

É o ERP **atrás do mesmo motor de política determinístico** que o agente da demo
web usa. O seu Claude, o seu Cursor ou o seu agente conecta e pode ler e
escrever, mas as regras do negócio continuam sendo decididas aqui:

> **o seu agente, as minhas regras, o meu log.**

## Ligando

Claude Code:

```bash
claude mcp add --transport http clinica-aurora https://aurora.pgdev.com.br/mcp
```

Claude Desktop ou Cursor, no arquivo de configuração de MCP:

```json
{
  "mcpServers": {
    "clinica-aurora": {
      "type": "http",
      "url": "https://aurora.pgdev.com.br/mcp"
    }
  }
}
```

Sem chave, sem cadastro. Cada conexão recebe a própria agenda semeada e nunca vê
a de outra pessoa.

## O que pedir, nesta ordem

1. **"Mostre a agenda da clínica."** Aquece: você vê os ids reais.
2. **"Remarque a consulta da Ana para as 16h."** A primeira chamada **não grava
   nada**: devolve a proposta e um token. O seu agente tem que te mostrar isso e
   chamar de novo com o token.
3. **"Cancele a consulta do João."** Ela é semeada a menos de 24 h, exceto de
   sábado às 10h30 a domingo às 8h. Sem motivo, a política **recusa**, e a
   resposta traz `cancel_window_requires_reason` mais a versão da regra. Repita
   informando o motivo e passa.
4. **"Agende para domingo às 3 da manhã."** Recusa por expediente.
5. **"Leia o log de decisão."** Toda chamada, toda decisão e toda escrita, com o
   código da regra que produziu cada uma.

## As tools

| Tool | O que faz |
|---|---|
| `buscar_paciente_tool` | Acha um paciente pelo nome e devolve o id. Nunca lista o cadastro |
| `listar_agenda_tool` | Lê a agenda. Sem filtro, devolve tudo, e só as linhas do `paciente_id` informado trazem o nome |
| `remarcar_consulta_tool` | Move uma consulta. Escrita |
| `cancelar_consulta_tool` | Cancela, registrando motivo. Escrita |
| `abrir_vaga_tool` | Abre horário livre. Escrita |
| `marcar_consulta_tool` | Marca para um paciente do cadastro. Escrita |
| `ler_politica` | As regras que este servidor aplica, com a versão |
| `ler_log_de_decisao` | O rastro desta sessão |

Toda tool de escrita aceita `confirmacao`. Sem ela, a chamada devolve a proposta
e um token, e **não grava**.

## O que é garantia, e o que é freio de mão

Esta distinção é o assunto da demo, então ela fica explícita.

**Garantia dura**, porque não depende do loop de ninguém:

- **A política decide.** Uma escrita fora da regra é recusada por este processo,
  com o código da regra e a versão do `rules.yaml` que valia na hora.
- **O domínio é fechado.** `consulta_id` casa `^cons_[0-9]+$`, paciente e
  profissional vêm de lista. Id inventado morre na validação, antes da política.
- **Há teto de escritas por sessão.** Não é regra da clínica: é freio contra
  agente em laço.
- **Nada é inventado.** Se o ERP não gravou, a tool devolve erro tipado.

**Freio de mão, não tranca:** o gate de confirmação. Na demo web, o loop é nosso
e dá para garantir que existiu um humano entre a proposta e a escrita. Aqui o
loop é do seu cliente, e um agente pode chamar duas vezes seguidas sozinho. O
token força a proposta a aparecer no transcrito antes da escrita, o que dá ao
humano a chance de intervir, mas não é o mesmo que garantir que ele interveio.

Está escrito assim porque a diferença entre as duas coisas é exatamente o que
esta demo existe para mostrar.

## Como roda

```
seu agente ──MCP/HTTP──▶ mcpsrv ──HTTP──▶ ERP (OpenAPI público)
                            │
                            ├── policy/   as regras, versionadas em YAML
                            └── tools/    6 operações, domínio fechado
```

`policy/`, `tools/` e `adapters/` são **compartilhados** com o agente da demo,
não copiados: uma regra que valesse no chat mas não no MCP seria pior do que não
ter MCP.

O processo é separado do agente por dependência: o SDK do MCP exige
`starlette>=0.49` e o agente roda `fastapi==0.115.6`, que exige `<0.42`. Os dois
não cabem no mesmo interpretador. O efeito colateral é que o código compartilhado
roda sob pydantic 2.13 aqui e 2.10 lá, e `tests/test_server.py` existe em parte
para vigiar essa diferença.

## Rodando local

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest==8.3.4 pytest-asyncio==0.25.0
ERP_URL=http://127.0.0.1:8080 MCP_ALLOWED_HOSTS='127.0.0.1:*' .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8091
.venv/bin/python -m pytest tests -q
```
