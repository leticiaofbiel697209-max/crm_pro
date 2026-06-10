import streamlit as st
import pandas as pd
from datetime import datetime

st.set_page_config(layout="wide")
st.title("📊 CRM Inteligente - Nível CEO")

def formatar_real(valor):
    try:
        return f"R${valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return "R$0,00"

def ler_excel(file, header=0):
    return pd.read_excel(file, header=header, engine="openpyxl")

def achar_coluna(df, nomes):
    for coluna in df.columns:
        for nome in nomes:
            if nome.lower() in str(coluna).lower():
                return coluna
    return None

def sugestao_ia(dias_sem, intervalo, orcamentos):
    if intervalo == 0:
        return "🟡 Cliente novo. Iniciar relacionamento comercial."

    if dias_sem >= intervalo * 0.9 and dias_sem <= intervalo * 1.2:
        return "🟢 Momento ideal. Ligar com oferta direta."

    if dias_sem > intervalo * 1.2 and dias_sem <= intervalo * 2:
        return "🔴 Cliente atrasado. Fazer contato de retomada urgente."

    if dias_sem > intervalo * 2:
        return "⚫ Cliente possivelmente perdido. Usar abordagem de reativação."

    if orcamentos > 0:
        return "📄 Cliente com orçamento em aberto. Priorizar follow-up."

    return "🔵 Ainda cedo. Manter relacionamento."

st.sidebar.header("Importar Dados")

vendas_file = st.sidebar.file_uploader("Relatório de Vendas", type=["xlsx"])
orc_file = st.sidebar.file_uploader("Relatório de Orçamentos", type=["xlsx"])
contas_file = st.sidebar.file_uploader("Contas a Receber", type=["xlsx"])

if st.sidebar.button("Analisar Dados"):

    if not vendas_file or not orc_file or not contas_file:
        st.error("Envie os três arquivos: vendas, orçamentos e contas a receber.")
        st.stop()

    try:
        vendas = ler_excel(vendas_file)
        contas = ler_excel(contas_file)
        orc = ler_excel(orc_file, header=1)

        col_cliente = achar_coluna(vendas, ["cliente"])
        col_data = achar_coluna(vendas, ["data"])
        col_valor = achar_coluna(vendas, ["valor"])

        col_cliente_orc = achar_coluna(orc, ["cliente"])
        col_numero_orc = achar_coluna(orc, ["nº", "numero", "número"])
        col_data_orc = achar_coluna(orc, ["data"])
        col_status_orc = achar_coluna(orc, ["situação", "situacao", "status"])

        col_cliente_contas = achar_coluna(contas, ["cliente", "destinado"])
        col_valor_contas = achar_coluna(contas, ["valor total", "valor"])
        col_venc = achar_coluna(contas, ["vencimento"])
        col_status_contas = achar_coluna(contas, ["situação", "situacao", "status"])

        vendas[col_data] = pd.to_datetime(vendas[col_data], dayfirst=True, errors="coerce")
        orc[col_data_orc] = pd.to_datetime(orc[col_data_orc], dayfirst=True, errors="coerce")
        contas[col_venc] = pd.to_datetime(contas[col_venc], dayfirst=True, errors="coerce")

        vendas[col_valor] = pd.to_numeric(vendas[col_valor], errors="coerce").fillna(0)
        contas[col_valor_contas] = pd.to_numeric(contas[col_valor_contas], errors="coerce").fillna(0)

        hoje = datetime.now()

        clientes = vendas.groupby(col_cliente).agg({
            col_data: ["max", "count"],
            col_valor: "sum"
        })

        clientes.columns = ["ultima_compra", "qtd_compras", "faturamento"]
        clientes = clientes.reset_index()
        clientes.rename(columns={col_cliente: "Cliente"}, inplace=True)

        intervalo = vendas.sort_values(col_data).groupby(col_cliente)[col_data].apply(
            lambda x: x.diff().mean().days if len(x.dropna()) > 1 else 0
        )

        clientes["intervalo"] = clientes["Cliente"].map(intervalo).fillna(0)
        clientes["dias_sem_comprar"] = (hoje - clientes["ultima_compra"]).dt.days
        clientes["ticket_medio"] = clientes["faturamento"] / clientes["qtd_compras"]

        orc_aberto = orc.copy()

        if col_status_orc:
            orc_aberto = orc_aberto[
                ~orc_aberto[col_status_orc].astype(str).str.upper().str.contains("CONCRETIZADO", na=False)
            ]

        orc_aberto = orc_aberto[
            orc_aberto[col_data_orc] >= (hoje - pd.Timedelta(days=30))
        ]

        orc_count = orc_aberto.groupby(col_cliente_orc)[col_numero_orc].count()
        clientes["orcamentos_em_aberto"] = clientes["Cliente"].map(orc_count).fillna(0)

        if col_status_contas:
            contas_atraso = contas[
                contas[col_status_contas].astype(str).str.upper().str.contains("ATRASADO|VENCIDO", na=False)
            ]
        else:
            contas_atraso = contas[contas[col_venc] < hoje]

        inad = contas_atraso.groupby(col_cliente_contas)[col_valor_contas].sum()
        clientes["inadimplencia"] = clientes["Cliente"].map(inad).fillna(0)

        clientes["acao_ia"] = clientes.apply(
            lambda x: sugestao_ia(
                x["dias_sem_comprar"],
                x["intervalo"],
                x["orcamentos_em_aberto"]
            ),
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
            "👑 CEO",
            "🔥 Prioridade",
            "📋 Resumo",
            "📄 Orçamentos",
            "🧠 Gestão",
            "📊 Base"
        ])

        with aba_ceo:
            st.subheader("👑 Painel CEO")

            receita_prevista = clientes["faturamento"].sum()
            capacidade_hoje = prioridade["ticket_medio"].sum()
            inadimplencia_total = clientes["inadimplencia"].sum()

            st.markdown(f"**Receita prevista:** **{formatar_real(receita_prevista)}**")
            st.markdown(f"**Venda possível hoje:** **{formatar_real(capacidade_hoje)}**")
            st.markdown(f"**Inadimplência real:** **{formatar_real(inadimplencia_total)}**")

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

Ticket médio: **{formatar_real(row['ticket_medio'])}**  
Orçamentos em aberto: **{int(row['orcamentos_em_aberto'])}**  
Inadimplência: **{formatar_real(row['inadimplencia'])}**

🤖 **IA:** {row['acao_ia']}

---
""")

        with aba_resumo:
            st.subheader("📋 Resumo Comercial")

            capacidade_resumo = resumo["ticket_medio"].sum()
            st.markdown(f"**Capacidade de venda do resumo:** **{formatar_real(capacidade_resumo)}**")

            for _, row in resumo.iterrows():
                atraso = int(row["dias_sem_comprar"] - row["intervalo"])

                st.markdown(f"""
### {row['Cliente']}

Compra a cada **{int(row['intervalo'])} dias**  
Está há **{int(row['dias_sem_comprar'])} dias** sem comprar  
Diferença do ciclo: **{atraso} dias**

Ticket médio: **{formatar_real(row['ticket_medio'])}**  
Orçamentos em aberto: **{int(row['orcamentos_em_aberto'])}**  
Inadimplência: **{formatar_real(row['inadimplencia'])}**

🤖 **IA:** {row['acao_ia']}

---
""")

        with aba_orc:
            st.subheader("📄 Orçamentos em aberto para retorno")

            if orc_aberto.empty:
                st.info("Nenhum orçamento em aberto nos últimos 30 dias.")
            else:
                cols = [col_cliente_orc, col_numero_orc, col_data_orc]
                if achar_coluna(orc_aberto, ["valor"]):
                    cols.append(achar_coluna(orc_aberto, ["valor"]))

                st.dataframe(orc_aberto[cols])

        with aba_gestao:
            st.subheader("🧠 Gestão")

            st.markdown(f"**Clientes analisados:** **{len(clientes)}**")
            st.markdown(f"**Clientes em prioridade:** **{len(prioridade)}**")
            st.markdown(f"**Clientes no resumo:** **{len(resumo)}**")
            st.markdown(f"**Inadimplência total:** **{formatar_real(clientes['inadimplencia'].sum())}**")

        with aba_base:
            st.subheader("📊 Base completa")

            base = clientes.copy()
            base["faturamento"] = base["faturamento"].apply(formatar_real)
            base["ticket_medio"] = base["ticket_medio"].apply(formatar_real)
            base["inadimplencia"] = base["inadimplencia"].apply(formatar_real)

            st.dataframe(base)

    except Exception as e:
        st.error(f"Erro ao processar: {e}")
