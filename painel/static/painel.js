// Secretaria de Vendas VIC - interações das telas (abas, linhas clicáveis, tratativa).
(function () {
  "use strict";

  const csrf = document.querySelector('meta[name="csrf"]')?.content || "";
  const ROTULO = {
    aberto: "Em aberto", corrigido: "Corrigido", ignorado: "Ignorado",
    resolvido_na_reanalise: "Resolvido na reanálise",
  };
  const ROTULO_VEREDITO = { real: "Problema real", falso: "Aviso errado", nao_sei: "Dúvida" };
  const SAI = "o apontamento sai dos pendentes e ";
  // Cada resposta do veredito guiado: o que é gravado (status e motivo fixos,
  // que métricas e Revisão de regras leem), o que acontece e o exemplo do campo.
  const MOTIVO_OUTRO = "Outro: ";
  const ESCOLHAS = {
    "real:corrigido": { status: "corrigido", motivo: null, obrigatorio: false,
      efeito: SAI + "conta como corrigido.", exemplo: "Ex.: pedi ao corretor um comprovante novo" },
    "real:fora_do_cv": { status: "ignorado", motivo: "Corrigido fora do CV", obrigatorio: false,
      efeito: SAI + "fica registrado que já foi corrigido fora do CV.", exemplo: "Ex.: o cadastro foi ajustado direto no sistema do banco" },
    "falso:ia": { status: "ignorado", motivo: "Falso positivo da IA", obrigatorio: true,
      efeito: SAI + "conta como erro de leitura da IA na Revisão de regras.", exemplo: "Ex.: a IA leu a data de emissão como validade" },
    "falso:regra": { status: "ignorado", motivo: "Regra não se aplica a este caso", obrigatorio: true,
      efeito: SAI + "conta na Revisão de regras como regra que não vale neste caso.", exemplo: "Ex.: a certidão de casamento já está na pasta do cônjuge" },
    "falso:outro": { status: "ignorado", motivo: null, obrigatorio: true,
      efeito: SAI + "fica registrado com o motivo que você escrever.", exemplo: "Ex.: o documento é de uma modalidade que não exige isso" },
    "nao_sei": { status: "aberto", motivo: null, obrigatorio: true,
      efeito: "fica em aberto, com a sua dúvida registrada.", exemplo: "Ex.: a foto está cortada, não sei se o número é 5 ou 6" },
  };

  // Linhas da lista: a linha inteira abre a reserva.
  document.querySelectorAll("tr.link[data-href]").forEach((tr) => {
    tr.addEventListener("click", (e) => {
      if (e.target.closest("a, button")) return;
      window.location.href = tr.dataset.href;
    });
  });

  // Filtros da lista: trocar a opção já filtra (sem JS inline, por causa da CSP).
  document.querySelectorAll("select[data-envia-ao-mudar]").forEach((s) => {
    s.addEventListener("change", () => s.form && s.form.submit());
  });

  // Blocos recolhíveis (<details data-lembrar>): o navegador guarda se ficou
  // fechado, para não reabrir a cada filtro. Sem storage, abre como no HTML.
  document.querySelectorAll("details[data-lembrar]").forEach((d) => {
    const chave = "recolhido:" + d.dataset.lembrar;
    try { if (localStorage.getItem(chave) === "1") d.open = false; } catch (e) { /* sem storage */ }
    d.addEventListener("toggle", () => {
      try { localStorage.setItem(chave, d.open ? "0" : "1"); } catch (e) { /* sem storage */ }
    });
  });

  // Abas de pessoa no detalhe.
  const abas = [...document.querySelectorAll('.tabs [role="tab"]')];
  function abrirAba(aba) {
    abas.forEach((a) => {
      const on = a === aba;
      a.setAttribute("aria-selected", on ? "true" : "false");
      const painel = document.getElementById(a.getAttribute("aria-controls"));
      if (painel) painel.hidden = !on;
    });
  }
  abas.forEach((aba, i) => {
    aba.addEventListener("click", () => abrirAba(aba));
    aba.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      const prox = abas[(i + (e.key === "ArrowRight" ? 1 : abas.length - 1)) % abas.length];
      abrirAba(prox);
      prox.focus();
    });
  });

  // Tratativa de apontamento.
  function el(tag, attrs, texto) {
    const n = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => n.setAttribute(k, v));
    if (texto != null) n.textContent = texto;
    return n;
  }

  function aplicar(ap, a) {
    ap.dataset.status = a.status;
    ap.classList.toggle("resolvido", a.status !== "aberto");

    const acoes = ap.querySelector(".acoes");
    acoes.replaceChildren();
    if (a.status === "aberto") {
      acoes.append(el("button", { type: "button", class: "btn small primary", "data-acao": "analisar" }, "Analisar este apontamento"));
    } else if (a.status === "corrigido" || a.status === "ignorado") {
      acoes.append(el("button", { type: "button", class: "btn small", "data-acao": "aberto" }, "Reabrir"));
    }

    const meta = ap.querySelector(".meta");
    const erro = meta.querySelector(".erro-inline");
    meta.replaceChildren(el("span", { class: "st st-" + a.status }, ROTULO[a.status] || a.status));
    if (a.veredito) meta.append(el("span", { class: "vd vd-" + a.veredito }, ROTULO_VEREDITO[a.veredito] || a.veredito));
    if (a.motivo) meta.append(el("span", {}, "Motivo: " + a.motivo));
    if (a.detalhe && a.motivo !== MOTIVO_OUTRO + a.detalhe) meta.append(el("span", { class: "porque" }, "“" + a.detalhe + "”"));
    if (a.responsavel) meta.append(el("span", {}, "por " + a.responsavel));
    if (a.atualizado_em) meta.append(el("span", {}, "em " + a.atualizado_em));
    erro.hidden = true;
    meta.append(erro);
    fecharForm(ap.querySelector("form.veredito"));
  }

  // Escolha atual do formulário em passos: {chave, escolha, texto, completo}.
  // O passo 2 só existe para real e falso; o passo 3 só depois dele.
  function lerForm(form) {
    const veredito = (form.querySelector('input[name="veredito"]:checked') || {}).value;
    let chave = veredito === "nao_sei" ? "nao_sei" : null;
    if (veredito === "real" || veredito === "falso") {
      const acao = form.querySelector('input[name="acao_' + veredito + '"]:checked');
      if (acao) chave = veredito + ":" + acao.value;
    }
    const escolha = chave ? ESCOLHAS[chave] : null;
    const texto = form.detalhe.value.trim();
    // "Outro: <texto>" tem de caber no motivo (500), por isso o limite menor.
    const limite = chave === "falso:outro" ? 500 - MOTIVO_OUTRO.length : 500;
    const completo = !!escolha && texto.length <= limite && (!escolha.obrigatorio || texto !== "");
    return { veredito, chave, escolha, texto, limite, completo };
  }

  function atualizarForm(form) {
    const e = lerForm(form);
    form.querySelectorAll("fieldset[data-para]").forEach((f) => { f.hidden = f.dataset.para !== e.veredito; });
    form.querySelector('[data-passo="3"]').hidden = !e.escolha;
    const efeito = form.querySelector(".efeito");
    efeito.hidden = !e.escolha;
    if (e.escolha) {
      efeito.querySelector("span").textContent = e.escolha.efeito;
      form.detalhe.placeholder = e.escolha.exemplo;
      form.detalhe.maxLength = e.limite;
      form.querySelector(".obrig").textContent = e.escolha.obrigatorio ? "(obrigatório)" : "(opcional)";
    }
    form.querySelector('button[type="submit"]').disabled = !e.completo;
  }

  function fecharForm(form) {
    form.reset();
    form.hidden = true;
    atualizarForm(form);
  }

  async function enviar(ap, corpo) {
    const erro = ap.querySelector(".erro-inline");
    const botoes = ap.querySelectorAll("button");
    botoes.forEach((b) => (b.disabled = true));
    try {
      const resp = await fetch("/api/tratativa", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ chave: ap.dataset.chave, motivo: null, detalhe: null, veredito: null, ...corpo }),
      });
      if (resp.status === 401 || resp.redirected) {
        window.location.href = "/login?next=" + encodeURIComponent(location.pathname);
        return;
      }
      const corpo = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(corpo.detail || "Não foi possível salvar (erro " + resp.status + ").");
      aplicar(ap, corpo);
    } catch (e) {
      erro.textContent = e.message + " Tente de novo.";
      erro.hidden = false;
    } finally {
      botoes.forEach((b) => (b.disabled = false));
      atualizarForm(ap.querySelector("form.veredito"));
    }
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-acao]");
    if (!b) return;
    const ap = b.closest(".ap");
    if (!ap) return;
    const acao = b.dataset.acao;
    const form = ap.querySelector("form.veredito");
    if (acao === "analisar") { form.hidden = false; form.querySelector('input[name="veredito"]').focus(); return; }
    if (acao === "cancelar") { fecharForm(form); return; }
    enviar(ap, { status: acao });
  });

  document.addEventListener("change", (e) => {
    const form = e.target.closest("form.veredito");
    if (form) atualizarForm(form);
  });
  document.addEventListener("input", (e) => {
    const form = e.target.closest("form.veredito");
    if (form) atualizarForm(form);
  });

  document.addEventListener("submit", (e) => {
    const form = e.target.closest("form.veredito");
    if (!form) return;
    e.preventDefault();
    const ap = form.closest(".ap");
    const { veredito, chave, escolha, texto, completo } = lerForm(form);
    if (!completo) return;
    // O motivo fica fixo (é o que o Impacto agrupa); o "por quê" vai à parte e
    // alimenta a Revisão de regras. Em Outro, o texto já é o próprio motivo.
    const motivo = chave === "falso:outro" ? MOTIVO_OUTRO + texto : escolha.motivo;
    enviar(ap, { status: escolha.status, motivo, detalhe: texto || null, veredito });
  });

  // Reanálise ("Analisar agora" na lista, "Reanalisar agora" na reserva). O
  // botão só grava o pedido na fila (POST /api/reanalise); quem analisa é o
  // executor. A tela consulta GET /api/reanalise/{id} a cada 5 s enquanto o
  // pedido está aberto e recarrega quando ele termina com análise nova.
  const ABERTOS = ["na_fila", "em_andamento"];
  const hhmm = (t) => (t || "").slice(11, 16);
  const SESSAO_EXPIRADA = "Sessão expirada, entre de novo.";

  // Consulta periódica (reanálise e busca). Rede fora e 5xx são transitórios:
  // tenta de novo com espera dobrando até 60 s. 401: sessão expirada; outro
  // 4xx: o detail. Nos dois, para e chama falhou(texto, true) para liberar o
  // botão. receber(estado) devolve true para continuar consultando.
  function sondar(url, receber, falhou) {
    let espera = 5000;
    async function consultar() {
      let resp = null;
      try {
        resp = await fetch(url, { headers: { Accept: "application/json" } });
      } catch (_) {
        resp = null;
      }
      const estado = resp && resp.ok ? await resp.json().catch(() => null) : null;
      if (!resp || resp.status >= 500 || (resp.ok && estado === null)) {
        espera = Math.min(espera * 2, 60000);
        falhou("Sem resposta do painel; tentando de novo em " + Math.round(espera / 1000) + " s.", false);
        setTimeout(consultar, espera);
        return;
      }
      if (!resp.ok) {
        const corpo = await resp.json().catch(() => ({}));
        falhou(resp.status === 401 ? SESSAO_EXPIRADA
          : corpo.detail || "Não foi possível consultar (erro " + resp.status + ").", true);
        return;
      }
      espera = 5000;
      if (receber(estado)) setTimeout(consultar, espera);
    }
    consultar();
  }

  function textoDoPedido(e) {
    const p = e.pedido;
    if (!p) return "";
    if (p.status === "na_fila") {
      return "Na fila" + (e.posicao ? " (" + e.posicao + "º)" : "") + (e.aguardando ? " · " + e.aguardando : "");
    }
    if (p.status === "em_andamento") return "Em andamento desde " + hhmm(p.iniciado_em);
    if (p.status === "concluido") return "Concluído às " + hhmm(p.concluido_em);
    if (p.status === "fora_do_funil") return p.resultado || "Saiu do funil";
    if (p.status === "falhou") return "Falhou às " + hhmm(p.concluido_em) + ": " + (p.erro || "sem detalhe");
    return p.status;
  }

  function mostrarPedido(caixa, e) {
    const st = caixa.querySelector(".reanalise-status");
    const aberto = !!(e.pedido && ABERTOS.includes(e.pedido.status));
    st.textContent = textoDoPedido(e);
    st.classList.toggle("erro", !!(e.pedido && e.pedido.status === "falhou"));
    caixa.querySelector("button[data-reanalisar]").disabled = aberto;
    return aberto;
  }

  function acompanhar(caixa, jaAberto) {
    let viuAberto = jaAberto;
    sondar("/api/reanalise/" + caixa.dataset.reanalise, (e) => {
      if (mostrarPedido(caixa, e)) {
        viuAberto = true;
        return true;
      }
      if (viuAberto && e.pedido && ["concluido", "fora_do_funil"].includes(e.pedido.status)) {
        window.location.reload();
      }
      return false;
    }, (texto, definitivo) => {
      if (!viuAberto && !definitivo) return;  // a lista inteira consulta ao abrir: sem ruído
      const st = caixa.querySelector(".reanalise-status");
      st.textContent = texto;
      st.classList.add("erro");
      if (definitivo) caixa.querySelector("button[data-reanalisar]").disabled = false;
    });
  }

  document.querySelectorAll("[data-reanalise]").forEach((caixa) => acompanhar(caixa, false));

  document.addEventListener("click", async (ev) => {
    const b = ev.target.closest("button[data-reanalisar]");
    if (!b) return;
    ev.stopPropagation();
    const caixa = b.closest("[data-reanalise]");
    const st = caixa.querySelector(".reanalise-status");
    b.disabled = true;
    let resp;
    try {
      resp = await fetch("/api/reanalise", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ idreserva: Number(caixa.dataset.reanalise) }),
      });
    } catch (_) {
      st.textContent = "Sem conexão com o painel. Tente de novo.";
      st.classList.add("erro");
      b.disabled = false;
      return;
    }
    const corpo = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      st.textContent = resp.status === 401 ? SESSAO_EXPIRADA
        : corpo.detail || ("Não foi possível pedir (erro " + resp.status + ").");
      st.classList.add("erro");
      b.disabled = false;
      return;
    }
    mostrarPedido(caixa, corpo);
    acompanhar(caixa, true);
  });

  // "Buscar reservas novas": um pedido de tipo funil na fila. O executor
  // atualiza a listagem do CV e enfileira "Analisar" das reservas do funil sem
  // planilha. A tela consulta GET /api/busca-funil a cada 5 s enquanto a busca
  // anda e recarrega quando termina (as analisadas entram na lista).
  const BUSCA_ATIVA = ["na_fila", "atualizando", "analisando"];

  function textoDaBusca(e) {
    const p = e.pedido;
    if (!p) return "";
    const teto = e.fora_pelo_teto
      ? " · " + e.fora_pelo_teto + " ficaram de fora (fila cheia, máx. 20): busque de novo depois" : "";
    const falhas = e.falharam ? " · " + e.falharam + " falharam" : "";
    if (e.fase === "na_fila") return "Na fila" + (e.aguardando ? " · " + e.aguardando : "");
    if (e.fase === "atualizando") return "Atualizando o funil...";
    if (e.fase === "falhou") return "A atualização do funil falhou: " + (p.erro || "sem detalhe");
    if (e.fase === "analisando") return "Funil atualizado · " + e.analisadas + " de " + e.total + " analisadas" + falhas + teto;
    if (!e.total) return "Funil atualizado às " + hhmm(p.concluido_em) + " · nenhuma reserva nova sem análise" + teto;
    return "Busca de " + hhmm(p.pedido_em) + ": " + e.analisadas + " de " + e.total + " analisadas" + falhas + teto;
  }

  const caixaBusca = document.querySelector("[data-busca]");
  if (caixaBusca) {
    const stBusca = caixaBusca.querySelector(".busca-status");
    const btBusca = caixaBusca.querySelector("button[data-buscar]");
    const mostrarBusca = (e) => {
      const ativa = BUSCA_ATIVA.includes(e.fase);
      stBusca.textContent = textoDaBusca(e);
      stBusca.classList.toggle("erro", e.fase === "falhou");
      btBusca.disabled = ativa;
      return ativa;
    };
    const acompanharBusca = (jaAtiva) => {
      let viuAtiva = jaAtiva;
      sondar("/api/busca-funil", (e) => {
        if (mostrarBusca(e)) {
          viuAtiva = true;
          return true;
        }
        if (viuAtiva && e.fase === "concluida") window.location.reload();
        return false;
      }, (texto, definitivo) => {
        if (!viuAtiva && !definitivo) return;
        stBusca.textContent = texto;
        stBusca.classList.add("erro");
        if (definitivo) btBusca.disabled = false;
      });
    };
    acompanharBusca(false);
    btBusca.addEventListener("click", async () => {
      btBusca.disabled = true;
      let resp;
      try {
        resp = await fetch("/api/busca-funil", {
          method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: "{}",
        });
      } catch (_) {
        stBusca.textContent = "Sem conexão com o painel. Tente de novo.";
        stBusca.classList.add("erro");
        btBusca.disabled = false;
        return;
      }
      const corpo = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        stBusca.textContent = resp.status === 401 ? SESSAO_EXPIRADA
          : corpo.detail || ("Não foi possível buscar (erro " + resp.status + ").");
        stBusca.classList.add("erro");
        btBusca.disabled = false;
        return;
      }
      mostrarBusca(corpo);
      acompanharBusca(true);
    });
  }
})();
