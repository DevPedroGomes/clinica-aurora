"""O catálogo de textos do código: completo, coerente e sem português no inglês.

Antes desta mudança, só três frases seguiam o idioma da sessão. O resto do que o
código punha na conversa (prompt, bloco de contexto, ordem do gate, erros das
tools, dicas da política, schema das tools) era português fixo, e numa sessão em
inglês a confirmação às vezes voltava em português.
"""

from __future__ import annotations

import re
import string
from pathlib import Path

import pytest

from src import i18n
from src.detector_idioma import detectar
from src.policy import engine as engine_mod
from src.prompts.system import PROMPTS, system_blocks
from src.tools.registry import REGISTRY, schema_para_anthropic

ENGINE_SRC = Path(engine_mod.__file__).read_text("utf-8")


def _campos(modelo: str) -> set[str]:
    return {nome for _, nome, _, _ in string.Formatter().parse(modelo) if nome}


# -- o catálogo ---------------------------------------------------------------


def test_toda_chave_tem_os_dois_idiomas():
    ruins = [
        chave
        for chave, entrada in i18n.CATALOGO.items()
        if set(entrada) != {"pt", "en"} or not all(v.strip() for v in entrada.values())
    ]
    assert not ruins


def test_os_dois_idiomas_pedem_os_mesmos_campos():
    ruins = [
        chave
        for chave, entrada in i18n.CATALOGO.items()
        if _campos(entrada["pt"]) != _campos(entrada["en"])
    ]
    assert not ruins


def test_o_ingles_do_catalogo_nao_e_portugues():
    ruins = [
        chave
        for chave, entrada in i18n.CATALOGO.items()
        if detectar(entrada["en"]) == "pt" or detectar(entrada["pt"]) == "en"
    ]
    assert not ruins


def test_chave_desconhecida_e_erro_de_quem_escreveu():
    with pytest.raises(KeyError):
        i18n.texto("nao.existe", "pt")


def test_idioma_fora_da_lista_cai_no_portugues():
    assert i18n.texto("fala.confirmacao", "xx") == "Posso seguir?"
    assert i18n.texto("fala.confirmacao", "en") == "Shall I go ahead?"


def test_campos_sao_preenchidos():
    texto = i18n.texto("idioma.reescrever", "en", idioma="English")
    assert "entirely in English" in texto and "{" not in texto


@pytest.mark.parametrize(
    ("valor", "esperado"), [("en", "en"), ("pt", "pt"), ("xx", "pt"), (None, "pt")]
)
def test_normalizar(valor, esperado):
    assert i18n.normalizar(valor) == esperado


def test_nome_do_idioma_vem_na_lingua_da_sessao():
    assert i18n.nome_do_idioma("es", "en") == "Spanish"
    assert i18n.nome_do_idioma("es", "pt") == "espanhol"
    assert i18n.nome_do_idioma("en", "pt") == "inglês"
    # Idioma que a tabela não conhece: o da sessão, nunca um KeyError.
    assert i18n.nome_do_idioma("fr", "en") == "English"


# -- as dicas do motor de política -------------------------------------------


def test_toda_dica_do_motor_esta_no_catalogo():
    """O motor devolve a CHAVE da dica; uma chave nova sem texto quebra aqui."""
    chaves = set(re.findall(r'"(dica\.[a-z_]+)"', ENGINE_SRC))
    assert chaves, "o motor não devolve nenhuma chave de dica?"
    faltando = chaves - set(i18n.CATALOGO)
    assert not faltando


@pytest.mark.parametrize("idioma", ["pt", "en"])
def test_nenhuma_dica_vaza_a_regra(idioma: str):
    """A dica entra no contexto do modelo: número de horas ou horário não vai."""
    for chave, entrada in i18n.CATALOGO.items():
        if chave.startswith("dica."):
            assert not re.search(r"\d", entrada[idioma]), chave


# -- o schema das tools -------------------------------------------------------


def test_toda_tool_e_todo_campo_descrito_tem_traducao():
    for nome, spec in REGISTRY.items():
        traducao = i18n.SCHEMA_EN.get(nome)
        assert traducao and traducao.get(""), f"{nome} sem descrição em inglês"
        props = spec.input_model.model_json_schema().get("properties", {})
        for campo, prop in props.items():
            if "description" in prop:
                assert campo in traducao, f"{nome}.{campo} sem tradução"


def test_schema_em_ingles_nao_leva_portugues():
    for tool in schema_para_anthropic("en"):
        textos = [tool["description"]] + [
            p["description"]
            for p in tool["input_schema"].get("properties", {}).values()
            if "description" in p
        ]
        for t in textos:
            assert detectar(t) != "pt", (tool["name"], t)


def test_schema_em_portugues_e_o_do_decorator():
    for tool in schema_para_anthropic("pt"):
        assert tool["description"] == REGISTRY[tool["name"]].description


def test_os_dois_schemas_tem_a_mesma_forma():
    """Só o texto muda: nomes, tipos e domínios fechados são os mesmos."""

    def sem_descricao(schema):
        if isinstance(schema, dict):
            return {
                k: sem_descricao(v) for k, v in schema.items() if k != "description"
            }
        if isinstance(schema, list):
            return [sem_descricao(v) for v in schema]
        return schema

    pt = {
        t["name"]: sem_descricao(t["input_schema"]) for t in schema_para_anthropic("pt")
    }
    en = {
        t["name"]: sem_descricao(t["input_schema"]) for t in schema_para_anthropic("en")
    }
    assert pt == en


def test_traduzir_tool_desconhecida_fica_como_esta():
    descricao, schema = i18n.traduzir_tool("nao_existe", "x", {"type": "object"}, "en")
    assert descricao == "x" and schema == {"type": "object"}


# -- o prompt do sistema -------------------------------------------------------


def test_um_prompt_por_idioma_da_sessao():
    assert system_blocks("en")[0]["text"] == PROMPTS["en"]
    assert system_blocks("pt")[0]["text"] == PROMPTS["pt"]
    assert system_blocks("xx")[0]["text"] == PROMPTS["pt"]
    assert detectar(PROMPTS["en"]) == "en"
    assert detectar(PROMPTS["pt"]) == "pt"


def test_os_dois_prompts_tem_as_mesmas_secoes():
    secoes = {i: re.findall(r"^# ", p, re.MULTILINE) for i, p in PROMPTS.items()}
    assert len(secoes["pt"]) == len(secoes["en"]) >= 7


def test_o_prompt_aponta_para_a_linha_de_idioma_do_contexto():
    assert (
        '"reply language"' in PROMPTS["en"] and "<operational_context>" in PROMPTS["en"]
    )
    assert '"idioma da resposta"' in PROMPTS["pt"]
    assert "<contexto_operacional>" in PROMPTS["pt"]
