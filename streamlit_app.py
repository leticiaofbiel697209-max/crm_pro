import streamlit as st
import pandas as pd
from datetime import datetime

st.set_page_config(layout="wide")
st.title("📊 CRM Inteligente - Nível CEO")

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

st.sidebar.header("Importar Dados")
vendas_file = st.sidebar.file_uploader("Relatório de Vendas", type=["xlsx"])
orc_file = st.sidebar.file_uploader("Relatório de Orçamentos", type=["xlsx"])
contas_file = st.sidebar.file_uploader("Contas a Receber", type=["xlsx"])

if st.sidebar.button("Analisar Dados"):

    if not vendas_file or not orc_file or not contas_file:
        st.error("Envie os três arquivos.")
        st.stop()

    try:
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
            st.error("Colunas não encontradas: " + ", ".join(faltando))
            st.write("Colunas vendas:", list(vendas.columns))
            st.write("Colunas orçamentos:", list(orc.columns))
            st.write("Colunas contas:", list(contas.columns))
            st.stop()

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

        orc_aberto = orc.copy()
        orc_aberto = orc_aberto[
            ~orc_aberto[co_status].astype(str).str.upper().str.contains("CONCRETIZADO", na=False)
        ]
        orc_aberto = orc_aberto[
            orc_aberto[co_data] >= (hoje - pd.Timedelta(days=30))
        ]

        orc_count = orc_aberto.groupby(co_cli)[co_num].count()
        clientes["orcamentos_em_aberto"] = clientes["Cliente"].map(orc_count).fillna(0)

        if cc_status:
            contas_atraso = contas[
                contas[cc_status].astype(str).str.upper().str.contains("ATRASADO|VENCIDO", na=False)
            ]
        elif cc_venc:
            contas_atraso = contas[contas[cc_venc] < hoje]
        else:
            contas_atraso = contas.iloc[0:0]

        inad = contas_atraso.groupby(cc_cli)[cc_valor].sum()
        clientes["inadimplencia"] = clientes["Cliente"].map(inad).fillna(0)

        clientes["acao_ia"] = clientes.apply(
            lambda x: sugestao_ia(x["dias_sem_comprar"], x["intervalo"], x["orcamentos_em_aberto"]),
            axis=1
        )

        prioridade = clientes[
            (clientes["intervalo"] > 0) &
            (clientes["dias_sem_comprar"] >= clientes["intervalo"] * 0.9) &
            (clientes["dias_sem_comprar"] <= clientes["intervalo"] * 1.2)
        ].sort_values("ticket_medio", ascending=False)

        resumo = clientes[
            (clientes["intervalo"] > 0) &
            (clientes["dias_sem_comprar"] >= clientes["intervalo"] * 0.8)
        ].sort_values("dias_sem_comprar", ascending=False)

        aba_ceo, aba_prioridade, aba_resumo, aba_orc, aba_gestao, aba_base = st.tabs([
            "👑 CEO", "🔥 Prioridade", "📋 Resumo", "📄 Orçamentos", "🧠 Gestão", "📊 Base"
        ])

        with aba_ceo:
            st.subheader("👑 Painel CEO")
            st.markdown(f"**Receita prevista:** **{fmt(clientes['faturamento'].sum())}**")
            st.markdown(f"**Venda possível hoje:** **{fmt(prioridade['ticket_medio'].sum())}**")
            st.markdown(f"**Inadimplência real:** **{fmt(clientes['inadimplencia'].sum())}**")

        with aba_prioridade:
            st.subheader("🔥 Prioridade")
            if prioridade.empty:
                st.info("Nenhum cliente no timing ideal hoje.")
            for _, row in prioridade.iterrows():
                atraso = int(row["dias_sem_comprar"] - row["intervalo"])
                st.markdown(f"""
### {row['Cliente']}
Compra a cada **{int(row['intervalo'])} dias**  
Está há **{int(row['dias_sem_comprar'])} dias** sem comprar  
Já era para ter comprado há **{max(atraso, 0)} dias**  

Ticket médio: **{fmt(row['ticket_medio'])}**  
Orçamentos em aberto: **{int(row['orcamentos_em_aberto'])}**  
Inadimplência: **{fmt(row['inadimplencia'])}**  

🤖 **IA:** {row['acao_ia']}
---
""")

        with aba_resumo:
            st.subheader("📋 Resumo Comercial")
            st.markdown(f"**Capacidade de venda do resumo:** **{fmt(resumo['ticket_medio'].sum())}**")
            for _, row in resumo.iterrows():
                atraso = int(row["dias_sem_comprar"] - row["intervalo"])
                st.markdown(f"""
### {row['Cliente']}
Compra a cada **{int(row['intervalo'])} dias**  
Está há **{int(row['dias_sem_comprar'])} dias** sem comprar  
Diferença do ciclo: **{atraso} dias**  

Ticket médio: **{fmt(row['ticket_medio'])}**  
Orçamentos em aberto: **{int(row['orcamentos_em_aberto'])}**  
Inadimplência: **{fmt(row['inadimplencia'])}**  

🤖 **IA:** {row['acao_ia']}
---
""")

        with aba_orc:
            st.subheader("📄 Orçamentos em aberto para retorno")
            if orc_aberto.empty:
                st.info("Nenhum orçamento em aberto nos últimos 30 dias.")
            else:
                cols = [co_num, co_cli, co_data]
                if co_valor:
                    cols.append(co_valor)
                st.dataframe(orc_aberto[cols])

        with aba_gestao:
            st.subheader("🧠 Gestão")
            st.markdown(f"**Clientes analisados:** **{len(clientes)}**")
            st.markdown(f"**Clientes em prioridade:** **{len(prioridade)}**")
            st.markdown(f"**Clientes no resumo:** **{len(resumo)}**")
            st.markdown(f"**Inadimplência total:** **{fmt(clientes['inadimplencia'].sum())}**")

        with aba_base:
            st.subheader("📊 Base completa")
            base = clientes.copy()
            for col in ["faturamento", "ticket_medio", "inadimplencia"]:
                base[col] = base[col].apply(fmt)
            st.dataframe(base)

    except Exception as e:
        st.error(f"Erro ao processar: {e}")
