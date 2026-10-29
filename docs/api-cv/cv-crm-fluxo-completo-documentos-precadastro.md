# CV CRM - Fluxo Completo: Buscar Documentos de TODOS os Pré-Cadastros

**Solução para consumir documentos de múltiplos pré-cadastros**

---

## 📋 O Problema
Você tem um endpoint que funciona para UM pré-cadastro:
```
GET /api/v1/comercial/precadastro/{idPrecadastro}/documentos
```

Mas precisa de TODOS. A solução é encadear dois endpoints.

---

## ✅ Solução: Fluxo em 2 Passos

### Passo 1️⃣: Listar TODOS os Pré-Cadastros
```
GET /api/v1/prospectar/precadastro
```

**Headers:**
```
email: seu-email@empresa.com
token: seu-token-aqui
Accept: application/json
```

**Resposta esperada:**
```json
{
  "dados": {
    "precadastros": [
      {
        "idprecadastro": 12345,
        "nome": "João Silva",
        "situacao": "Documentos Pendentes",
        "data_criacao": "2026-08-20"
      },
      {
        "idprecadastro": 12346,
        "nome": "Maria Santos",
        "situacao": "Documentos Pendentes",
        "data_criacao": "2026-08-21"
      }
    ]
  }
}
```

**Parâmetros opcionais de filtro:**
- `situacao` — filtrar por status (ex: "Documentos Pendentes", "Aprovado")
- `data_inicio` — pré-cadastros criados a partir de
- `data_fim` — pré-cadastros criados até
- `page` — número da página (paginação)
- `limit` — quantidade por página

---

### Passo 2️⃣: Para Cada Pré-Cadastro, Buscar Documentos

Iterar sobre a lista do Passo 1 e chamar:
```
GET /api/v1/comercial/precadastro/{idPrecadastro}/documentos
```

Para `idPrecadastro = 12345`:
```
GET /api/v1/comercial/precadastro/12345/documentos
```

**Headers (mesmos do Passo 1):**
```
email: seu-email@empresa.com
token: seu-token-aqui
Accept: application/json
```

**Resposta:** (já você conhece este formato)
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
        "link": "https://.../arquivo.pdf?X-Amz-Signature=..."
      }
    ]
  }
}
```

---

## 🔄 Pseudocódigo (qualquer linguagem)

```javascript
// 1. Obter lista de pré-cadastros
const response1 = await fetch(
  'https://suaempresa.cvcrm.com.br/api/v1/prospectar/precadastro',
  {
    method: 'GET',
    headers: {
      'email': 'seu-email@empresa.com',
      'token': 'seu-token',
      'Accept': 'application/json'
    }
  }
);

const data1 = await response1.json();
const precadastros = data1.dados.precadastros;

// 2. Para cada pré-cadastro, obter documentos
const todosDocs = [];

for (const pc of precadastros) {
  const response2 = await fetch(
    `https://suaempresa.cvcrm.com.br/api/v1/comercial/precadastro/${pc.idprecadastro}/documentos`,
    {
      method: 'GET',
      headers: {
        'email': 'seu-email@empresa.com',
        'token': 'seu-token',
        'Accept': 'application/json'
      }
    }
  );

  const data2 = await response2.json();
  
  // Armazenar documentos com referência ao pré-cadastro
  todosDocs.push({
    idprecadastro: pc.idprecadastro,
    nome_cliente: pc.nome,
    documentos: data2.dados.documentos
  });
}

console.log(todosDocs);
```

---

## 🧪 Como Testar no Postman

### Request 1: Listar Pré-Cadastros
1. **Method:** GET
2. **URL:** `https://suaempresa.cvcrm.com.br/api/v1/prospectar/precadastro`
3. **Headers:**
   ```
   email: seu-email@empresa.com
   token: seu-token
   Accept: application/json
   ```
4. **Send** → Copie os `idprecadastro` da resposta

### Request 2: Buscar Documentos (para cada ID)
1. **Method:** GET
2. **URL:** `https://suaempresa.cvcrm.com.br/api/v1/comercial/precadastro/{{idPrecadastro}}/documentos`
   - Substitua `{{idPrecadastro}}` pelos IDs do Passo 1
3. **Headers:** (mesmas do Request 1)
4. **Send**

---

## 🔗 Endpoints Relacionados

| Nome | Método | URL | Uso |
|------|--------|-----|-----|
| **Listar Pré-Cadastros** | GET | `/api/v1/prospectar/precadastro` | ✅ Obter lista com filtros |
| **Buscar Documentos de PC** | GET | `/api/v1/comercial/precadastro/{id}/documentos` | ✅ Já testado com sucesso |
| **Retorna PC Pendentes** | GET | `/api/v1/prospectar/precadastro/pendentes` | Apenas pré-cadastros com docs pendentes |
| **Pré-Cadastro Pendentes** | GET | `/reference/retornarprecadastrospendentes` | Mesmo endpoint, slug alternativo |

---

## ⚙️ Otimizações Recomendadas

### 1. Filtrar apenas PC com documentos pendentes
```
GET /api/v1/prospectar/precadastro?situacao=Documentos%20Pendentes
```

### 2. Implementar paginação
```
GET /api/v1/prospectar/precadastro?page=1&limit=50
```

### 3. Respectar rate limit
- **200 requisições/minuto** para APIs padrão
- Se tiver 1000 pré-cadastros: 1 lista + 1000 documentos = ~1001 requisições
- Recomendação: processar em lotes com delay entre eles

### 4. Cache de resultados
- Armazene o mapeamento: `idprecadastro → data_ultima_consulta`
- Não reprocesse pré-cadastros consultados hoje

---

## ✅ Checklist de Implementação

- [ ] Testar `GET /api/v1/prospectar/precadastro` no Postman
- [ ] Confirmar response contém lista de `idprecadastro`
- [ ] Testar `GET /api/v1/comercial/precadastro/{id}/documentos` com múltiplos IDs
- [ ] Implementar loop em Node.js/Python
- [ ] Adicionar tratamento de erros e rate limiting
- [ ] Implementar paginação se houver muitos pré-cadastros
- [ ] Testar com carga real no seu ambiente
- [ ] Documentar endpoints no seu projeto

---

