---
name: CV CRM - Buscar e Download de Documentos
description: Guia prático para implementar busca e download de documentos de reservas
type: reference
---

# CV CRM - Buscar e Download de Documentos de Reserva

Implementação independente de linguagem

## 1. Credenciais e Endpoints

### Headers obrigatórios em TODA requisição:
```
email: <e-mail do usuário integrador no CV>
token: <token gerado no CV para esse usuário>
Content-Type: application/json
```

### Dois endpoints principais:
- **API CVIO:** `/api/cvio/` — dados cadastrais (titular, associados)
- **API v1:** `/api/v1/` — dados operacionais (documentos)

**Exemplo:**
```
Host: suaempresa.cvcrm.com.br
API v1: https://suaempresa.cvcrm.com.br/api/v1/
API CVIO: https://suaempresa.cvcrm.com.br/api/cvio/
```

---

## 2. Fluxo em 4 Passos

### [1] GET /api/cvio/reserva/{reserva} → nomes (titular / 2º comprador)

**Resposta (objeto com chave = número da reserva):**
```json
{
  "12345": {
    "titular": { "nome": "FULANO DE TAL", "...": "..." },
    "associados": {
      "0": { "tipo": "2º Promissário Comprador", "nome": "CICLANA DE TAL" },
      "1": { "tipo": "Cônjuge", "nome": "..." }
    }
  }
}
```

**⚠️ Pontos de atenção:**
- Acesse `resposta["12345"]` — se a chave não existir, a reserva não existe (HTTP 200 mesmo assim!)
- `associados` é um OBJETO indexado por string, NÃO array
- Segundo comprador: `tipo === "2º Promissário Comprador"` (string exata com º)
- Esse passo só é necessário se você quer nomear arquivo por pessoa

---

### [2] GET /api/v1/comercial/reservas/{reserva}/documentos → metadados + links

**Resposta (envelope dados.documentos):**
```json
{
  "dados": {
    "documentos": {
      "titular": [
        {
          "idreservasdocumentos": 987654,
          "idtipo": 11,
          "nome": "RG frente e verso.pdf",
          "situacao": "Aprovado",
          "link": "https://.../arquivo.pdf?X-Amz-Signature=..."
        }
      ],
      "2o_promissario_comprador": [ /* mesma estrutura */ ],
      "conjuge": [ /* ... */ ]
    }
  }
}
```

**Campos usados:**
| Campo | Uso |
|-------|-----|
| `idreservasdocumentos` | ID único. **Maior = mais recente** (chave de deduplicação) |
| `idtipo` | Tipo do documento (tabela abaixo) |
| `nome` | Nome original (não confiável, mas usado em heurísticas) |
| `situacao` | Status. Só interessa **"Aprovado"** |
| `link` | URL assinada com querystring (validade curta!) |

**Tipos de documento (idtipo):**
| idtipo | Documento | Prefixo no arquivo |
|--------|-----------|-------------------|
| 5 | Certidão | CERTIDAO_ |
| 6 | Renda / avaliação | RENDA_AVALIACAO_ |
| 7 | Comprovante de residência | COMP_RESIDENCIA_ |
| 11 | Identidade | IDENTIDADE_ |
| 14 | Renda / avaliação | RENDA_AVALIACAO_ |
| 20 | Contrato | CONTRATO_ |

**⚠️ Notas:**
- Chaves observadas: `titular`, `2o_promissario_comprador` (SEM acento, SEM º — diferente do Passo 1!), e outras
- Trate grupos ausentes como lista vazia
- Qualquer outro `idtipo` é descartado

---

### [3] Filtrar / Deduplicar

**Regras aplicadas (nesta ordem):**

1. `situacao === "Aprovado"` — descarta pendentes e reprovados
2. `idtipo` está na lista de tipos aceitos
3. `nome` não contém "extrato" (case-insensitive)
4. **Deduplicação por tipo:** dentro do mesmo grupo de pessoa, se houver múltiplos documentos com mesmo `idtipo`, mantenha apenas o de maior `idreservasdocumentos`
5. Para 2º comprador: excluir `idtipo === 20` (contrato é único da reserva, já vem no grupo do titular)

**Pseudocódigo:**
```
para cada grupo em [titular, 2o_promissario_comprador]:
  docs = documentos[grupo] ou []
  docs = docs.filtra(d => 
    d.situacao == "Aprovado" 
    && d.idtipo in TIPOS_VALIDOS
    && "extrato" not in minusculas(d.nome)
  )
  
  se grupo != titular: 
    docs = docs.filtra(d => d.idtipo != 20)
  
  mapa = {}
  para cada d em docs:
    se mapa[d.idtipo] ausente ou d.idreservasdocumentos > mapa[d.idtipo].idreservasdocumentos:
      mapa[d.idtipo] = d
  
  processa(valores de mapa, nome_da_pessoa(grupo))
```

---

### [4] GET {doc.link} → Download binário

**⚠️ CRÍTICO:**
- Resposta é **BINÁRIA**. Configure cliente HTTP para buffer/bytes, NÃO string
  - Node: `responseType: "arraybuffer"`
  - Python: `requests.get(..., stream=True).content`
  - C#: `GetByteArrayAsync`
- Link tem **validade curta** — baixe logo após listar
- Envie os mesmos headers (`email`/`token`) no download

**Descobrir extensão:**
```javascript
url_sem_query = link.split("?")[0]
ext = extensão de url_sem_query  // ".pdf", ".jpg", ".docx", ".zip", ou ""
// Extensão vazia = PDF
```

---

## 3. Tratamento por Tipo de Arquivo

| Extensão | O que fazer |
|----------|-------------|
| `.pdf` ou vazia | Usar buffer como está |
| `.zip` | **Só para idtipo === 20 (contrato)**. Abrir ZIP, pegar primeira entrada `.pdf`, usar esse conteúdo. ZIP em outro tipo = descartado |
| `.jpg`, `.png`, `.docx`, etc | Converter para PDF (usar LibreOffice headless ou equivalente) |

---

## 4. Passo 5 (Opcional) — Descartar Holerite e Extrato

**Aplicável apenas a idtipo 6 e 14 (renda), em 2 camadas:**

### a) Pelo nome (antes de baixar — economiza rede):
Descartar se contiver: `holerite`, `contracheque`, `contra-cheque`, `contra cheque`, `contra ch`, `contrach`, `demonstrativo`, `folha pagamento`

### b) Pelo conteúdo (depois de baixar):
Extrair texto e procurar termos (usar PDF-parse ou equivalente)

1. Se não for imagem → tentar extração de texto do PDF
2. Se texto < 50 caracteres → é PDF escaneado ou imagem → cair para OCR
3. **Exceção:** Se conter `avaliação de risco`, `resultado da avaliação`, `código avaliação` → é documento viabilidade CAIXA/SIACI → **ACEITAR** mesmo que mencione "contracheque"
4. Descartar se contiver: `demonstrativo de pagamento`, `data de pagamento`, `holerite`, `contracheque`, `salário contratual`, `folha de pagamento`, `demonstrativo salarial`, `folha mensal`, `adiantamento salarial`, `horas normais`
5. Descartar se tiver `(proventos ou vencimentos)` AND `descontos` AND `(inss ou fgts ou líquido a receber)`
6. **Extrato:** Descartar se contiver `extrato de conta`, `extrato bancário`, `extrato consolidado`, `saldo anterior`; ou combinação `saldo + lançamentos`; ou `saldo + débito + crédito`

**Normalizar texto:** 
```javascript
texto.toLowerCase().replace(/\s+/g, " ").trim()
```

### OCR Pipeline (se necessário):
```bash
# ImageMagick + Tesseract
magick <arquivo>[0] -density 300 -quality 90 -colorspace Gray -auto-orient \
  [-rotate <graus>] -deskew 40% saida.png
tesseract saida.png saida -l por --psm 3
```

**Notas:**
- Para PDF: use `[0]` (primeira página) e `-density 300`
- Para imagem: sem `[0]` e sem `-density`
- Fotos vêm rotacionadas: tente 4 rotações (0, 90, 180, 270), mantenha maior comprimento de texto

---

## 5. Erros e Comportamento Observado

| Situação | Comportamento |
|----------|---------------|
| Reserva inexistente | HTTP 200 com corpo sem chave da reserva. **Sempre valide a chave**, não confie no status |
| Grupo de pessoa ausente | Chave simplesmente não vem em documentos. Trate como lista vazia |
| Link expirado | Erro no download (não na listagem). Baixe imediatamente após listar |
| Falha em um documento | Não aborte. Registre em `erros[]` e segue para próximo |
| Arquivo temporário | Sempre limpe em `finally`, inclusive em caminhos de erro |

---

## 6. Formato de Retorno Recomendado

```json
{
  "importados": [
    { "nome": "IDENTIDADE_FULANO DE TAL.pdf", "idDocumento": 42 }
  ],
  "ignorados": [
    { "nome": "holerite.pdf", "motivo": "holerite/demonstrativo identificado pelo conteúdo" }
  ],
  "erros": [
    { "nome": "foto.jpg", "erro": "..." }
  ]
}
```

---

## 7. Checklist de Implementação

- [ ] Configurar host, email e token como segredo (env/cofre)
- [ ] GET `/api/cvio/reserva/{reserva}` → ler `resposta[reserva].titular.nome` e varrer `associados` procurando `tipo == "2º Promissário Comprador"`
- [ ] GET `/api/v1/comercial/reservas/{reserva}/documentos` → ler `dados.documentos.titular` e `dados.documentos["2o_promissario_comprador"]`
- [ ] Filtrar por `situacao == "Aprovado"` + `idtipo` válido + nome sem "extrato"
- [ ] Deduplicar por `idtipo`, mantendo maior `idreservasdocumentos`
- [ ] Excluir `idtipo == 20` do segundo comprador
- [ ] Para cada documento: GET link em modo **binário**, com mesmos headers
- [ ] Resolver extensão a partir de `link.split("?")[0]`; ZIP só para tipo 20
- [ ] Converter não-PDF para PDF
- [ ] (Opcional) Aplicar heurísticas de holerite/extrato nos tipos 6 e 14
- [ ] Nomear arquivo: `<PREFIXO><nome sanitizado>.pdf` (sanitizar: remove acentos NFD + diacríticos, mantém `[a-zA-Z0-9 _-]`)
- [ ] Garantir **idempotência**: registre reservas já importadas, não reprocesse
