import streamlit as st
import pandas as pd
from datetime import datetime
from pathlib import Path

st.set_page_config(layout="wide")
st.title("CRM Inteligente - Nivel CEO")


# =========================
# FUNCOES
# =========================

def formatar_real(valor):
    try:
        return f"R${valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "R$0,00"


def achar_coluna(df, nomes):
    for nome in df.columns:
        for n in nomes:
            if n.lower() in str(nome).lower():
                return nome
    return None


def validar_colunas(colunas):
    faltando = [nome for nome, valor in colunas.items() if valor is None]
    if faltando:
        raise ValueError("Colunas nao encontradas: " + ", ".join(faltando))


def ler_arquivo(uploaded_file, header=0):
    if uploaded_file is None:
        raise ValueError("Envie todos os arquivos antes de analisar.")

    nome = uploaded_file.name.lower()
    extensao = Path(nome).suffix
    uploaded_file.seek(0)

    if extensao == ".xlsx":
        return pd.read_excel(uploaded_file, header=header, engine="openpyxl")

    if extensao == ".xls":
        return pd.read_excel(uploaded_file, header=header, engine="xlrd")

    if extensao in [".csv", ".txt"]:
        try:
            return pd.read_csv(uploaded_file, header=header, sep=None, engine="python", encoding="utf-8")
        except UnicodeDecodeError:
            uploaded_file.seek(0)
            return pd.read_csv(uploaded_file, header=header, sep=None, engine="python", encoding="latin1")

    raise ValueError(
        f"Formato nao suportado para o arquivo '{uploaded_file.name}'. "
        "Use .xlsx, .xls, .csv ou .txt."
    )


def sugestao_ia(dias_sem, intervalo, orcamentos):
    if intervalo == 0:
        return "Cliente novo. Iniciar relacionamento."
    if dias_sem > intervalo:
        return "Cliente em atraso. Contato imediato com proposta direta."
    if dias_sem >= intervalo * 0.8:
        return "Cliente proximo do ciclo de compra. Fazer abordagem consultiva."
    if orcamentos > 0:
        return "Follow-up de orcamento em aberto."
    return "Cliente inativo. Reativacao com condicao especial."


# =========================
# UPLOAD
# =========================

st.sidebar.header("Importar Dados")

tipos_aceitos = ["xlsx", "xls", "csv", "txt"]
vendas_file = st.sidebar.file_uploader("Vendas", type=tipos_aceitos)
orc_file = st.sidebar.file_uploader("Orcamentos", type=tipos_aceitos)
contas_file = st.sidebar.file_uploader("Contas a Receber", type=tipos_aceitos)

if st.sidebar.button("Analisar Dados"):
    try:
        vendas = ler_arquivo(vendas_file)
        contas = ler_arquivo(contas_file)
        orc = ler_arquivo(orc_file, header=1)

        # =========================
        # IDENTIFICAR COLUNAS
        # =========================

        col_cliente = achar_coluna(vendas, ["cliente", "razao", "nome"])
        col_data = achar_coluna(vendas, ["data", "emissao"])
        col_valor = achar_coluna(vendas, ["valor", "total"])

        col_cliente_contas = achar_coluna(contas, ["cliente", "razao", "nome"])
        col_valor_contas = achar_coluna(contas, ["valor", "total"])
        col_venc = achar_coluna(contas, ["venc", "vencimento"])

        col_cliente_orc = achar_coluna(orc, ["cliente", "razao", "nome"])
        col_numero_orc = achar_coluna(orc, ["nº", "n°", "numero", "num", "orcamento"])
        col_data_orc = achar_coluna(orc, ["data", "emissao"])
        col_status_orc = achar_coluna(orc, ["situa", "status"])

        validar_colunas({
            "cliente em Vendas": col_cliente,
            "data em Vendas": col_data,
            "valor em Vendas": col_valor,
            "cliente em Contas a Receber": col_cliente_contas,
            "valor em Contas a Receber": col_valor_contas,
            "vencimento em Contas a Receber": col_venc,
            "cliente em Orcamentos": col_cliente_orc,
            "numero em Orcamentos": col_numero_orc,
            "data em Orcamentos": col_data_orc,
            "status/situacao em Orcamentos": col_status_orc,
        })

        # =========================
        # TRATAMENTO
        # =========================

        vendas[col_data] = pd.to_datetime(vendas[col_data], dayfirst=True, errors="coerce")
        contas[col_venc] = pd.to_datetime(contas[col_venc], dayfirst=True, errors="coerce")
        orc[col_data_orc] = pd.to_datetime(orc[col_data_orc], dayfirst=True, errors="coerce")

        vendas[col_valor] = pd.to_numeric(vendas[col_valor], errors="coerce").fillna(0)
        contas[col_valor_contas] = pd.to_numeric(contas[col_valor_contas], errors="coerce").fillna(0)

        vendas = vendas.dropna(subset=[col_cliente, col_data])
        contas = contas.dropna(subset=[col_cliente_contas, col_venc])
        orc = orc.dropna(subset=[col_cliente_orc, col_data_orc])

        if vendas.empty:
            raise ValueError("A planilha de vendas nao tem linhas validas para analisar.")

        hoje = pd.Timestamp(datetime.now())

        # =========================
        # BASE CLIENTES
        # =========================

        clientes = vendas.groupby(col_cliente).agg({
            col_data: ["max", "count"],
            col_valor: "sum",
        })

        clientes.columns = ["ultima_compra", "qtd", "faturamento"]
        clientes = clientes.reset_index()
        clientes.rename(columns={col_cliente: "Cliente"}, inplace=True)

        intervalo = vendas.sort_values(col_data).groupby(col_cliente)[col_data].apply(
            lambda x: x.diff().mean().days if len(x) > 1 and pd.notna(x.diff().mean()) else 0
        )

        clientes["intervalo"] = clientes["Cliente"].map(intervalo).fillna(0)
        clientes["dias_sem_comprar"] = (hoje - clientes["ultima_compra"]).dt.days

        # =========================
        # ORCAMENTOS
        # =========================

        orc_aberto = orc[orc[col_status_orc].astype(str).str.lower() != "concretizado"]
        orc_aberto = orc_aberto[orc_aberto[col_data_orc] >= (hoje - pd.Timedelta(days=30))]

        orc_count = orc_aberto.groupby(col_cliente_orc)[col_numero_orc].count()
        clientes["orcamentos"] = clientes["Cliente"].map(orc_count).fillna(0)

        # =========================
        # INADIMPLENCIA
        # =========================

        contas_atraso = contas[contas[col_venc] < hoje]
        inad = contas_atraso.groupby(col_cliente_contas)[col_valor_contas].sum()
        clientes["inadimplencia"] = clientes["Cliente"].map(inad).fillna(0)

        # =========================
        # IA
        # =========================

        clientes["acao"] = clientes.apply(
            lambda x: sugestao_ia(x["dias_sem_comprar"], x["intervalo"], x["orcamentos"]),
            axis=1,
        )

        # =========================
        # ABAS
        # =========================

        aba1, aba2, aba3, aba4, aba5 = st.tabs([
            "Prioridade",
            "Resumo",
            "Orcamentos",
            "Gestao",
            "Base",
        ])
