"""A imagem do MCP leva só o que o Dockerfile copia, e o servidor sobe com isso.

Regressão do #8: `tools/` e `policy/` passaram a importar `src/i18n.py`, o
Dockerfile não copiava esse arquivo, e o MCP caiu em produção com
`ModuleNotFoundError`. A suíte não viu porque roda sobre a árvore inteira do
repo, onde o arquivo existe.

Este teste lê as instruções COPY do estágio final de `mcpsrv/Dockerfile`, monta
numa pasta temporária exatamente esse conjunto de arquivos (respeitando o
`.dockerignore`) e importa o alvo do CMD num interpretador isolado (`-I`, sem o
diretório do repo no path). Um import que saia do conjunto quebra aqui, no CI,
antes da imagem.
"""

from __future__ import annotations

import glob
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
DOCKERFILE = RAIZ / "mcpsrv" / "Dockerfile"

# Roda no interpretador isolado: só a pasta montada entra no path, como o
# uvicorn faz com o WORKDIR no container.
IMPORTAR = """
import importlib, sys
pasta, modulo, atributo = sys.argv[1:4]
sys.path.insert(0, pasta)
m = importlib.import_module(modulo)
getattr(m, atributo)
fora = sorted(
    nome for nome, mod in list(sys.modules.items())
    if nome.split(".")[0] in {"src", "mcpsrv"}
    and not str(getattr(mod, "__file__", "") or pasta).startswith(pasta)
)
if fora:
    raise SystemExit(f"modulos de fora da imagem: {fora}")
"""


def _instrucoes(texto: str) -> list[tuple[str, str]]:
    """(INSTRUÇÃO, argumentos), com continuação de linha e comentários resolvidos."""
    linhas: list[str] = []
    atual = ""
    for bruta in texto.splitlines():
        linha = bruta.strip()
        if not atual and (not linha or linha.startswith("#")):
            continue
        if linha.endswith("\\"):
            atual += linha[:-1] + " "
            continue
        linhas.append(atual + linha)
        atual = ""
    saida = []
    for linha in linhas:
        nome, _, resto = linha.partition(" ")
        saida.append((nome.upper(), resto.strip()))
    return saida


def _estagio_final(instrucoes: list[tuple[str, str]]) -> list[tuple[str, str]]:
    inicio = max(i for i, (nome, _) in enumerate(instrucoes) if nome == "FROM")
    return instrucoes[inicio + 1 :]


def _argumentos(resto: str) -> list[str]:
    if resto.startswith("["):
        return list(json.loads(resto))
    return shlex.split(resto)


def ler_dockerfile(texto: str) -> tuple[list[tuple[list[str], str, str]], str]:
    """As cópias do estágio final (fontes, destino, WORKDIR da hora) e o CMD."""
    copias: list[tuple[list[str], str, str]] = []
    workdir = "/"
    cmd = ""
    for nome, resto in _estagio_final(_instrucoes(texto)):
        if nome == "WORKDIR":
            workdir = str(Path(workdir) / resto)
        elif nome in {"COPY", "ADD"}:
            args = _argumentos(resto)
            if any(a.startswith("--from") for a in args):
                continue  # vem de outro estágio, não do repo
            caminhos = [a for a in args if not a.startswith("--")]
            copias.append((caminhos[:-1], caminhos[-1], workdir))
        elif nome == "CMD":
            cmd = resto
    return copias, cmd


def alvo_do_cmd(cmd: str) -> tuple[str, str]:
    """O `modulo:atributo` que o uvicorn sobe."""
    args = _argumentos(cmd)
    assert args and args[0] == "uvicorn", f"CMD inesperado: {cmd}"
    alvo = next(a for a in args[1:] if re.fullmatch(r"[\w.]+:\w+", a))
    modulo, atributo = alvo.split(":")
    return modulo, atributo


def _ignorados() -> list[str]:
    arquivo = RAIZ / ".dockerignore"
    linhas = arquivo.read_text("utf-8").splitlines() if arquivo.exists() else []
    return [
        linha.strip().rstrip("/")
        for linha in linhas
        if linha.strip() and not linha.startswith("#")
    ]


def montar(copias: list[tuple[list[str], str, str]], raiz_da_imagem: Path) -> None:
    """Copia para `raiz_da_imagem` o que cada COPY levaria para a imagem."""
    ignorar = shutil.ignore_patterns(*_ignorados())
    for fontes, destino, workdir in copias:
        absoluto = destino if destino.startswith("/") else f"{workdir}/{destino}"
        alvo = raiz_da_imagem / absoluto.lstrip("/")
        achadas = [Path(c) for f in fontes for c in sorted(glob.glob(str(RAIZ / f)))]
        assert achadas, f"COPY sem fonte no repo: {fontes}"
        em_pasta = destino.endswith("/") or len(achadas) > 1
        for fonte in achadas:
            if fonte.is_dir():
                # Docker copia o CONTEÚDO da pasta para o destino.
                shutil.copytree(fonte, alvo, ignore=ignorar, dirs_exist_ok=True)
            else:
                final = alvo / fonte.name if em_pasta else alvo
                final.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fonte, final)


def importar(pasta: Path, modulo: str, atributo: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-c", IMPORTAR, str(pasta), modulo, atributo],
        cwd=pasta,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _workdir_final(copias: list[tuple[list[str], str, str]]) -> str:
    return copias[-1][2]


def test_servidor_importa_so_com_os_arquivos_que_o_dockerfile_copia(tmp_path):
    copias, cmd = ler_dockerfile(DOCKERFILE.read_text("utf-8"))
    modulo, atributo = alvo_do_cmd(cmd)
    montar(copias, tmp_path)
    pasta = tmp_path / _workdir_final(copias).lstrip("/")

    r = importar(pasta, modulo, atributo)

    assert r.returncode == 0, r.stderr[-3000:]


def test_sem_o_catalogo_de_idiomas_o_import_quebra(tmp_path):
    """A trava funciona: tirar o COPY do i18n.py é o #8, e tem de falhar aqui."""
    texto = DOCKERFILE.read_text("utf-8")
    sem_i18n = "\n".join(
        linha
        for linha in texto.splitlines()
        if not (linha.strip().startswith("COPY") and "i18n.py" in linha)
    )
    assert sem_i18n != texto, "o Dockerfile não tem mais o COPY do i18n.py"
    copias, cmd = ler_dockerfile(sem_i18n)
    montar(copias, tmp_path)
    pasta = tmp_path / _workdir_final(copias).lstrip("/")

    r = importar(pasta, *alvo_do_cmd(cmd))

    assert r.returncode != 0
    assert "No module named 'src.i18n'" in r.stderr


@pytest.mark.parametrize(
    ("instrucao", "esperado"),
    [
        ("COPY a.py b.py ./x/", (["a.py", "b.py"], "./x/")),
        ('COPY ["a.py", "./x/a.py"]', (["a.py"], "./x/a.py")),
        ("COPY --chown=1000:1000 pasta ./pasta", (["pasta"], "./pasta")),
    ],
)
def test_leitura_das_formas_de_copy(instrucao, esperado):
    copias, _ = ler_dockerfile(
        f"FROM x AS a\nCOPY --from=a /y /y\nWORKDIR /app\n{instrucao}\n"
    )
    assert copias == [(*esperado, "/app")]
