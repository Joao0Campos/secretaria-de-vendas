# CV CRM - Endpoint: Retorna os Documentos do Pré-cadastro

**Este é o endpoint que você procurava!** ✅

## 📋 Informações Básicas

- **Nome:** Retorna os Documentos do Pré-cadastro
- **Método HTTP:** GET
- **URL da API:** `https://{dominiodocliente}.cvcrm.com.br/api/v1/comercial/precadastro/{idPrecadastro}/documentos`
- **Exemplo:** `https://suaempresa.cvcrm.com.br/api/v1/comercial/precadastro/{idPrecadastro}/documentos`
- **Documentação:** https://desenvolvedor.cvcrm.com.br/reference/retornardocumento-3

---

## 🔐 Autenticação

**Tipo:** Email + Token (v1)

**Headers obrigatórios:**
```
email: <e-mail do usuário integrador no CV>
token: <token gerado no CV para esse usuário>
Accept: application/json
```

---

## 📤 Requisição

### Método
```
GET
```

### URL Pattern
```
/api/v1/comercial/precadastro/{idPrecadastro}/documentos
```

### Parâmetros

| Parâmetro | Tipo | Localização | Obrigatório | Descrição |
|-----------|------|-------------|-------------|-----------|
| idPrecadastro | integer | Path | ✅ Sim | ID do pré-cadastro |

### Exemplo cURL
```bash
curl --request GET \
  --url https://suaempresa.cvcrm.com.br/api/v1/comercial/precadastro/{idPrecadastro}/documentos \
  --header 'email: seu-email@empresa.com' \
  --header 'token: seu-token-aqui' \
  --header 'accept: application/json'
```

---

## 📥 Resposta

### Status HTTP
- **200 OK** — Documentos retornados com sucesso
- **400 Bad Request** — Parâmetro inválido
- **401 Unauthorized** — Autenticação falhou
- **404 Not Found** — Pré-cadastro não encontrado

### Estrutura da Resposta (200 OK)

```json
{
  "dados": {
    "documentos": [
      {
        "iddocumento": 12345,
        "idtipo": 11,
        "nome": "RG frente e verso.pdf",
        "situacao": "Aprovado",
        "dataenvio": "2026-08-24T10:30:00",
        "link": "https://.../arquivo.pdf?X-Amz-Signature=...",
        "observacoes": "Documento válido"
      }
    ]
  }
}
```

### Campos da Resposta

| Campo | Tipo | Descrição |
|-------|------|-----------|
| iddocumento | integer | ID único do documento |
| idtipo | integer | Tipo do documento (ver tabela abaixo) |
| nome | string | Nome original do arquivo |
| situacao | string | Status: "Aprovado", "Pendente", "Reprovado" |
| dataenvio | string (ISO 8601) | Data e hora do envio |
| link | string | URL assinada para download (validade curta) |
| observacoes | string | Notas adicionais |

### Tipos de Documento (idtipo)

| idtipo | Documento | Prefixo |
|--------|-----------|---------|
| 5 | Certidão | CERTIDAO_ |
| 6 | Renda / Avaliação | RENDA_AVALIACAO_ |
| 7 | Comprovante de Residência | COMP_RESIDENCIA_ |
| 11 | Identidade (RG/CNH) | IDENTIDADE_ |
| 14 | Renda / Avaliação | RENDA_AVALIACAO_ |
| 20 | Contrato | CONTRATO_ |

---

## 💡 Casos de Uso

### 1. Listar documentos de um pré-cadastro
```javascript
// Node.js example
const fetch = require('node-fetch');

const idPrecadastro = 12345;
const response = await fetch(
  `https://suaempresa.cvcrm.com.br/api/v1/comercial/precadastro/${idPrecadastro}/documentos`,
  {
    method: 'GET',
    headers: {
      'email': 'seu-email@empresa.com',
      'token': 'seu-token',
      'Accept': 'application/json'
    }
  }
);

const data = await response.json();
console.log(data.dados.documentos);
```

### 2. Filtrar por situação
```javascript
const documentos = data.dados.documentos;
const aprovados = documentos.filter(doc => doc.situacao === 'Aprovado');
const pendentes = documentos.filter(doc => doc.situacao === 'Pendente');
```

### 3. Agrupar por tipo
```javascript
const porTipo = {};
documentos.forEach(doc => {
  if (!porTipo[doc.idtipo]) porTipo[doc.idtipo] = [];
  porTipo[doc.idtipo].push(doc);
});
```

---

## ⚠️ Pontos Importantes

1. **Link com validade curta** — Baixe os documentos imediatamente após listar. Não persista o link.
2. **Autenticação obrigatória** — Email e token são requeridos em TODA requisição.
3. **Rate limit** — 200 requisições/minuto (APIs padrão)
4. **Resposta binária** — Se baixar arquivo, configure cliente HTTP para `arraybuffer` (não string)
5. **Grupo opcional** — Se grupo ausente, retorna lista vazia (não erro)

---

## 🔄 Fluxo Completo: Do Pré-cadastro aos Documentos

```
1. Usuário faz pré-cadastro no CV
2. GET /api/v1/prospectar/precadastro/{id} 
   → Obter dados do pré-cadastro
3. GET /api/v1/comercial/precadastro/{idPrecadastro}/documentos 
   → ✅ VOCÊ ESTÁ AQUI - Listar documentos
4. GET {doc.link} 
   → Download de cada documento
5. Validar/Processar documentos
6. PUT - Atualizar status do pré-cadastro
```

---

## 📞 Endpoints Relacionados

Outras operações sobre documentos de pré-cadastro:

| Nome | Método | URL |
|------|--------|-----|
| **Retorna os Documentos** | GET | `/api/v1/comercial/precadastro/{id}/documentos` |
| **Cadastra Documentos** | POST | `/reference/cadastrardocumento-3` |
| **Altera Documentos** | POST | `/reference/aprovardocumento-1` |
| **Pré-cadastros Pendentes** | GET | `/reference/retornarprecadastrospendentes` |

---

## 🛠️ Testar no Postman

1. **Method:** GET
2. **URL:** `https://suaempresa.cvcrm.com.br/api/v1/comercial/precadastro/12345/documentos`
3. **Headers:**
   ```
   email: seu-email@empresa.com
   token: seu-token
   Accept: application/json
   ```
4. **Send**

---

## 📝 Checklist de Implementação

- [ ] Configurar email e token nos headers
- [ ] Definir o idPrecadastro que deseja consultar
- [ ] Fazer GET request para `/api/v1/comercial/precadastro/{idPrecadastro}/documentos`
- [ ] Processar resposta JSON
- [ ] Iterar sobre `dados.documentos`
- [ ] Para cada documento: validar `situacao === "Aprovado"`
- [ ] Se precisar baixar: usar `doc.link` com headers (validade curta!)
- [ ] Implementar tratamento de erros (400, 401, 404)

---

## 🔗 Referência Completa

**Portal:** https://desenvolvedor.cvcrm.com.br
**Documentação:** https://desenvolvedor.cvcrm.com.br/reference/retornardocumento-3
**Suporte:** support@cvcrm.com.br
