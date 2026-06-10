import streamlit as st
import pandas as pd
from datetime import datetime

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

def sugestao_ia(dias, intervalo, orcs):
    if intervalo <= 0:
        return "🟡 Cliente novo. Iniciar relacionamento comercial."
    if intervalo * 0.9 <= dias <= intervalo * 1.2:
        return "🟢 Momento ideal. Ligar com oferta direta."
    if intervalo * 1.2 < dias <= intervalo * 2:
        return "🔴 Cliente atrasado. Fazer contato de retomada urgente."
    if dias > intervalo * 2:
        return "⚫ Cliente possivelmente perdido. Usar abordagem de reativação."
    if orcs > 0:
        return "📄 Cliente com orçamento em aberto. Priorizar follow-up."
    return "🔵 Ainda cedo. Manter relacionamento."

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

    clientes["acao_ia"] = clientes.apply(
        lambda x: sugestao_ia(x["dias_sem_comprar"], x["intervalo"], x["orcamentos_em_aberto"]),
        axis=1
    )

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
        (clientes["intervalo"] > 0) &
        (clientes["dias_sem_comprar"] >= clientes["intervalo"] * 0.9) &
        (clientes["dias_sem_comprar"] <= clientes["intervalo"] * 1.2) &
        (~clientes["Cliente"].isin(st.session_state.clientes_ligados))
    ].sort_values("ticket_medio", ascending=False)

def montar_resumo(clientes):
    return clientes[
        (clientes["intervalo"] > 0) &
        (clientes["dias_sem_comprar"] >= clientes["intervalo"] * 0.8) &
        (~clientes["Cliente"].isin(st.session_state.clientes_ligados))
    ].sort_values("dias_sem_comprar", ascending=False)

def card_cliente(row, tipo):
    atraso = int(row["dias_sem_comprar"] - row["intervalo"])

    st.markdown(f"""
<div style="background:white;padding:15px;border-radius:10px;margin-bottom:10px;border:1px solid #ddd;">
<b>{row['Cliente']}</b><br>
Compra a cada <b>{int(row['intervalo'])} dias</b><br>
Está há <b>{int(row['dias_sem_comprar'])} dias</b> sem comprar<br>
Já era para ter comprado há <b>{max(atraso, 0)} dias</b><br><br>
Ticket médio: <b>{fmt(row['ticket_medio'])}</b><br>
Potencial mensal: <b>{fmt(row['potencial_mensal'])}</b><br>
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

def renderizar():
    dados = st.session_state.dados_processados
    clientes = dados["clientes"]
    orc_aberto = dados["orc_aberto"]
    co_num = dados["co_num"]
    co_cli = dados["co_cli"]
    co_valor = dados["co_valor"]

    prioridade = montar_prioridade(clientes)
    resumo = montar_resumo(clientes)

    aba_ceo, aba_prioridade, aba_resumo, aba_orc, aba_gestao, aba_base = st.tabs([
        "👑 CEO", "🔥 Prioridade", "📋 Resumo", "📄 Orçamentos", "🧠 Gestão", "📊 Base"
    ])

    with aba_ceo:
        st.subheader("👑 Painel CEO")

        receita_prevista = clientes["faturamento"].sum()
        capacidade_hoje = prioridade["ticket_medio"].sum()
        inadimplencia_total = clientes["inadimplencia"].sum()
        potencial_mensal_carteira = clientes["potencial_mensal"].sum()

        st.markdown(f"**Receita prevista:** **{fmt(receita_prevista)}**")
        st.caption("Período: corresponde ao faturamento total contido no relatório de vendas importado.")

        st.markdown(f"**Potencial mensal da carteira:** **{fmt(potencial_mensal_carteira)}**")
        st.caption("Cálculo: média mensal de compras dos últimos 3 meses, somada entre todos os clientes.")

        st.markdown(f"**Venda possível hoje:** **{fmt(capacidade_hoje)}**")
        st.caption("Cálculo: soma do ticket médio dos clientes na aba Prioridade, dentro da janela ideal de recompra e ainda não marcados como 'Já liguei'.")

        st.markdown(f"**Inadimplência real:** **{fmt(inadimplencia_total)}**")
        st.caption("Cálculo: soma das contas com status Atrasado/Vencido.")

    with aba_prioridade:
        st.subheader("🔥 Prioridade")

        if prioridade.empty:
            st.info("Nenhum cliente no timing ideal hoje.")

        cards = list(prioridade.iterrows())
        for i in range(0, len(cards), 3):
            cols = st.columns(3)
            for j, (_, row) in enumerate(cards[i:i+3]):
                with cols[j]:
                    card_cliente(row, "prioridade")

    with aba_resumo:
        st.subheader("📋 Resumo Comercial")
        st.markdown(f"**Capacidade de venda do resumo:** **{fmt(resumo['ticket_medio'].sum())}**")

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
        st.markdown(f"**Inadimplência total:** **{fmt(clientes['inadimplencia'].sum())}**")

    with aba_base:
        st.subheader("📊 Base completa")

        acoes = ["Todas"] + sorted(clientes["acao_ia"].unique().tolist())
        filtro_acao = st.selectbox("Filtrar por ação sugerida", acoes, key="filtro_base_acao")

        base = clientes.copy()
        if filtro_acao != "Todas":
            base = base[base["acao_ia"] == filtro_acao]

        cards = list(base.iterrows())
        for i in range(0, len(cards), 3):
            cols = st.columns(3)
            for j, (_, r) in enumerate(cards[i:i+3]):
                with cols[j]:
                    st.markdown(f"""
<div style="background:white;padding:15px;border-radius:10px;margin-bottom:10px;border:1px solid #ddd;">
<b>{r['Cliente']}</b><br>
Faturamento: <b>{fmt(r['faturamento'])}</b><br>
Ticket médio: <b>{fmt(r['ticket_medio'])}</b><br>
Potencial mensal: <b>{fmt(r['potencial_mensal'])}</b><br>
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
