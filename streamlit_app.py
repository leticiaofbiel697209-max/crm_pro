import streamlit as st
import pandas as pd
from datetime import date, datetime, timedelta
from io import BytesIO
from html import escape
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import gspread
from google.oauth2.service_account import Credentials

try:
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    )
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    REPORTLAB_OK = True
except Exception:
    REPORTLAB_OK = False

st.set_page_config(layout="wide")
st.title("📊 CRM Inteligente - Nível CEO")

if "dados_processados" not in st.session_state:
    st.session_state.dados_processados = None
if "clientes_ligados" not in st.session_state:
    st.session_state.clientes_ligados = set()
if "observacoes_orc" not in st.session_state:
    st.session_state.observacoes_orc = {}
if "gestaoclick_lojas" not in st.session_state:
    st.session_state.gestaoclick_lojas = []
if "gestaoclick_usuarios" not in st.session_state:
    st.session_state.gestaoclick_usuarios = []

NOME_PLANILHA = "CRM_HISTORICO_LUKATONER"
USUARIO_PADRAO = "Gabriel"
API_BASE = "https://api.gestaoclick.com"

class GestaoClickAPI:
    def __init__(self, access_token, secret_token):
        self.headers = {
            "Content-Type": "application/json",
            "access-token": access_token,
            "secret-access-token": secret_token,
        }
        self.last_request = 0.0

    def request(self, path, params=None, method="GET", body=None):
        elapsed = time.monotonic() - self.last_request
        if elapsed < 0.36:
            time.sleep(0.36 - elapsed)

        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url, data=data, headers=self.headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"GestãoClick retornou erro {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Não foi possível acessar o GestãoClick: {exc.reason}"
            ) from exc
        finally:
            self.last_request = time.monotonic()

        if payload.get("status") != "success":
            raise RuntimeError(
                payload.get("message") or "Resposta inesperada do GestãoClick."
            )
        return payload

    def list_all(self, path, params=None):
        records = []
        page = 1
        while True:
            query = dict(params or {})
            query.update({"pagina": page, "limite": 100})
            payload = self.request(path, query)
            page_records = payload.get("data") or []
            records.extend(page_records)
            meta = payload.get("meta") or {}
            if not meta.get("proxima_pagina") and len(page_records) < 100:
                break
            page += 1
            if page > 200:
                raise RuntimeError("A consulta excedeu 200 páginas.")
        return records

    def stores(self):
        return self.list_all("/lojas")

    def users(self, store_id):
        return self.list_all("/usuarios", {"loja_id": store_id})

    def sales(self, start_date, end_date, store_id):
        return self.list_all("/vendas", {
            "loja_id": store_id,
            "data_inicio": start_date.isoformat(),
            "data_fim": end_date.isoformat(),
        })

    def budgets(self, start_date, end_date, store_id):
        return self.list_all("/orcamentos", {
            "loja_id": store_id,
            "data_inicio": start_date.isoformat(),
            "data_fim": end_date.isoformat(),
        })

    def overdue_receivables(self, end_date, store_id):
        return self.list_all("/recebimentos", {
            "loja_id": store_id,
            "data_fim": end_date.isoformat(),
            "liquidado": "at",
        })

    def budget(self, budget_id, store_id):
        return self.request(
            f"/orcamentos/{budget_id}", {"loja_id": store_id}
        ).get("data") or {}

    @staticmethod
    def prepare_budget(budget):
        budget["tipo"] = (
            "servico"
            if budget.get("servicos") and not budget.get("produtos")
            else "produto"
        )
        for wrapper in budget.get("produtos") or []:
            product = wrapper.get("produto") or {}
            if not product.get("id") and product.get("produto_id"):
                product["id"] = product["produto_id"]
        return budget

    def append_budget_note(self, budget_id, store_id, note, user):
        budget = self.budget(budget_id, store_id)
        if not budget:
            raise RuntimeError("O orçamento não foi encontrado no GestãoClick.")

        timestamp = datetime.now().strftime("%d/%m/%Y %H:%M")
        entry = f"[CRM {timestamp}] {user} | {note.strip()}"
        previous = str(budget.get("observacoes_interna") or "").strip()
        budget["observacoes_interna"] = f"{previous}\n{entry}".strip()
        budget = self.prepare_budget(budget)
        return self.request(
            f"/orcamentos/{budget_id}",
            {"loja_id": store_id},
            method="PUT",
            body=budget,
        ).get("data") or {}

def credenciais_gestaoclick():
    try:
        config = st.secrets.get("gestaoclick", {})
        access = str(config.get("access_token", "")).strip()
        secret = str(config.get("secret_token", "")).strip()
    except Exception:
        access = ""
        secret = ""

    access = str(st.session_state.get("gc_access_token", access)).strip()
    secret = str(st.session_state.get("gc_secret_token", secret)).strip()
    return access, secret

def api_gestaoclick():
    access, secret = credenciais_gestaoclick()
    if not access or not secret:
        raise RuntimeError("Informe os dois tokens da API do GestãoClick.")
    return GestaoClickAPI(access, secret)

def fmt(v):
    try:
        return f"R${float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return "R$0,00"

def fmt_html(v):
    return fmt(v).replace("$", "&#36;")

def html_seguro(v):
    return escape(str(v), quote=True)

def norm(x):
    return str(x).strip().lower().replace("º", "o").replace("°", "o")

def conectar_google_sheets():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    service_account_info = dict(st.secrets["gcp_service_account"])

    service_account_info["private_key"] = (
        service_account_info["private_key"]
        .replace("\\n", "\n")
        .strip()
    )

    creds = Credentials.from_service_account_info(
        service_account_info,
        scopes=scope
    )

    gc = gspread.authorize(creds)

    return gc.open(NOME_PLANILHA)

def aba_sheets(nome):
    planilha = conectar_google_sheets()
    return planilha.worksheet(nome)

def carregar_clientes_ligados_hoje():
    try:
        ws = aba_sheets("clientes_ligados")
        dados = ws.get_all_records()
        hoje = datetime.now().strftime("%d/%m/%Y")
        return {str(l["cliente"]).strip() for l in dados if str(l.get("data", "")).strip() == hoje}
    except Exception:
        return set()

def salvar_cliente_ligado(cliente, origem):
    try:
        ws = aba_sheets("clientes_ligados")
        hoje = datetime.now().strftime("%d/%m/%Y")
        ws.append_row([hoje, cliente, USUARIO_PADRAO, origem])
    except Exception as e:
        st.warning(f"Não consegui salvar no Google Sheets: {e}")

def carregar_observacoes_orcamentos():
    try:
        ws = aba_sheets("orcamentos_observacoes")
        dados = ws.get_all_records()
        obs = {}
        for l in dados:
            num = str(l.get("numero_orcamento", "")).strip()
            if num:
                obs[num] = str(l.get("observacao", ""))
        return obs
    except Exception:
        return {}

def salvar_observacao_orcamento(numero, cliente, observacao):
    try:
        ws = aba_sheets("orcamentos_observacoes")
        hoje = datetime.now().strftime("%d/%m/%Y")
        registros = ws.get_all_records()
        numero = str(numero)

        linha_existente = None
        for i, r in enumerate(registros, start=2):
            if str(r.get("numero_orcamento", "")).strip() == numero:
                linha_existente = i
                break

        if linha_existente:
            ws.update(f"A{linha_existente}:E{linha_existente}", [[numero, cliente, observacao, USUARIO_PADRAO, hoje]])
        else:
            ws.append_row([numero, cliente, observacao, USUARIO_PADRAO, hoje])
    except Exception as e:
        st.warning(f"Não consegui salvar observação: {e}")

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
    def converter(v):
        if pd.isna(v):
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)

        texto = re.sub(r"[^\d,.\-]", "", str(v).strip())
        if not texto:
            return 0.0

        if "," in texto and "." in texto:
            if texto.rfind(",") > texto.rfind("."):
                texto = texto.replace(".", "").replace(",", ".")
            else:
                texto = texto.replace(",", "")
        elif "," in texto:
            texto = texto.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", texto):
            texto = texto.replace(".", "")

        try:
            return float(texto)
        except ValueError:
            return 0.0

    return s.apply(converter)

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
        if dias <= 30:
            return "🟣 NOVO"
        if dias <= 60:
            return "🟡 ATENÇÃO"
        return "⚫ CLIENTE INATIVO"
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

def processar_dataframes(vendas, orc, contas):
    hoje = datetime.now()

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
    status_fechado = (
        "CONCRETIZADO|CANCELADO|PERDIDO|REPROVADO|FATURADO|"
        "FINALIZADO|FECHADO|VENDIDO"
    )
    orc_aberto = orc_aberto[
        ~orc_aberto[co_status].astype(str).str.upper().str.contains(
            status_fechado, na=False, regex=True
        )
    ]
    orc_aberto = orc_aberto[
        orc_aberto[co_data] >= (hoje - pd.Timedelta(days=30))
    ].copy()

    orc_aberto["dias_no_sistema"] = (hoje - orc_aberto[co_data]).dt.days
    orc_aberto["acao_recomendada_orcamento"] = orc_aberto["dias_no_sistema"].apply(status_orcamento)

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

    clientes["temperatura"] = clientes.apply(lambda x: temperatura_cliente(x["dias_sem_comprar"], x["intervalo"]), axis=1)

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
        "periodo_inicio": vendas[cv_data].min(),
        "periodo_fim": vendas[cv_data].max(),
    }

def processar_dados(vendas_file, orc_file, contas_file):
    vendas = carregar_excel(vendas_file, [["cliente"], ["data"], ["valor"]])
    orc = carregar_excel(
        orc_file,
        [["nº", "n°", "numero", "número"], ["cliente"], ["data"], ["situação", "status"]]
    )
    contas = carregar_excel(
        contas_file,
        [["cliente", "destinado"], ["vencimento"], ["valor"], ["situação", "status"]]
    )
    dados = processar_dataframes(vendas, orc, contas)
    dados["origem"] = "excel"
    return dados

def api_para_dataframes(vendas_api, orcamentos_api, recebimentos_api, vendedor_id=None):
    if vendedor_id:
        vendas_api = [
            item for item in vendas_api
            if str(item.get("vendedor_id") or "") == str(vendedor_id)
        ]
        orcamentos_api = [
            item for item in orcamentos_api
            if str(item.get("vendedor_id") or "") == str(vendedor_id)
        ]

    vendas = pd.DataFrame([{
        "Cliente": item.get("nome_cliente") or "Cliente sem nome",
        "Data": pd.to_datetime(item.get("data"), format="%Y-%m-%d", errors="coerce"),
        "Valor": item.get("valor_total") or 0,
        "Vendedor": item.get("nome_vendedor") or "Sem vendedor",
        "Vendedor ID": item.get("vendedor_id"),
    } for item in vendas_api])

    orcamentos = pd.DataFrame([{
        "Numero": item.get("codigo") or item.get("id"),
        "Cliente": item.get("nome_cliente") or "Cliente sem nome",
        "Data": pd.to_datetime(item.get("data"), format="%Y-%m-%d", errors="coerce"),
        "Situacao": item.get("nome_situacao") or "",
        "Valor": item.get("valor_total") or 0,
        "Vendedor": item.get("nome_vendedor") or "Sem vendedor",
        "_orcamento_id": item.get("id"),
        "_observacoes_interna": item.get("observacoes_interna") or "",
    } for item in orcamentos_api])

    contas = pd.DataFrame([{
        "Cliente": item.get("nome_cliente") or "Cliente sem nome",
        "Vencimento": pd.to_datetime(
            item.get("data_vencimento"), format="%Y-%m-%d", errors="coerce"
        ),
        "Valor Total": item.get("valor_total") or item.get("valor") or 0,
        "Situacao": "ATRASADO",
    } for item in recebimentos_api])

    if vendas.empty:
        vendas = pd.DataFrame(columns=["Cliente", "Data", "Valor", "Vendedor", "Vendedor ID"])
    if orcamentos.empty:
        orcamentos = pd.DataFrame(columns=[
            "Numero", "Cliente", "Data", "Situacao", "Valor", "Vendedor",
            "_orcamento_id", "_observacoes_interna"
        ])
    if contas.empty:
        contas = pd.DataFrame(columns=["Cliente", "Vencimento", "Valor Total", "Situacao"])
    return vendas, orcamentos, contas

def processar_api(api, inicio, fim, loja_id, vendedor_id=None, vendedor_nome="Todos"):
    vendas_api = api.sales(inicio, fim, loja_id)
    orcamentos_api = api.budgets(max(inicio, fim - timedelta(days=30)), fim, loja_id)
    recebimentos_api = api.overdue_receivables(fim, loja_id)
    vendas, orcamentos, contas = api_para_dataframes(
        vendas_api, orcamentos_api, recebimentos_api, vendedor_id
    )
    if vendas.empty:
        raise RuntimeError("Nenhuma venda foi encontrada para os filtros selecionados.")

    dados = processar_dataframes(vendas, orcamentos, contas)
    dados.update({
        "origem": "api",
        "loja_id": str(loja_id),
        "vendedor_id": str(vendedor_id or ""),
        "vendedor_nome": vendedor_nome,
        "atualizado_em": datetime.now(),
    })
    return dados

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

def calcular_churn(clientes):
    clientes_com_ciclo = clientes[clientes["intervalo"] > 0]
    if clientes_com_ciclo.empty:
        return 0.0, 0, 0

    clientes_churn = clientes_com_ciclo[
        clientes_com_ciclo["dias_sem_comprar"] > clientes_com_ciclo["intervalo"] * 2
    ]
    taxa = len(clientes_churn) / len(clientes_com_ciclo) * 100
    return taxa, len(clientes_churn), len(clientes_com_ciclo)

def listar_clientes_churn(clientes):
    churn = clientes[
        (clientes["intervalo"] > 0) &
        (clientes["dias_sem_comprar"] > clientes["intervalo"] * 2)
    ].copy()
    churn["limite_churn_dias"] = (churn["intervalo"] * 2).round().astype(int)
    churn["dias_alem_limite"] = (
        churn["dias_sem_comprar"] - churn["limite_churn_dias"]
    ).clip(lower=0).astype(int)
    return churn.sort_values(
        ["potencial_mensal", "dias_alem_limite"],
        ascending=[False, False]
    )

def card_cliente(row, tipo):
    atraso = int(row["dias_sem_comprar"] - row["intervalo"])
    estrela = "⭐ Cliente estratégico<br>" if row["cliente_estrategico"] else ""
    cliente_html = html_seguro(row["Cliente"])
    temperatura_html = html_seguro(row["temperatura"])
    risco_html = html_seguro(row["risco_inadimplencia"])
    acao_html = html_seguro(row["acao_ia"])

    st.markdown(f"""
<div style="background:white;padding:15px;border-radius:10px;margin-bottom:10px;border:1px solid #ddd;">
<b>{cliente_html}</b><br>
{estrela}
Temperatura: <b>{temperatura_html}</b><br>
Score comercial: <b>{int(row['score_comercial'])}/100</b><br><br>
Compra a cada <b>{int(row['intervalo'])} dias</b><br>
Está há <b>{int(row['dias_sem_comprar'])} dias</b> sem comprar<br>
Já era para ter comprado há <b>{max(atraso, 0)} dias</b><br><br>
Ticket médio: <b>{fmt_html(row['ticket_medio'])}</b><br>
Potencial mensal: <b>{fmt_html(row['potencial_mensal'])}</b><br>
Potencial recuperável: <b>{fmt_html(row['potencial_recuperavel'])}</b><br>
Orçamentos em aberto: <b>{int(row['orcamentos_em_aberto'])}</b><br>
Inadimplência: <b>{fmt_html(row['inadimplencia'])}</b><br>
Score de risco: <b>{int(row['score_risco'])}/100 — {risco_html}</b><br><br>
Recomendação: <b>{acao_html}</b>
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
        salvar_cliente_ligado(row["Cliente"], tipo)
        st.rerun()

def gerar_texto_email(
    prioridade, orc_aberto, clientes, clientes_churn,
    co_num, co_cli, co_valor, periodo_inicio, periodo_fim
):
    hoje_txt = datetime.now().strftime("%d/%m/%Y")
    taxa_churn, qtd_churn, base_churn = calcular_churn(clientes)
    periodo = f"{periodo_inicio:%d/%m/%Y} a {periodo_fim:%d/%m/%Y}"
    orc_urgentes = orc_aberto[orc_aberto["dias_no_sistema"] >= 2].sort_values(
        "dias_no_sistema", ascending=False
    )
    temperaturas = clientes["temperatura"].value_counts()

    linhas = [
        f"RESUMO COMERCIAL DIÁRIO - {hoje_txt}",
        f"Período das vendas analisadas: {periodo}",
        "",
        "VISÃO EXECUTIVA",
        f"- Faturamento histórico importado: {fmt(clientes['faturamento'].sum())}",
        f"- Potencial mensal da carteira: {fmt(clientes['potencial_mensal'].sum())}",
        f"- Capacidade estimada das prioridades de hoje: {fmt(prioridade['ticket_medio'].sum())}",
        f"- Potencial recuperável: {fmt(clientes['potencial_recuperavel'].sum())}",
        f"- Inadimplência identificada: {fmt(clientes['inadimplencia'].sum())}",
        f"- Churn estimado: {taxa_churn:.1f}% ({qtd_churn} de {base_churn} clientes com ciclo conhecido)",
        "",
        "CARTEIRA",
        f"- Quentes: {int(temperaturas.get('🟢 QUENTE', 0))}",
        f"- Em atenção: {int(temperaturas.get('🟡 ATENÇÃO', 0))}",
        f"- Atrasados na recompra: {int(temperaturas.get('🔴 ATRASADO NA RECOMPRA', 0))}",
        f"- Inativos: {int(temperaturas.get('⚫ CLIENTE INATIVO', 0))}",
        "",
        f"PRIORIDADES DE HOJE ({len(prioridade)})"
    ]

    if prioridade.empty:
        linhas.append("- Nenhum cliente no timing ideal.")
    else:
        for i, (_, r) in enumerate(prioridade.head(10).iterrows(), 1):
            linhas.append(
                f"{i}. {r['Cliente']} | Ticket {fmt(r['ticket_medio'])} | "
                f"Potencial {fmt(r['potencial_mensal'])} | {r['acao_ia']}"
            )

    linhas.extend(["", f"ORÇAMENTOS URGENTES ({len(orc_urgentes)})"])
    if orc_urgentes.empty:
        linhas.append("- Nenhum orçamento com dois dias ou mais sem retorno.")
    else:
        for i, (_, r) in enumerate(orc_urgentes.head(10).iterrows(), 1):
            valor = fmt(r[co_valor]) if co_valor else "valor não informado"
            linhas.append(
                f"{i}. Nº {r[co_num]} | {r[co_cli]} | {int(r['dias_no_sistema'])} dias | {valor}"
            )

    linhas.extend(["", f"CHURN PARA RECUPERAÇÃO ({len(clientes_churn)})"])
    if clientes_churn.empty:
        linhas.append("- Nenhum cliente classificado em churn.")
    else:
        for i, (_, r) in enumerate(clientes_churn.head(10).iterrows(), 1):
            linhas.append(
                f"{i}. {r['Cliente']} | {int(r['dias_sem_comprar'])} dias sem comprar | "
                f"Potencial em risco {fmt(r['potencial_mensal'])}"
            )

    linhas.extend([
        "",
        "PLANO DO DIA",
        f"- Realizar {len(prioridade)} contatos prioritários.",
        f"- Retornar {len(orc_urgentes)} orçamentos urgentes.",
        f"- Iniciar recuperação dos {min(len(clientes_churn), 10)} clientes de churn com maior potencial.",
        "- Tratar inadimplência antes de oferecer nova venda aos clientes com pendências.",
        "",
        "Observação: capacidade estimada não é previsão garantida; representa a soma dos tickets médios das prioridades."
    ])
    return "\n".join(linhas)

def gerar_pdf(
    prioridade, orc_aberto, clientes, clientes_churn,
    co_num, co_cli, co_valor, periodo_inicio, periodo_fim
):
    if not REPORTLAB_OK:
        return None

    taxa_churn, qtd_churn, base_churn = calcular_churn(clientes)
    periodo = f"{periodo_inicio:%d/%m/%Y} a {periodo_fim:%d/%m/%Y}"
    orc_urgentes = orc_aberto[orc_aberto["dias_no_sistema"] >= 2].sort_values(
        "dias_no_sistema", ascending=False
    )

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=16 * mm,
        title="Relatório Comercial Executivo"
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="TituloCEO", parent=styles["Title"], fontSize=20, leading=24,
        textColor=colors.HexColor("#17324D"), alignment=TA_CENTER, spaceAfter=6
    ))
    styles.add(ParagraphStyle(
        name="SecaoCEO", parent=styles["Heading1"], fontSize=13, leading=16,
        textColor=colors.HexColor("#17324D"), spaceBefore=10, spaceAfter=7
    ))
    styles.add(ParagraphStyle(
        name="Pequeno", parent=styles["BodyText"], fontSize=8, leading=10
    ))
    styles.add(ParagraphStyle(
        name="CabecalhoTabela", parent=styles["Pequeno"],
        textColor=colors.white, fontName="Helvetica-Bold"
    ))
    elementos = []

    def p(valor, estilo="Pequeno"):
        texto = re.sub(r"[\U00010000-\U0010ffff]", "", str(valor)).strip()
        return Paragraph(escape(texto), styles[estilo])

    def tabela(dados, larguras=None):
        cabecalho = [
            Paragraph(escape(celula.getPlainText()), styles["CabecalhoTabela"])
            if isinstance(celula, Paragraph)
            else Paragraph(escape(str(celula)), styles["CabecalhoTabela"])
            for celula in dados[0]
        ]
        dados = [cabecalho] + dados[1:]
        tabela_pdf = Table(dados, colWidths=larguras, repeatRows=1, hAlign="LEFT")
        tabela_pdf.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17324D")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B8C2CC")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6F8")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        return tabela_pdf

    elementos.append(Paragraph("RELATÓRIO COMERCIAL EXECUTIVO", styles["TituloCEO"]))
    elementos.append(Paragraph(
        f"Emitido em {datetime.now():%d/%m/%Y} | Vendas analisadas: {periodo}",
        styles["Normal"]
    ))
    elementos.append(Spacer(1, 8))

    indicadores = [
        [p("Indicador"), p("Resultado"), p("Leitura")],
        [p("Faturamento histórico"), p(fmt(clientes["faturamento"].sum())), p("Total existente no arquivo importado.")],
        [p("Potencial mensal"), p(fmt(clientes["potencial_mensal"].sum())), p("Média mensal das compras dos últimos três meses.")],
        [p("Capacidade das prioridades"), p(fmt(prioridade["ticket_medio"].sum())), p("Soma dos tickets médios; não é previsão garantida.")],
        [p("Potencial recuperável"), p(fmt(clientes["potencial_recuperavel"].sum())), p("Potencial de atrasados e inativos.")],
        [p("Inadimplência"), p(fmt(clientes["inadimplencia"].sum())), p("Pendências identificadas no contas a receber.")],
        [p("Churn estimado"), p(f"{taxa_churn:.1f}%"), p(f"{qtd_churn} de {base_churn} clientes com ciclo conhecido.")],
    ]
    elementos.append(Paragraph("1. Painel executivo", styles["SecaoCEO"]))
    elementos.append(tabela(indicadores, [45 * mm, 35 * mm, 80 * mm]))

    temperaturas = clientes["temperatura"].value_counts()
    carteira = [
        [p("Situação"), p("Clientes")],
        [p("Quentes"), p(int(temperaturas.get("🟢 QUENTE", 0)))],
        [p("Em atenção"), p(int(temperaturas.get("🟡 ATENÇÃO", 0)))],
        [p("Atrasados na recompra"), p(int(temperaturas.get("🔴 ATRASADO NA RECOMPRA", 0)))],
        [p("Inativos"), p(int(temperaturas.get("⚫ CLIENTE INATIVO", 0)))],
        [p("Novos"), p(int(temperaturas.get("🟣 NOVO", 0)))],
    ]
    elementos.append(Paragraph("2. Situação da carteira", styles["SecaoCEO"]))
    elementos.append(tabela(carteira, [80 * mm, 35 * mm]))

    elementos.append(Paragraph("3. Prioridades comerciais", styles["SecaoCEO"]))
    prioridades_pdf = [[p("Cliente"), p("Dias"), p("Ticket"), p("Potencial"), p("Recomendação")]]
    for _, r in prioridade.head(20).iterrows():
        prioridades_pdf.append([
            p(r["Cliente"]), p(int(r["dias_sem_comprar"])), p(fmt(r["ticket_medio"])),
            p(fmt(r["potencial_mensal"])), p(r["acao_ia"])
        ])
    if len(prioridades_pdf) == 1:
        elementos.append(Paragraph("Nenhum cliente no timing ideal hoje.", styles["Normal"]))
    else:
        elementos.append(tabela(prioridades_pdf, [38 * mm, 14 * mm, 25 * mm, 27 * mm, 56 * mm]))

    elementos.append(PageBreak())
    elementos.append(Paragraph("4. Churn e receita em risco", styles["SecaoCEO"]))
    elementos.append(Paragraph(
        f"Taxa estimada: <b>{taxa_churn:.1f}%</b>. Um cliente entra em churn quando "
        "possui ciclo de recompra conhecido e ultrapassa duas vezes seu intervalo médio sem comprar.",
        styles["BodyText"]
    ))
    elementos.append(Spacer(1, 6))
    churn_pdf = [[p("Cliente"), p("Sem comprar"), p("Ciclo"), p("Além do limite"), p("Potencial em risco")]]
    for _, r in clientes_churn.head(25).iterrows():
        churn_pdf.append([
            p(r["Cliente"]), p(f"{int(r['dias_sem_comprar'])} dias"),
            p(f"{int(r['intervalo'])} dias"), p(f"{int(r['dias_alem_limite'])} dias"),
            p(fmt(r["potencial_mensal"]))
        ])
    if len(churn_pdf) == 1:
        elementos.append(Paragraph("Nenhum cliente classificado em churn.", styles["Normal"]))
    else:
        elementos.append(tabela(churn_pdf, [48 * mm, 27 * mm, 24 * mm, 30 * mm, 31 * mm]))

    elementos.append(Paragraph("5. Orçamentos que exigem retorno", styles["SecaoCEO"]))
    orc_pdf = [[p("Orçamento"), p("Cliente"), p("Dias"), p("Valor"), p("Prioridade")]]
    for _, r in orc_urgentes.head(25).iterrows():
        orc_pdf.append([
            p(r[co_num]), p(r[co_cli]), p(int(r["dias_no_sistema"])),
            p(fmt(r[co_valor]) if co_valor else "Não informado"), p(r["acao_recomendada_orcamento"])
        ])
    if len(orc_pdf) == 1:
        elementos.append(Paragraph("Nenhum orçamento urgente.", styles["Normal"]))
    else:
        elementos.append(tabela(orc_pdf, [25 * mm, 50 * mm, 15 * mm, 28 * mm, 42 * mm]))

    inadimplentes = clientes[clientes["inadimplencia"] > 0].sort_values(
        "inadimplencia", ascending=False
    )
    elementos.append(Paragraph("6. Inadimplência por cliente", styles["SecaoCEO"]))
    inad_pdf = [[p("Cliente"), p("Valor"), p("Média de atraso"), p("Risco")]]
    for _, r in inadimplentes.head(25).iterrows():
        inad_pdf.append([
            p(r["Cliente"]), p(fmt(r["inadimplencia"])),
            p(f"{int(r['media_dias_atraso'])} dias"), p(r["risco_inadimplencia"])
        ])
    if len(inad_pdf) == 1:
        elementos.append(Paragraph("Nenhuma inadimplência identificada.", styles["Normal"]))
    else:
        elementos.append(tabela(inad_pdf, [55 * mm, 32 * mm, 32 * mm, 41 * mm]))

    elementos.append(Paragraph("7. Plano de ação", styles["SecaoCEO"]))
    elementos.append(Paragraph(
        f"<b>Hoje:</b> realizar {len(prioridade)} contatos prioritários e retornar "
        f"{len(orc_urgentes)} orçamentos urgentes.<br/>"
        f"<b>Próximos 7 dias:</b> acompanhar clientes em atenção e propostas ainda abertas.<br/>"
        f"<b>Recuperação:</b> abordar primeiro os {min(len(clientes_churn), 10)} clientes "
        "em churn com maior potencial mensal e tratar pendências financeiras antes de uma nova oferta.",
        styles["BodyText"]
    ))

    elementos.append(Paragraph("8. Metodologia", styles["SecaoCEO"]))
    elementos.append(Paragraph(
        "<b>Churn estimado:</b> clientes com ciclo conhecido e mais de duas vezes o intervalo "
        "médio sem comprar, dividido pela quantidade de clientes com ciclo conhecido.<br/>"
        "<b>Potencial mensal:</b> compras dos últimos três meses divididas por três.<br/>"
        "<b>Capacidade das prioridades:</b> soma dos tickets médios dos clientes quentes; "
        "não representa promessa de venda.<br/>"
        "<b>Cliente estratégico:</b> cliente situado entre os 10% de maior faturamento histórico.",
        styles["BodyText"]
    ))

    def rodape(canvas, documento):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#667788"))
        canvas.drawString(15 * mm, 9 * mm, "CRM Inteligente - Relatório Comercial")
        canvas.drawRightString(195 * mm, 9 * mm, f"Página {documento.page}")
        canvas.restoreState()

    doc.build(elementos, onFirstPage=rodape, onLaterPages=rodape)
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
    periodo_inicio = dados.get("periodo_inicio", clientes["ultima_compra"].min())
    periodo_fim = dados.get("periodo_fim", clientes["ultima_compra"].max())

    prioridade = montar_prioridade(clientes)
    resumo = montar_resumo(clientes)
    taxa_churn, qtd_churn, base_churn = calcular_churn(clientes)
    clientes_churn = listar_clientes_churn(clientes)

    if dados.get("origem") == "api":
        atualizado = dados.get("atualizado_em")
        texto_atualizacao = atualizado.strftime("%d/%m/%Y %H:%M") if atualizado else "agora"
        st.success(
            f"Dados carregados pela API do GestãoClick | "
            f"Vendedor: {dados.get('vendedor_nome', 'Todos')} | "
            f"Atualizado em {texto_atualizacao}"
        )
    else:
        st.info("Dados carregados por arquivos Excel.")

    aba_ceo, aba_churn, aba_prioridade, aba_resumo, aba_orc, aba_gestao, aba_base, aba_email, aba_relatorio = st.tabs([
        "👑 CEO", "📉 Churn", "🔥 Prioridade", "📋 Resumo", "📄 Orçamentos", "🧠 Gestão", "📊 Base", "✉️ Resumo E-mail", "📧 Relatório Comercial"
    ])

    with aba_ceo:
        st.subheader("👑 Painel CEO")

        col_churn, col_perdidos, col_base = st.columns(3)
        with col_churn:
            st.metric("Taxa de churn estimada", f"{taxa_churn:.1f}%")
        with col_perdidos:
            st.metric("Clientes em churn", qtd_churn)
        with col_base:
            st.metric("Base analisada", base_churn)

        with st.expander("Como a taxa de churn foi calculada?"):
            st.markdown(
                """
                **Fórmula**

                `Taxa de churn = clientes em churn ÷ clientes com ciclo conhecido × 100`

                Um cliente entra em **churn estimado** quando:

                - possui pelo menos duas compras, permitindo calcular seu intervalo médio;
                - está sem comprar há mais de duas vezes o seu intervalo médio de recompra.

                **Exemplo:** se um cliente costuma comprar a cada 30 dias e está há mais
                de 60 dias sem comprar, ele é considerado em churn. Clientes com apenas
                uma compra não entram na base, pois ainda não possuem ciclo conhecido.
                """
            )
            st.write(
                f"Cálculo atual: {qtd_churn} ÷ {base_churn} × 100 = {taxa_churn:.1f}%"
                if base_churn
                else "Ainda não há clientes com histórico suficiente para calcular o churn."
            )

        st.markdown(f"**Receita prevista:** **{fmt(clientes['faturamento'].sum())}**")
        st.caption("Soma do faturamento total existente no relatório de vendas importado. O período depende do arquivo enviado.")
        st.markdown(f"**Potencial mensal da carteira:** **{fmt(clientes['potencial_mensal'].sum())}**")
        st.caption("Média mensal de compras dos últimos 3 meses.")
        st.markdown(f"**Venda possível hoje:** **{fmt(prioridade['ticket_medio'].sum())}**")
        st.caption("Soma do ticket médio dos clientes classificados como QUENTE na aba Prioridade.")
        st.markdown(f"**Potencial recuperável:** **{fmt(clientes['potencial_recuperavel'].sum())}**")
        st.caption("Soma do potencial mensal dos clientes classificados como ATRASADO NA RECOMPRA ou CLIENTE INATIVO.")
        st.markdown(f"**Inadimplência real:** **{fmt(clientes['inadimplencia'].sum())}**")

    with aba_churn:
        st.subheader("📉 Clientes em churn")
        st.caption(
            "Clientes com ciclo de recompra conhecido que estão há mais de duas vezes "
            "o intervalo médio sem comprar."
        )

        col1, col2, col3 = st.columns(3)
        col1.metric("Taxa de churn", f"{taxa_churn:.1f}%")
        col2.metric("Clientes em churn", qtd_churn)
        col3.metric(
            "Potencial mensal em risco",
            fmt(clientes_churn["potencial_mensal"].sum())
        )

        if clientes_churn.empty:
            st.success("Nenhum cliente está classificado em churn.")
        else:
            for _, r in clientes_churn.iterrows():
                cliente_html = html_seguro(r["Cliente"])
                ultima_compra = r["ultima_compra"].strftime("%d/%m/%Y")
                st.markdown(f"""
<div style="background:white;padding:15px;border-radius:10px;margin-bottom:10px;border-left:6px solid #d62728;border-top:1px solid #ddd;border-right:1px solid #ddd;border-bottom:1px solid #ddd;">
<b>{cliente_html}</b><br>
Última compra: <b>{ultima_compra}</b><br>
Está há <b>{int(r['dias_sem_comprar'])} dias</b> sem comprar<br>
Ciclo médio: <b>{int(r['intervalo'])} dias</b><br>
Limite para churn: <b>{int(r['limite_churn_dias'])} dias</b><br>
Passou do limite há: <b>{int(r['dias_alem_limite'])} dias</b><br><br>
Faturamento histórico: <b>{fmt_html(r['faturamento'])}</b><br>
Ticket médio: <b>{fmt_html(r['ticket_medio'])}</b><br>
Potencial mensal em risco: <b>{fmt_html(r['potencial_mensal'])}</b><br>
Inadimplência: <b>{fmt_html(r['inadimplencia'])}</b>
</div>
""", unsafe_allow_html=True)

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
                        valor_txt = fmt_html(r[co_valor]) if co_valor else "Sem valor"
                        chave_obs = f"obs_orc_{r[co_num]}"
                        num_orc = str(r[co_num])
                        num_orc_html = html_seguro(r[co_num])
                        cliente_orc_html = html_seguro(r[co_cli])
                        status_orc_html = html_seguro(r["acao_recomendada_orcamento"])

                        st.markdown(f"""
<div style="background:white;padding:15px;border-radius:10px;margin-bottom:10px;border:1px solid #ddd;">
<b>Orçamento Nº {num_orc_html}</b><br>
Cliente: <b>{cliente_orc_html}</b><br>
Tempo no sistema: <b>{int(r['dias_no_sistema'])} dia(s)</b><br>
Status: <b>{status_orc_html}</b><br>
Valor: <b>{valor_txt}</b>
</div>
""", unsafe_allow_html=True)

                        if dados.get("origem") == "api" and str(r.get("_observacoes_interna", "")).strip():
                            with st.expander("Ver histórico do GestãoClick"):
                                st.text(str(r.get("_observacoes_interna", "")))

                        obs = st.text_area(
                            "Nova observação" if dados.get("origem") == "api" else "Observação",
                            value=st.session_state.observacoes_orc.get(num_orc, ""),
                            key=chave_obs
                        )

                        if st.button(f"💾 Salvar observação {num_orc}", key=f"salvar_obs_{num_orc}"):
                            try:
                                if not obs.strip():
                                    raise RuntimeError("Digite uma observação antes de salvar.")
                                if dados.get("origem") == "api":
                                    orcamento_id = str(r.get("_orcamento_id") or "").strip()
                                    if not orcamento_id:
                                        raise RuntimeError("ID interno do orçamento não encontrado.")
                                    api_gestaoclick().append_budget_note(
                                        orcamento_id,
                                        dados["loja_id"],
                                        obs,
                                        st.session_state.get(
                                            "gc_usuario_nome", USUARIO_PADRAO
                                        )
                                    )
                                    st.session_state.observacoes_orc[num_orc] = obs
                                    st.success("Observação gravada diretamente no GestãoClick.")
                                else:
                                    st.session_state.observacoes_orc[num_orc] = obs
                                    salvar_observacao_orcamento(num_orc, r[co_cli], obs)
                                    st.success("Observação salva no Google Sheets.")
                            except Exception as e:
                                st.error(f"Não foi possível salvar a observação: {e}")

    with aba_gestao:
        st.subheader("🧠 Gestão")
        st.markdown(f"**Clientes analisados:** **{len(clientes)}**")
        st.markdown(f"**Clientes em prioridade:** **{len(prioridade)}**")
        st.markdown(f"**Clientes no resumo:** **{len(resumo)}**")
        st.markdown(f"**Potencial mensal da carteira:** **{fmt(clientes['potencial_mensal'].sum())}**")
        st.markdown(f"**Potencial recuperável:** **{fmt(clientes['potencial_recuperavel'].sum())}**")
        st.markdown(f"**Inadimplência total:** **{fmt(clientes['inadimplencia'].sum())}**")
        st.markdown(f"**Taxa de churn estimada:** **{taxa_churn:.1f}%**")
        st.caption(f"Clientes em churn: {qtd_churn} | Base analisada: {base_churn}")

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
                    cliente_html = html_seguro(r["Cliente"])
                    temperatura_html = html_seguro(r["temperatura"])
                    risco_html = html_seguro(r["risco_inadimplencia"])
                    acao_html = html_seguro(r["acao_ia"])
                    st.markdown(f"""
<div style="background:white;padding:15px;border-radius:10px;margin-bottom:10px;border:1px solid #ddd;">
<b>{cliente_html}</b><br>
{estrela}
Temperatura: <b>{temperatura_html}</b><br>
Score comercial: <b>{int(r['score_comercial'])}/100</b><br>
Faturamento: <b>{fmt_html(r['faturamento'])}</b><br>
Ticket médio: <b>{fmt_html(r['ticket_medio'])}</b><br>
Potencial mensal: <b>{fmt_html(r['potencial_mensal'])}</b><br>
Potencial recuperável: <b>{fmt_html(r['potencial_recuperavel'])}</b><br>
Compras: <b>{int(r['qtd_compras'])}</b><br>
Intervalo médio: <b>{int(r['intervalo'])} dias</b><br>
Última compra: <b>{r['ultima_compra'].strftime('%d/%m/%Y')}</b><br>
Dias sem comprar: <b>{int(r['dias_sem_comprar'])}</b><br>
Orçamentos em aberto: <b>{int(r['orcamentos_em_aberto'])}</b><br>
Inadimplência: <b>{fmt_html(r['inadimplencia'])}</b><br>
Score de risco: <b>{int(r['score_risco'])}/100 — {risco_html}</b><br>
Recomendação: <b>{acao_html}</b>
</div>
""", unsafe_allow_html=True)

    with aba_email:
        st.subheader("✉️ Resumo para E-mail")
        st.caption(
            "Resumo diário e acionável para a equipe: indicadores, prioridades, "
            "orçamentos urgentes, churn e plano do dia."
        )
        texto_email = gerar_texto_email(
            prioridade, orc_aberto, clientes, clientes_churn,
            co_num, co_cli, co_valor, periodo_inicio, periodo_fim
        )
        st.text_area("Texto pronto para enviar:", texto_email, height=650)
        st.download_button(
            "Baixar resumo em .txt",
            texto_email,
            f"Resumo_Comercial_{datetime.now():%d_%m_%Y}.txt",
            "text/plain"
        )

    with aba_relatorio:
        st.subheader("📧 Relatório Comercial")
        st.caption(
            "Relatório executivo completo com período analisado, indicadores, carteira, "
            "prioridades, churn, orçamentos, inadimplência, plano de ação e metodologia."
        )
        pdf = gerar_pdf(
            prioridade, orc_aberto, clientes, clientes_churn,
            co_num, co_cli, co_valor, periodo_inicio, periodo_fim
        )
        if pdf:
            st.download_button(
                "📄 Baixar Relatório Executivo em PDF",
                pdf,
                file_name=f"Relatorio_Comercial_Executivo_{datetime.now():%d_%m_%Y}.pdf",
                mime="application/pdf"
            )
        else:
            st.warning("PDF indisponível. Verifique se 'reportlab' está no requirements.txt.")

st.sidebar.header("Fonte dos dados")
modo_dados = st.sidebar.radio(
    "Como deseja carregar?",
    ["API GestãoClick", "Excel (contingência)"]
)

if modo_dados == "API GestãoClick":
    st.sidebar.subheader("Conexão GestãoClick")
    access_padrao, secret_padrao = credenciais_gestaoclick()
    if "gc_access_token" not in st.session_state:
        st.session_state.gc_access_token = access_padrao
    if "gc_secret_token" not in st.session_state:
        st.session_state.gc_secret_token = secret_padrao
    if "gc_usuario_nome" not in st.session_state:
        st.session_state.gc_usuario_nome = USUARIO_PADRAO

    st.sidebar.text_input(
        "Access token",
        key="gc_access_token",
        type="password"
    )
    st.sidebar.text_input(
        "Secret access token",
        key="gc_secret_token",
        type="password"
    )
    st.sidebar.caption(
        "Em produção, salve os tokens em st.secrets['gestaoclick']."
    )
    st.sidebar.text_input(
        "Nome de quem registra as observações",
        key="gc_usuario_nome"
    )

    if st.sidebar.button("Conectar e carregar lojas"):
        try:
            with st.spinner("Conectando ao GestãoClick..."):
                st.session_state.gestaoclick_lojas = api_gestaoclick().stores()
                st.session_state.gestaoclick_usuarios = []
            st.sidebar.success("Conexão realizada.")
        except Exception as e:
            st.sidebar.error(f"Erro de conexão: {e}")

    lojas = st.session_state.gestaoclick_lojas
    if lojas:
        lojas_validas = [
            loja for loja in lojas
            if str(loja.get("id") or "").strip()
        ]
        loja_escolhida = st.sidebar.selectbox(
            "Loja",
            lojas_validas,
            format_func=lambda loja: (
                loja.get("nome") or loja.get("nome_fantasia") or f"Loja {loja.get('id')}"
            )
        )
        loja_id = str(loja_escolhida.get("id"))

        if st.sidebar.button("Carregar vendedores"):
            try:
                with st.spinner("Carregando vendedores..."):
                    st.session_state.gestaoclick_usuarios = api_gestaoclick().users(loja_id)
                st.sidebar.success("Vendedores carregados.")
            except Exception as e:
                st.sidebar.error(f"Erro ao carregar vendedores: {e}")

        usuarios = [
            usuario for usuario in st.session_state.gestaoclick_usuarios
            if str(usuario.get("id") or "").strip()
            and str(usuario.get("nome") or "").strip()
        ]
        opcoes_vendedor = [{"id": "", "nome": "Todos"}, *usuarios]
        vendedor = st.sidebar.selectbox(
            "Vendedor",
            opcoes_vendedor,
            format_func=lambda item: item.get("nome") or "Sem nome"
        )

        fim_padrao = date.today()
        inicio_padrao = fim_padrao - timedelta(days=365)
        inicio_api = st.sidebar.date_input(
            "Vendas desde",
            value=inicio_padrao,
            max_value=fim_padrao
        )
        fim_api = st.sidebar.date_input(
            "Até",
            value=fim_padrao,
            min_value=inicio_api,
            max_value=fim_padrao
        )
        st.sidebar.caption(
            "Para churn, recomenda-se analisar pelo menos 12 meses de vendas."
        )

        if st.sidebar.button("Atualizar dados do GestãoClick", type="primary"):
            try:
                with st.spinner(
                    "Buscando vendas, orçamentos e contas a receber..."
                ):
                    st.session_state.clientes_ligados = carregar_clientes_ligados_hoje()
                    st.session_state.observacoes_orc = {}
                    st.session_state.dados_processados = processar_api(
                        api_gestaoclick(),
                        inicio_api,
                        fim_api,
                        loja_id,
                        vendedor.get("id") or None,
                        vendedor.get("nome") or "Todos"
                    )
                st.success("Dados atualizados pelo GestãoClick.")
                st.rerun()
            except Exception as e:
                st.error(f"Erro ao buscar dados do GestãoClick: {e}")
    else:
        st.sidebar.info("Conecte a API para selecionar uma loja.")

else:
    st.sidebar.header("Importar arquivos")
    vendas_file = st.sidebar.file_uploader("Relatório de Vendas", type=["xlsx"])
    orc_file = st.sidebar.file_uploader("Relatório de Orçamentos", type=["xlsx"])
    contas_file = st.sidebar.file_uploader("Contas a Receber", type=["xlsx"])

    if st.sidebar.button("Analisar arquivos", type="primary"):
        if not vendas_file or not orc_file or not contas_file:
            st.error("Envie os três arquivos.")
            st.stop()

        try:
            st.session_state.clientes_ligados = carregar_clientes_ligados_hoje()
            st.session_state.observacoes_orc = carregar_observacoes_orcamentos()
            st.session_state.dados_processados = processar_dados(
                vendas_file, orc_file, contas_file
            )
        except Exception as e:
            st.error(f"Erro ao processar: {e}")

if st.session_state.dados_processados is not None:
    renderizar()
else:
    st.info(
        "Conecte o GestãoClick ou use os arquivos Excel na barra lateral."
    )
