"""O funil do CV no painel (Parte C, 19/10/2026): reservas_cv.json, a listagem
que o varredura_diaria.py --atualizar-funil regrava a cada 15 min.

⚠️ Tudo aqui é SEGUNDO A LISTAGEM do CV (CVDW), que atrasa - às vezes horas. Em
13/10/2026, 9 reservas listadas como "Nova Reserva" já estavam canceladas. A
situação real só vale depois da busca direta, que a reanálise faz. Por isso a
tela diz "segundo a listagem do CV".

O site não importa o varredura_diaria (que traz o CV e a IA): a lista de etapas
é copiada aqui, e um teste garante que as duas são iguais.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# = varredura_diaria.STATUS_DA_VARREDURA (conferido em painel/tests/test_telas_reanalise.py).
STATUS_DO_FUNIL = (
    "Contrato Aguardando Validação",
    "Nova Reserva",
    "Reserva Aprovada",
    "Análise Gerencial",
    "Geração de Contrato",
)
_NO_FUNIL = {s.strip().lower() for s in STATUS_DO_FUNIL}


def no_funil(situacao: str | None) -> bool:
    return (situacao or "").strip().lower() in _NO_FUNIL


class Listagem:
    """reservas_cv.json (ao lado do banco), relido só quando o arquivo muda: são
    ~5 MB. A gravação dele é por troca atômica (varredura_diaria._gravar_listagem),
    então nunca é lido pela metade."""

    def __init__(self, pasta: Path):
        self.caminho = Path(pasta) / "reservas_cv.json"
        self._mtime: int | None = None
        self._reservas: dict[int, dict] = {}
        self._atualizado_em: datetime | None = None

    def _carregar(self) -> None:
        try:
            mtime = self.caminho.stat().st_mtime_ns
        except OSError:
            self._mtime, self._reservas, self._atualizado_em = None, {}, None
            return
        if mtime == self._mtime:
            return
        try:
            dados = json.loads(self.caminho.read_text(encoding="utf-8"))
            reservas = {int(k): v for k, v in (dados.get("reservas") or {}).items()
                        if str(k).isdigit() and isinstance(v, dict)}
            try:
                quando = datetime.strptime(str(dados.get("atualizado_em"))[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                quando = None
        except (OSError, ValueError, AttributeError):
            return  # fica com a última leitura boa
        self._mtime, self._reservas, self._atualizado_em = mtime, reservas, quando

    def ids(self) -> frozenset[int]:
        self._carregar()
        return frozenset(self._reservas)

    def situacao(self, idreserva: int) -> str | None:
        self._carregar()
        return str((self._reservas.get(idreserva) or {}).get("situacao") or "").strip() or None

    def atualizado_em(self) -> datetime | None:
        self._carregar()
        return self._atualizado_em

    def no_funil_sem_analise(self, analisadas: set[int]) -> list[dict]:
        """Reservas nas etapas do funil (segundo a listagem) que ainda não têm
        planilha. Só número, situação e modalidade: a listagem não tem nome, de
        propósito. Mais novas primeiro, como a varredura."""
        self._carregar()
        itens = [{"id": i, "situacao": str(r.get("situacao") or "").strip(),
                  "modalidade": str(r.get("tipovenda") or "").strip() or None}
                 for i, r in self._reservas.items() if no_funil(r.get("situacao")) and i not in analisadas]
        return sorted(itens, key=lambda x: x["id"], reverse=True)


def resumo_do_funil(listagem: Listagem) -> dict | None:
    """Para o topo das páginas: quando o funil foi atualizado."""
    quando = listagem.atualizado_em()
    if quando is None:
        return None
    return {"hhmm": quando.strftime("%H:%M"), "quando": quando.strftime("%d/%m %H:%M"),
            "hoje": quando.date() == datetime.now().date()}
