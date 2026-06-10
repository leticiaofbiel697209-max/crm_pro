import streamlit as st
import pandas as pd
from datetime import datetime

st.set_page_config(layout="wide")
st.title("📊 CRM Inteligente - Nível CEO")

# =========================
# FUNÇÕES
# =========================

def formatar_real(valor):
    try:
        return f"R${valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return "R$0,00"

def achar_coluna(df, nomes):
    for nome in df.columns:
        for n in nomes:
            if n.lower() in str(nome).lower():
                return nome
    return None

def sugestao_ia(dias_sem, intervalo, orcamentos):
    if intervalo == 0:
        return "Cliente novo. Iniciar relacionamento."
    if dias_sem > intervalo:
        return "🔴 Cliente em atraso. Contato imediato com proposta direta."
    elif dias_sem >= intervalo * 0.8:
        return "🟡 Cliente próximo do ciclo de compra. Fazer abordagem consultiva."
    elif orcamentos > 0:
        return "🟢 Follow-up de orçamento em aberto."
    else:
        return "🔵 Cliente inativo. Reativação com condição especial."

# =========================
# UPLOAD
# =========================

st.sidebar.header("Importar Dados")

vendas_file = st.sidebar.file_uploader("Vendas")
orc_file = st.sidebar.file_uploader("Orçamentos")
contas_file = st.sidebar.file_uploader("Contas a Receber")

if st.sidebar.button("Analisar Dados"):

    try:
        vendas = pd.read_excel(vendas_file)
        contas = pd.read_excel(contas_file)
        orc = pd.read_excel(orc_file, header=1)

        # =========================
        # IDENTIFICAR COLUNAS
        # =========================

        col_cliente = achar_coluna(vendas, ["cliente"])
        col_data = achar_coluna(vendas, ["data"])
        col_valor = achar_coluna(vendas, ["valor"])

        col_cliente_contas = achar_coluna(contas, ["cliente"])
        col_valor_contas = achar_coluna(contas, ["valor"])
        col_venc = achar_coluna(contas, ["venc"])

        col_cliente_orc = achar_coluna(orc, ["cliente"])
        col_numero_orc = achar_coluna(orc, ["nº", "numero"])
        col_data_orc = achar_coluna(orc, ["data"])
        col_status_orc = achar_coluna(orc, ["situa"])

        # =========================
        # TRATAMENTO
        # =========================

        vendas[col_data] = pd.to_datetime(vendas[col_data], dayfirst=True)
        contas[col_venc] = pd.to_datetime(contas[col_venc], dayfirst=True)
        orc[col_data_orc] = pd.to_datetime(orc[col_data_orc], dayfirst=True)

        hoje = datetime.now()

        # =========================
        # BASE CLIENTES
        # =========================

        clientes = vendas.groupby(col_cliente).agg({
            col_data: ['max', 'count'],
            col_valor: 'sum'
        })

        clientes.columns = ['ultima_compra', 'qtd', 'faturamento']
        clientes = clientes.reset_index()
        clientes.rename(columns={col_cliente: 'Cliente'}, inplace=True)

        intervalo = vendas.sort_values(col_data).groupby(col_cliente)[col_data].apply(
            lambda x: x.diff().mean().days if len(x) > 1 else 0
        )

        clientes['intervalo'] = clientes['Cliente'].map(intervalo)
        clientes['dias_sem_comprar'] = (hoje - clientes['ultima_compra']).dt.days

        # =========================
        # ORÇAMENTOS
        # =========================

        orc_aberto = orc[orc[col_status_orc] != 'Concretizado']
        orc_aberto = orc_aberto[orc_aberto[col_data_orc] >= (hoje - pd.Timedelta(days=30))]

        orc_count = orc_aberto.groupby(col_cliente_orc)[col_numero_orc].count()
        clientes['orcamentos'] = clientes['Cliente'].map(orc_count).fillna(0)

        # =========================
        # INADIMPLÊNCIA
        # =========================

        contas_atraso = contas[contas[col_venc] < hoje]
        inad = contas_atraso.groupby(col_cliente_contas)[col_valor_contas].sum()

        clientes['inadimplencia'] = clientes['Cliente'].map(inad).fillna(0)

        # =========================
        # IA
        # =========================

        clientes['acao'] = clientes.apply(
            lambda x: sugestao_ia(x['dias_sem_comprar'], x['intervalo'], x['orcamentos']),
            axis=1
        )

        # =========================
        # ABAS
        # =========================

        aba1, aba2, aba3, aba4, aba5 = st.tabs([
            "📌 Prioridade",
            "📊 Resumo",
            "📂 Orçamentos",
            "📈 Gestão",
            "📋 Base"
        ])

        prioridade = clientes[
            clientes['dias_sem_comprar'] >= clientes['intervalo'] * 0.8
        ].sort_values('dias_sem_comprar', ascending=False)

        # =========================
        # PRIORIDADE
        # =========================

        with aba1:
            for _, row in prioridade.iterrows():
                atraso = int(row['dias_sem_comprar'] - row['intervalo'])

                st.markdown(f"""
**{row['Cliente']}**

Compra a cada {int(row['intervalo'])} dias  
Está há {int(row['dias_sem_comprar'])} dias sem comprar  
Já era para ter comprado há {max(atraso,0)} dias  

📌 {row['acao']}
---
""")

        # =========================
        # RESUMO
        # =========================

        with aba2:
            capacidade = prioridade['faturamento'].sum()

            st.markdown(f"""
**Capacidade de venda hoje:** **{formatar_real(capacidade)}**
""")

            for _, row in prioridade.iterrows():
                st.markdown(f"""
**{row['Cliente']}**

Compra a cada {int(row['intervalo'])} dias  
Está há {int(row['dias_sem_comprar'])} dias sem comprar  

📌 {row['acao']}
---
""")

        # =========================
        # ORÇAMENTOS
        # =========================

        with aba3:
            st.dataframe(orc_aberto[[col_cliente_orc, col_numero_orc, col_data_orc]])

        # =========================
        # GESTÃO
        # =========================

        with aba4:
            total_inad = clientes['inadimplencia'].sum()

            st.markdown(f"""
**Inadimplência total:** **{formatar_real(total_inad)}**
""")

        # =========================
        # BASE
        # =========================

        with aba5:
            clientes['faturamento'] = clientes['faturamento'].apply(formatar_real)
            clientes['inadimplencia'] = clientes['inadimplencia'].apply(formatar_real)

            st.dataframe(clientes)

    except Exception as e:
        st.error(f"Erro ao processar: {e}")
