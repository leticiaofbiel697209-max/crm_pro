import streamlit as st
import pandas as pd
from datetime import datetime
from io import BytesIO

try:
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    REPORTLAB_OK = True
except Exception:
    REPORTLAB_OK = False

st.set_page_config(layout="wide")
st.title("📊 CRM Inteligente - Nível CEO")

if "clientes_ligados" not in st.session_state:
    st.session_state.clientes_ligados = set()

if "dados_processados" not in st.session_state:
    st.session_state.dados_processados = None

if "observacoes_orc" not in st.session_state:
    st.session_state.observacoes_orc = {}

def fmt(v):
    try:
        return f"R${float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return "R$0,00"

def norm(x):
    return str(x).strip().lower().replace("º", "o").replace("°", "o")

def achar_coluna(df, termos):
    for c in df.columns:
        nc = norm(c)
        for t in termos:
            if norm(t) in nc:
                return c
    return None

def carregar_excel(file, grupos_busca):
    bruto = pd.read_excel(file, header=None, engine="openpyxl")
    melhor_linha, melhor_score = 0, -1

    for i in range(min(15, len(bruto))):
        valores = [norm(x) for x in bruto.iloc[i].tolist()]
        score = 0
        for grupo in grupos_busca:
            if any(any(norm(t) in v for v in valores) for t in grupo):
                score += 1
        if score > melhor_score:
            melhor_linha, melhor_score = i, score

    df = pd.read_excel(file, header=melhor_linha, engine="openpyxl")
    df = df.dropna(how="all")
    df.columns = [str(c).strip() for c in df.columns]
    return df

def data_coluna(s):
    return pd.to_datetime(s, dayfirst=True, errors="coerce")

def numero_coluna(s):
    return pd.to_numeric(
        s.astype(str).str.replace(".", "", regex=False).str.replace(",", ".", regex=False),
        errors="coerce"
    ).fillna(0)

def status_orcamento(dias):
    if dias <= 1:
        return "✅ Aceitável"
    if dias == 2:
        return "📞 Ligar hoje"
    if dias == 3:
        return "⚠️ Está perdendo tempo"
    return "🚨 Risco de ter perdido"

def score_risco(media_atraso):
    if pd.isna(media_atraso) or media_atraso <= 0:
        return 100
    return max(0, min(100, int(100 - media_atraso * 2)))

def descricao_score(score):
    if score >= 85:
        return "🟢 Baixo risco de inadimplência"
    if score >= 65:
        return "🟡 Risco moderado de inadimplência"
    if score >= 40:
        return "🟠 Alto risco de inadimplência"
    return "🔴 Risco crítico de inadimplência"

def temperatura_cliente(dias, intervalo):
    if intervalo <= 0:
        return "🟣 NOVO"
    if intervalo * 0.9 <= dias <= intervalo * 1.2:
        return "🟢 QUENTE"
    if intervalo * 1.2 < dias <= intervalo * 1.5:
        return "🟡 ATENÇÃO"
    if intervalo * 1.5 < dias <= intervalo * 2:
        return "🔴 ATRASADO NA RECOMPRA"
    if dias > intervalo * 2:
        return "⚫ CLIENTE INATIVO"
    return "🔵 CEDO"

def sugestao_ia(dias, intervalo, orcs, inad, potencial):
    temp = temperatura_cliente(dias, intervalo)

    if inad > 0:
        return "💸 Cliente com inadimplência. Priorizar cobrança antes de nova venda."
    if orcs > 0 and temp in ["🟢 QUENTE", "🟡 ATENÇÃO"]:
        return "📄 Cliente com orçamento em aberto e bom momento de compra. Priorizar fechamento hoje."
    if temp == "🟢 QUENTE":
        return f"🟢 Momento ideal. Ligar com oferta direta. Potencial mensal: {fmt(potencial)}."
    if temp == "🟡 ATENÇÃO":
        return "🟡 Cliente passou levemente do ciclo. Fazer contato de retomada antes que esfrie."
    if temp == "🔴 ATRASADO NA RECOMPRA":
        return "🔴 Cliente atrasado na recompra. Entender se comprou de concorrente ou se esqueceu."
    if temp == "⚫ CLIENTE INATIVO":
        return "⚫ Cliente inativo. Usar abordagem de reativação com condição especial."
    if orcs > 0:
        return "📄 Cliente com orçamento em aberto. Fazer follow-up comercial."
    if temp == "🔵 CEDO":
        return "🔵 Ainda cedo para venda direta. Manter relacionamento ou aquecer contato."
    return "🟣 Cliente novo. Iniciar relacionamento comercial."

def score_comercial(row):
    score = 0
    temp = row["temperatura"]

    if temp == "🟢 QUENTE":
        score += 40
    elif temp == "🟡 ATENÇÃO":
        score += 30
    elif temp == "🔴 ATRASADO NA RECOMPRA":
        score += 20
    elif temp == "⚫ CLIENTE INATIVO":
        score += 10

    if row["orcamentos_em_aberto"] > 0:
        score += 20
    if row["score_risco"] >= 85:
        score += 20
    elif row["score_risco"] >= 65:
        score += 10
    if row["potencial_mensal"] > 0:
        score += 20

    return min(score, 100)

def processar_dados(vendas_file, orc_file, contas_file):
    hoje = datetime.now()

    vendas = carregar_excel(vendas_file, [["cliente"], ["data"], ["valor"]])
    orc = carregar_excel(orc_file, [["nº", "n°", "numero", "número"], ["cliente"], ["data"], ["situação", "status"]])
    contas = carregar_excel(contas_file, [["cliente", "destinado"], ["vencimento"], ["valor"], ["situação", "status"]])

    cv_cli = achar_coluna(vendas, ["cliente"])
    cv_data = achar_coluna(vendas, ["data"])
    cv_valor = achar_coluna(vendas, ["valor"])

    co_num = achar_coluna(orc, ["nº", "n°", "numero", "número"])
    co_cli = achar_coluna(orc, ["cliente"])
    co_data = achar_coluna(orc, ["data"])
    co_status = achar_coluna(orc, ["situação", "situacao", "status"])
    co_valor = achar_coluna(orc, ["valor"])

    cc_cli = achar_coluna(contas, ["cliente", "destinado"])
    cc_venc = achar_coluna(contas, ["vencimento"])
    cc_status = achar_coluna(contas, ["situação", "situacao", "status"])
    cc_valor = achar_coluna(contas, ["valor total", "valor"])

    faltando = []
    for nome, col in {
        "Cliente vendas": cv_cli,
        "Data vendas": cv_data,
        "Valor vendas": cv_valor,
        "Nº orçamento": co_num,
        "Cliente orçamento": co_cli,
        "Data orçamento": co_data,
        "Status orçamento": co_status,
        "Cliente contas": cc_cli,
        "Valor contas": cc_valor,
    }.items():
        if col is None:
            faltando.append(nome)

    if faltando:
        raise Exception("Colunas não encontradas: " + ", ".join(faltando))

    vendas[cv_data] = data_coluna(vendas[cv_data])
    vendas[cv_valor] = numero_coluna(vendas[cv_valor])
    vendas = vendas.dropna(subset=[cv_cli, cv_data])

    orc[co_data] = data_coluna(orc[co_data])
    if co_valor:
        orc[co_valor] = numero_coluna(orc[co_valor])

    contas[cc_valor] = numero_coluna(contas[cc_valor])
    if cc_venc:
        contas[cc_venc] = data_coluna(contas[cc_venc])

    clientes = vendas.groupby(cv_cli).agg({
        cv_data: ["max", "count"],
        cv_valor: "sum"
    })

    clientes.columns = ["ultima_compra", "qtd_compras", "faturamento"]
    clientes = clientes.reset_index().rename(columns={cv_cli: "Cliente"})

    intervalo = vendas.sort_values(cv_data).groupby(cv_cli)[cv_data].apply(
        lambda x: x.diff().mean().days if len(x.dropna()) > 1 else 0
    )

    clientes["intervalo"] = clientes["Cliente"].map(intervalo).fillna(0)
    clientes["dias_sem_comprar"] = (hoje - clientes["ultima_compra"]).dt.days
    clientes["ticket_medio"] = clientes["faturamento"] / clientes["qtd_compras"]

    data_limite_3m = hoje - pd.DateOffset(months=3)
    vendas_3m = vendas[vendas[cv_data] >= data_limite_3m].copy()
    potencial_3m = vendas_3m.groupby(cv_cli)[cv_valor].sum() / 3
    clientes["potencial_mensal"] = clientes["Cliente"].map(potencial_3m).fillna(0)

    orc_aberto = orc.copy()
    orc_aberto = orc_aberto[
        ~orc_aberto[co_status].astype(str).str.upper().str.contains("CONCRETIZADO", na=False)
    ]
    orc_aberto = orc_aberto[
        orc_aberto[co_data] >= (hoje - pd.Timedelta(days=30))
    ].copy()

    orc_aberto["dias_no_sistema"] = (hoje - orc_aberto[co_data]).dt.days
    orc_aberto["ação_recomendada"] = orc_aberto["dias_no_sistema"].apply(status_orcamento)

    orc_count = orc_aberto.groupby(co_cli)[co_num].count()
    clientes["orcamentos_em_aberto"] = clientes["Cliente"].map(orc_count).fillna(0)

    orc_nums = orc_aberto.groupby(co_cli)[co_num].apply(lambda x: list(x.astype(str)))
    clientes["numeros_orcamentos"] = clientes["Cliente"].map(orc_nums).apply(lambda x: x if isinstance(x, list) else [])

    if cc_status:
        contas_atraso = contas[
            contas[cc_status].astype(str).str.upper().str.contains("ATRASADO|VENCIDO", na=False)
        ].copy()
    elif cc_venc:
        contas_atraso = contas[contas[cc_venc] < hoje].copy()
    else:
        contas_atraso = contas.iloc[0:0].copy()

    if cc_venc and not contas_atraso.empty:
        contas_atraso["dias_atraso"] = (hoje - contas_atraso[cc_venc]).dt.days.clip(lower=0)
        media_atraso = contas_atraso.groupby(cc_cli)["dias_atraso"].mean()
    else:
        media_atraso = pd.Series(dtype=float)

    inad = contas_atraso.groupby(cc_cli)[cc_valor].sum() if not contas_atraso.empty else pd.Series(dtype=float)

    clientes["inadimplencia"] = clientes["Cliente"].map(inad).fillna(0)
    clientes["media_dias_atraso"] = clientes["Cliente"].map(media_atraso).fillna(0)
    clientes["score_risco"] = clientes["media_dias_atraso"].apply(score_risco)
    clientes["risco_inadimplencia"] = clientes["score_risco"].apply(descricao_score)

    clientes["temperatura"] = clientes.apply(
        lambda x: temperatura_cliente(x["dias_sem_comprar"], x["intervalo"]),
        axis=1
    )

    limite_estrategico = clientes["faturamento"].quantile(0.90)
    clientes["cliente_estrategico"] = clientes["faturamento"] >= limite_estrategico

    clientes["potencial_recuperavel"] = clientes.apply(
        lambda x: x["potencial_mensal"] if x["temperatura"] in ["🔴 ATRASADO NA RECOMPRA", "⚫ CLIENTE INATIVO"] else 0,
        axis=1
    )

    clientes["acao_ia"] = clientes.apply(
        lambda x: sugestao_ia(
            x["dias_sem_comprar"],
            x["intervalo"],
            x["orcamentos_em_aberto"],
            x["inadimplencia"],
            x["potencial_mensal"]
        ),
        axis=1
    )

    clientes["score_comercial"] = clientes.apply(score_comercial, axis=1)

    return {
        "clientes": clientes,
        "orc_aberto": orc_aberto,
        "co_num": co_num,
        "co_cli": co_cli,
        "co_data": co_data,
        "co_valor": co_valor,
    }

def montar_prioridade(clientes):
    return clientes[
        (clientes["temperatura"] == "🟢 QUENTE") &
        (~clientes["Cliente"].isin(st.session_state.clientes_ligados))
    ].sort_values("score_comercial", ascending=False)

def montar_resumo(clientes):
    return clientes[
        (clientes["temperatura"].isin(["🟢 QUENTE", "🟡 ATENÇÃO", "🔴 ATRASADO NA RECOMPRA", "⚫ CLIENTE INATIVO"])) &
        (~clientes["Cliente"].isin(st.session_state.clientes_ligados))
    ].sort_values("score_comercial", ascending=False)

def card_cliente(row, tipo):
    atraso = int(row["dias_sem_comprar"] - row["intervalo"])
    estrela = "⭐ Cliente estratégico<br>" if row["cliente_estrategico"] else ""

    st.markdown(f"""
<div style="background:white;padding:15px;border-radius:10px;margin-bottom:10px;border:1px solid #ddd;">
<b>{row['Cliente']}</b><br>
{estrela}
Temperatura: <b>{row['temperatura']}</b><br>
Score comercial: <b>{int(row['score_comercial'])}/100</b><br><br>
Compra a cada <b>{int(row['intervalo'])} dias</b><br>
Está há <b>{int(row['dias_sem_comprar'])} dias</b> sem comprar<br>
Já era para ter comprado há <b>{max(atraso, 0)} dias</b><br><br>
Ticket médio: <b>{fmt(row['ticket_medio'])}</b><br>
Potencial mensal: <b>{fmt(row['potencial_mensal'])}</b><br>
Potencial recuperável: <b>{fmt(row['potencial_recuperavel'])}</b><br>
Orçamentos em aberto: <b>{int(row['orcamentos_em_aberto'])}</b><br>
Inadimplência: <b>{fmt(row['inadimplencia'])}</b><br>
Score de risco: <b>{int(row['score_risco'])}/100 — {row['risco_inadimplencia']}</b><br><br>
IA: <b>{row['acao_ia']}</b>
</div>
""", unsafe_allow_html=True)

    with st.expander("Ver orçamentos em aberto"):
        if row["numeros_orcamentos"]:
            for num in row["numeros_orcamentos"]:
                st.write(f"• Orçamento Nº {num}")
        else:
            st.write("Nenhum orçamento em aberto.")

    if st.button(f"✅ Já liguei - {row['Cliente']}", key=f"liguei_{tipo}_{row['Cliente']}"):
        st.session_state.clientes_ligados.add(row["Cliente"])
        st.rerun()

def gerar_texto_email(prioridade, resumo, orc_aberto, clientes):
    hoje_txt = datetime.now().strftime("%d/%m/%Y")

    linhas = [
        f"Resumo Comercial - {hoje_txt}",
        "",
        f"Clientes para ligar hoje: {len(prioridade)}",
        f"Potencial mensal da carteira: {fmt(clientes['potencial_mensal'].sum())}",
        f"Venda possível hoje: {fmt(prioridade['ticket_medio'].sum())}",
        f"Potencial recuperável: {fmt(clientes['potencial_recuperavel'].sum())}",
        f"Inadimplência real: {fmt(clientes['inadimplencia'].sum())}",
        "",
        "PRIORIDADE:"
    ]

    for _, r in prioridade.head(10).iterrows():
        linhas.append(f"- {r['Cliente']} | {r['temperatura']} | Ticket: {fmt(r['ticket_medio'])} | IA: {r['acao_ia']}")

    linhas.append("")
    linhas.append("CLIENTES EM RECUPERAÇÃO:")

    recuperacao = resumo[resumo["temperatura"].isin(["🔴 ATRASADO NA RECOMPRA", "⚫ CLIENTE INATIVO"])].head(10)

    if recuperacao.empty:
        linhas.append("- Nenhum.")
    else:
        for _, r in recuperacao.iterrows():
            linhas.append(f"- {r['Cliente']} | {r['temperatura']} | Potencial recuperável: {fmt(r['potencial_recuperavel'])}")

    return "\n".join(linhas)

def gerar_pdf(prioridade, resumo, clientes):
    if not REPORTLAB_OK:
        return None

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer)
    styles = getSampleStyleSheet()
    elementos = []

    elementos.append(Paragraph("RELATÓRIO COMERCIAL", styles["Title"]))
    elementos.append(Paragraph(datetime.now().strftime("%d/%m/%Y"), styles["Normal"]))
    elementos.append(Spacer(1, 20))

    elementos.append(Paragraph(f"Venda possível hoje: {fmt(prioridade['ticket_medio'].sum())}", styles["Heading2"]))
    elementos.append(Paragraph(f"Potencial mensal da carteira: {fmt(clientes['potencial_mensal'].sum())}", styles["Heading2"]))
    elementos.append(Paragraph(f"Potencial recuperável: {fmt(clientes['potencial_recuperavel'].sum())}", styles["Heading2"]))
    elementos.append(Paragraph(f"Inadimplência real: {fmt(clientes['inadimplencia'].sum())}", styles["Heading2"]))

    elementos.append(Spacer(1, 20))
    elementos.append(Paragraph("CLIENTES PRIORITÁRIOS", styles["Heading1"]))

    if prioridade.empty:
        elementos.append(Paragraph("Nenhum cliente prioritário hoje.", styles["Normal"]))
    else:
        for _, r in prioridade.head(20).iterrows():
            elementos.append(Paragraph(
                f"<b>{r['Cliente']}</b><br/>"
                f"Temperatura: {r['temperatura']}<br/>"
                f"Ticket médio: {fmt(r['ticket_medio'])}<br/>"
                f"Potencial mensal: {fmt(r['potencial_mensal'])}<br/>"
                f"Ação: {r['acao_ia']}",
                styles["Normal"]
            ))
            elementos.append(Spacer(1, 10))

    doc.build(elementos)
    pdf = buffer.getvalue()
    buffer.close()
    return pdf

def renderizar():
    dados = st.session_state.dados_processados
    clientes = dados["clientes"]
    orc_aberto = dados["orc_aberto"]
    co_num = dados["co_num"]
    co_cli = dados["co_cli"]
    co_valor = dados["co_valor"]

    prioridade = montar_prioridade(clientes)
    resumo = montar_resumo(clientes)

    aba_ceo, aba_prioridade, aba_resumo, aba_orc, aba_gestao, aba_base, aba_email, aba_relatorio = st.tabs([
        "👑 CEO", "🔥 Prioridade", "📋 Resumo", "📄 Orçamentos", "🧠 Gestão", "📊 Base", "✉️ Resumo E-mail", "📧 Relatório Comercial"
    ])

    with aba_ceo:
        st.subheader("👑 Painel CEO")

        st.markdown(f"**Receita prevista:** **{fmt(clientes['faturamento'].sum())}**")
        st.caption("Período: faturamento total contido no relatório de vendas importado.")

        st.markdown(f"**Potencial mensal da carteira:** **{fmt(clientes['potencial_mensal'].sum())}**")
        st.caption("Média mensal dos últimos 3 meses.")

        st.markdown(f"**Venda possível hoje:** **{fmt(prioridade['ticket_medio'].sum())}**")
        st.caption("Soma do ticket médio dos clientes na aba Prioridade.")

        st.markdown(f"**Potencial recuperável:** **{fmt(clientes['potencial_recuperavel'].sum())}**")
        st.caption("Soma do potencial mensal dos clientes atrasados na recompra ou inativos.")

        st.markdown(f"**Inadimplência real:** **{fmt(clientes['inadimplencia'].sum())}**")

    with aba_prioridade:
        st.subheader("🔥 Prioridade")
        cards = list(prioridade.iterrows())

        if prioridade.empty:
            st.info("Nenhum cliente no timing ideal hoje.")

        for i in range(0, len(cards), 3):
            cols = st.columns(3)
            for j, (_, row) in enumerate(cards[i:i+3]):
                with cols[j]:
                    card_cliente(row, "prioridade")

    with aba_resumo:
        st.subheader("📋 Resumo Comercial")
        st.markdown(f"**Clientes para ação:** **{len(resumo)}**")
        st.markdown(f"**Capacidade de venda do resumo:** **{fmt(resumo['ticket_medio'].sum())}**")
        st.markdown(f"**Potencial recuperável:** **{fmt(resumo['potencial_recuperavel'].sum())}**")

        cards = list(resumo.iterrows())
        for i in range(0, len(cards), 3):
            cols = st.columns(3)
            for j, (_, row) in enumerate(cards[i:i+3]):
                with cols[j]:
                    card_cliente(row, "resumo")

    with aba_orc:
        st.subheader("📄 Orçamentos em aberto para retorno")

        if orc_aberto.empty:
            st.info("Nenhum orçamento em aberto nos últimos 30 dias.")
        else:
            cards = list(orc_aberto.iterrows())
            for i in range(0, len(cards), 3):
                cols = st.columns(3)
                for j, (_, r) in enumerate(cards[i:i+3]):
                    with cols[j]:
                        valor_txt = fmt(r[co_valor]) if co_valor else "Sem valor"
                        chave_obs = f"obs_orc_{r[co_num]}"

                        st.markdown(f"""
<div style="background:white;padding:15px;border-radius:10px;margin-bottom:10px;border:1px solid #ddd;">
<b>Orçamento Nº {r[co_num]}</b><br>
Cliente: <b>{r[co_cli]}</b><br>
Tempo no sistema: <b>{int(r['dias_no_sistema'])} dia(s)</b><br>
Status: <b>{r['ação_recomendada']}</b><br>
Valor: <b>{valor_txt}</b>
</div>
""", unsafe_allow_html=True)

                        st.text_area(
                            "Observação",
                            value=st.session_state.observacoes_orc.get(str(r[co_num]), ""),
                            key=chave_obs
                        )
                        st.session_state.observacoes_orc[str(r[co_num])] = st.session_state[chave_obs]

    with aba_gestao:
        st.subheader("🧠 Gestão")
        st.markdown(f"**Clientes analisados:** **{len(clientes)}**")
        st.markdown(f"**Clientes em prioridade:** **{len(prioridade)}**")
        st.markdown(f"**Clientes no resumo:** **{len(resumo)}**")
        st.markdown(f"**Potencial mensal da carteira:** **{fmt(clientes['potencial_mensal'].sum())}**")
        st.markdown(f"**Potencial recuperável:** **{fmt(clientes['potencial_recuperavel'].sum())}**")
        st.markdown(f"**Inadimplência total:** **{fmt(clientes['inadimplencia'].sum())}**")

    with aba_base:
        st.subheader("📊 Base completa")

        acoes = ["Todas"] + sorted(clientes["acao_ia"].unique().tolist())
        temperaturas = ["Todas"] + sorted(clientes["temperatura"].unique().tolist())

        col1, col2 = st.columns(2)
        with col1:
            filtro_acao = st.selectbox("Filtrar por ação sugerida", acoes, key="filtro_base_acao")
        with col2:
            filtro_temp = st.selectbox("Filtrar por temperatura", temperaturas, key="filtro_base_temp")

        base = clientes.copy()

        if filtro_acao != "Todas":
            base = base[base["acao_ia"] == filtro_acao]
        if filtro_temp != "Todas":
            base = base[base["temperatura"] == filtro_temp]

        cards = list(base.iterrows())

        for i in range(0, len(cards), 3):
            cols = st.columns(3)
            for j, (_, r) in enumerate(cards[i:i+3]):
                with cols[j]:
                    estrela = "⭐ Cliente estratégico<br>" if r["cliente_estrategico"] else ""
                    st.markdown(f"""
<div style="background:white;padding:15px;border-radius:10px;margin-bottom:10px;border:1px solid #ddd;">
<b>{r['Cliente']}</b><br>
{estrela}
Temperatura: <b>{r['temperatura']}</b><br>
Score comercial: <b>{int(r['score_comercial'])}/100</b><br>
Faturamento: <b>{fmt(r['faturamento'])}</b><br>
Ticket médio: <b>{fmt(r['ticket_medio'])}</b><br>
Potencial mensal: <b>{fmt(r['potencial_mensal'])}</b><br>
Potencial recuperável: <b>{fmt(r['potencial_recuperavel'])}</b><br>
Compras: <b>{int(r['qtd_compras'])}</b><br>
Intervalo médio: <b>{int(r['intervalo'])} dias</b><br>
Última compra: <b>{r['ultima_compra'].strftime('%d/%m/%Y')}</b><br>
Dias sem comprar: <b>{int(r['dias_sem_comprar'])}</b><br>
Orçamentos em aberto: <b>{int(r['orcamentos_em_aberto'])}</b><br>
Inadimplência: <b>{fmt(r['inadimplencia'])}</b><br>
Score de risco: <b>{int(r['score_risco'])}/100 — {r['risco_inadimplencia']}</b><br>
IA: <b>{r['acao_ia']}</b>
</div>
""", unsafe_allow_html=True)

    with aba_email:
        st.subheader("✉️ Resumo para E-mail")
        texto_email = gerar_texto_email(prioridade, resumo, orc_aberto, clientes)
        st.text_area("Copie e envie para as vendedoras:", texto_email, height=500)
        st.download_button("Baixar resumo em .txt", texto_email, "resumo_comercial.txt", "text/plain")

    with aba_relatorio:
        st.subheader("📧 Relatório Comercial")

        st.markdown(f"""
### Resumo Executivo

💰 Venda possível hoje: **{fmt(prioridade['ticket_medio'].sum())}**

📈 Potencial mensal da carteira: **{fmt(clientes['potencial_mensal'].sum())}**

📉 Potencial recuperável: **{fmt(clientes['potencial_recuperavel'].sum())}**

🚨 Clientes inativos: **{len(clientes[clientes['temperatura'] == '⚫ CLIENTE INATIVO'])}**

📄 Orçamentos em aberto: **{len(orc_aberto)}**
""")

        pdf = gerar_pdf(prioridade, resumo, clientes)

        if pdf:
            st.download_button(
                "📄 Baixar Relatório PDF",
                pdf,
                file_name=f"Relatorio_Comercial_{datetime.now().strftime('%d_%m_%Y')}.pdf",
                mime="application/pdf"
            )
        else:
            st.warning("PDF indisponível. Verifique se 'reportlab' está no requirements.txt.")

st.sidebar.header("Importar Dados")
vendas_file = st.sidebar.file_uploader("Relatório de Vendas", type=["xlsx"])
orc_file = st.sidebar.file_uploader("Relatório de Orçamentos", type=["xlsx"])
contas_file = st.sidebar.file_uploader("Contas a Receber", type=["xlsx"])

if st.sidebar.button("Analisar Dados"):
    if not vendas_file or not orc_file or not contas_file:
        st.error("Envie os três arquivos.")
        st.stop()

    try:
        st.session_state.dados_processados = processar_dados(vendas_file, orc_file, contas_file)
    except Exception as e:
        st.error(f"Erro ao processar: {e}")

if st.session_state.dados_processados is not None:
    renderizar()
else:
    st.info("Importe os relatórios na barra lateral e clique em Analisar Dados.")
