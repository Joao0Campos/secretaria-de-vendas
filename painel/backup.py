"""Backup diário do painel.db (opção B, 20/10/2026: o banco existe SÓ no servidor).

- Consistente: API de backup do SQLite (nunca a cópia do arquivo aberto), num
  temporário ao lado; PRAGMA integrity_check no arquivo gerado; só então vira
  painel_dados/backups/painel-AAAA-MM-DD.db. Guarda os GUARDAR_DIAS mais novos.
- Quando: uma thread do próprio serviço do painel confere a cada
  INTERVALO_DA_CONFERENCIA_S (e logo ao subir) se já passou das HORA_DO_BACKUP
  e o backup de hoje ainda não existe. Escolhida no lugar de "1ª requisição
  depois das 2h" (de madrugada pode não haver requisição nenhuma) e de uma
  tarefa do Windows (outra conta, outra configuração para a TI manter): vive e
  morre com o serviço, que o NSSM já reinicia, e recupera o backup perdido se
  o servidor estava desligado às 2h.
- Log: painel_dados/logs/backup.txt (uma linha por backup ou falha), e o
  último resultado em painel_dados/backups/ultimo_resultado.json, que o site
  mostra para o admin (estado(): idade do último backup bom, último resultado,
  espaço livre - alerta abaixo de 2 GB).
- Cópia fora do servidor: GET /api/executor/backup (chave do executor) devolve
  o mais recente, e o PC baixa um por dia (sincronizar_servidor.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

GUARDAR_DIAS = 14
HORA_DO_BACKUP = 2
INTERVALO_DA_CONFERENCIA_S = 600
ESPACO_MINIMO_GB = 2.0
BACKUP_VELHO_APOS_H = 36   # um por dia: mais que isso, algo deixou de rodar
RE_NOME = re.compile(r"painel-(\d{4}-\d{2}-\d{2})\.db")


class BackupCorrompido(RuntimeError):
    pass


def nome_do_dia(dia: date) -> str:
    return f"painel-{dia:%Y-%m-%d}.db"


def _somente_leitura(caminho: Path) -> str:
    return Path(caminho).resolve().as_uri() + "?mode=ro"  # espaços e acentos do caminho viram %XX


def integridade(caminho: Path) -> str:
    con = sqlite3.connect(_somente_leitura(caminho), uri=True)
    try:
        return str(con.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        con.close()


def fazer_backup(db: Path, pasta: Path, dia: date) -> Path:
    """Backup consistente de 'db' em pasta/painel-<dia>.db (já existe: devolve ele)."""
    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / nome_do_dia(dia)
    if destino.exists():
        return destino
    temporario = pasta / f".{destino.name}.{secrets.token_hex(4)}.parcial"
    try:
        origem = sqlite3.connect(_somente_leitura(db), uri=True, timeout=30)
        copia = sqlite3.connect(temporario)
        try:
            origem.backup(copia)
        finally:
            copia.close()
            origem.close()
        resultado = integridade(temporario)
        if resultado != "ok":
            raise BackupCorrompido(f"integrity_check: {resultado[:200]}")
        os.replace(temporario, destino)
        return destino
    finally:
        temporario.unlink(missing_ok=True)


def copiar_banco(db: Path, destino: Path) -> str:
    """Cópia consistente (API de backup) para 'destino', que não pode existir.
    Devolve o integrity_check da cópia ('ok' = boa). Migração e restauração."""
    destino = Path(destino)
    if destino.exists():
        raise FileExistsError(destino)
    origem = sqlite3.connect(_somente_leitura(db), uri=True, timeout=30)
    copia = sqlite3.connect(destino)
    try:
        origem.backup(copia)
    finally:
        copia.close()
        origem.close()
    return integridade(destino)


def backups(pasta: Path) -> list[Path]:
    """Os backups da pasta, do mais novo para o mais velho (pela data do nome)."""
    if not pasta.is_dir():
        return []
    return sorted((p for p in pasta.iterdir() if p.is_file() and RE_NOME.fullmatch(p.name)),
                  key=lambda p: p.name, reverse=True)


def rotacionar(pasta: Path, guardar: int = GUARDAR_DIAS) -> list[Path]:
    apagados = []
    for velho in backups(pasta)[guardar:]:
        velho.unlink(missing_ok=True)
        apagados.append(velho)
    return apagados


def sha256(caminho: Path) -> str:
    resumo = hashlib.sha256()
    with open(caminho, "rb") as f:
        for pedaco in iter(lambda: f.read(1 << 20), b""):
            resumo.update(pedaco)
    return resumo.hexdigest()


def _log(pasta_dados: Path, texto: str) -> None:
    try:
        logs = pasta_dados / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "backup.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {texto}\n")
    except OSError:
        pass


def _registrar_resultado(pasta: Path, ok: bool, mensagem: str) -> None:
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        temporario = pasta / f".ultimo_resultado.{secrets.token_hex(4)}.parcial"
        temporario.write_text(json.dumps({"em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ok": ok,
                                          "mensagem": mensagem[:300]}, ensure_ascii=False), encoding="utf-8")
        os.replace(temporario, pasta / "ultimo_resultado.json")
    except OSError:  # disco cheio: o site mostra o espaço livre e a idade do último bom
        pass


def estado(pasta_dados: Path, agora: datetime | None = None) -> dict:
    """Para o admin: o último backup bom e a idade dele, o último resultado e
    o espaço livre, com os alertas."""
    agora = agora or datetime.now()
    pasta = Path(pasta_dados) / "backups"
    lista = backups(pasta)
    ultimo = lista[0] if lista else None
    idade_h = None
    if ultimo is not None:
        try:
            idade_h = (agora - datetime.fromtimestamp(ultimo.stat().st_mtime)).total_seconds() / 3600
        except OSError:
            pass
    try:
        resultado = json.loads((pasta / "ultimo_resultado.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        resultado = None
    try:
        livre_gb = shutil.disk_usage(pasta_dados).free / 2**30
    except OSError:
        livre_gb = None
    alertas = []
    if ultimo is None:
        alertas.append("nenhum backup ainda")
    elif idade_h is not None and idade_h > BACKUP_VELHO_APOS_H:
        alertas.append(f"último backup bom há {idade_h:.0f} h")
    if resultado and not resultado.get("ok"):
        alertas.append(f"a última tentativa falhou: {resultado.get('mensagem', '')}")
    if livre_gb is not None and livre_gb < ESPACO_MINIMO_GB:
        alertas.append(f"pouco espaço: {livre_gb:.1f} GB livres")
    return {"ultimo": ultimo.name if ultimo else None,
            "idade_horas": round(idade_h, 1) if idade_h is not None else None,
            "ultimo_resultado": resultado, "livre_gb": round(livre_gb, 1) if livre_gb is not None else None,
            "alertas": alertas}


def rodar_se_pendente(db: Path, agora: datetime | None = None) -> Path | None:
    """Faz o backup de hoje se já passou das HORA_DO_BACKUP e ele não existe.
    Nunca levanta exceção: o resultado vai para o log."""
    agora = agora or datetime.now()
    pasta_dados = Path(db).parent
    pasta = pasta_dados / "backups"
    if agora.hour < HORA_DO_BACKUP or (pasta / nome_do_dia(agora.date())).exists():
        return None
    try:
        feito = fazer_backup(Path(db), pasta, agora.date())
        apagados = rotacionar(pasta)
        texto = (f"backup ok: {feito.name} ({feito.stat().st_size / 2**20:.1f} MB, integrity_check ok, "
                 f"sha256 {sha256(feito)[:12]}...); {len(apagados)} antigo(s) apagado(s), "
                 f"{len(backups(pasta))} guardado(s)")
        _log(pasta_dados, texto)
        _registrar_resultado(pasta, True, texto)
        return feito
    except Exception as exc:  # o painel continua no ar; amanhã (ou daqui a 10 min) tenta de novo
        _log(pasta_dados, f"backup FALHOU: {type(exc).__name__}: {exc}")
        _registrar_resultado(pasta, False, f"{type(exc).__name__}: {exc}")
        return None


class Agendador:
    """Thread do serviço do painel: confere ao subir e a cada intervalo."""

    def __init__(self, db: Path, intervalo_s: float = INTERVALO_DA_CONFERENCIA_S):
        self.db, self.intervalo_s = Path(db), intervalo_s
        self._parar = threading.Event()
        self._fio: threading.Thread | None = None

    def iniciar(self) -> None:
        def laco():
            while True:
                rodar_se_pendente(self.db)
                if self._parar.wait(self.intervalo_s):
                    return
        self._fio = threading.Thread(target=laco, name="backup-diario", daemon=True)
        self._fio.start()

    def parar(self) -> None:
        self._parar.set()
        if self._fio:
            self._fio.join(timeout=30)
