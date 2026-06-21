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
if "alteracao_gestaoclick_pendente" not in st.session_state:
    st.session_state.alteracao_gestaoclick_pendente = None
if "metas_vendedor" not in st.session_state:
    st.session_state.metas_vendedor = {}

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

    def sale_statuses(self, store_id):
        return self.list_all("/situacoes_vendas", {"loja_id": store_id})

    def budgets(self, start_date, end_date, store_id):
        return self.list_all("/orcamentos", {
            "loja_id": store_id,
            "data_inicio": start_date.isoformat(),
            "data_fim": end_date.isoformat(),
        })

    def open_receivables(self, store_id):
        records = []
        seen = set()
        for status in ("ab", "at"):
            for item in self.list_all("/recebimentos", {
                "loja_id": store_id,
                "liquidado": status,
            }):
                key = str(item.get("id") or item.get("codigo") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                copy = dict(item)
                copy["_status_financeiro"] = (
                    "ATRASADO" if status == "at" else "EM ABERTO"
                )
                records.append(copy)
        return records

    def open_payables(self, store_id):
        records = []
        seen = set()
        for status in ("ab", "at"):
            for item in self.list_all("/pagamentos", {
                "loja_id": store_id,
                "liquidado": status,
            }):
                key = str(item.get("id") or item.get("codigo") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                copy = dict(item)
                copy["_status_financeiro"] = (
                    "ATRASADO" if status == "at" else "EM ABERTO"
                )
                records.append(copy)
        return records

    def settled_movements(self, path, start_date, end_date, store_id):
        return self.list_all(path, {
            "loja_id": store_id,
            "data_inicio": start_date.isoformat(),
            "data_fim": end_date.isoformat(),
            "liquidado": "pg",
        })

def deduplicar_registros(registros):
    unicos = {}
    sem_id = []
    for item in registros:
        key = str(item.get("id") or "").strip()
        if key:
            unicos[key] = item
        else:
            sem_id.append(item)
    return list(unicos.values()) + sem_id

def custo_total_venda(item):
    custo = 0.0
    for campo in ("produtos", "servicos"):
        for wrapper in item.get(campo) or []:
            detalhe = wrapper.get("produto") or wrapper.get("servico") or {}
            quantidade = pd.to_numeric(
                pd.Series([detalhe.get("quantidade") or 1]), errors="coerce"
            ).fillna(1).iloc[0]
            custo_unitario = pd.to_numeric(
                pd.Series([detalhe.get("valor_custo") or 0]), errors="coerce"
            ).fillna(0).iloc[0]
            custo += float(quantidade) * float(custo_unitario)
    if custo == 0:
        custo = float(pd.to_numeric(
            pd.Series([item.get("valor_custo") or 0]), errors="coerce"
        ).fillna(0).iloc[0])
    return custo

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

def preparar_financeiro(contas, col_cliente, col_vencimento, col_valor, col_status):
    financeiro = pd.DataFrame({
        "Cliente": contas[col_cliente].astype(str).str.strip(),
        "Vencimento": (
            contas[col_vencimento]
            if col_vencimento
            else pd.Series(pd.NaT, index=contas.index)
        ),
        "Valor": contas[col_valor],
        "Situacao": (
            contas[col_status].astype(str)
            if col_status
            else pd.Series("EM ABERTO", index=contas.index)
        ),
    })
    financeiro["Vencimento"] = pd.to_datetime(
        financeiro["Vencimento"], errors="coerce"
    )
    financeiro["Valor"] = pd.to_numeric(
        financeiro["Valor"], errors="coerce"
    ).fillna(0)
    financeiro["Situacao"] = financeiro["Situacao"].str.upper().str.strip()

    status_pago = "PAGO|LIQUIDADO|RECEBIDO|CONFIRMADO|QUITADO"
    financeiro["Liquidado"] = financeiro["Situacao"].str.contains(
        status_pago, na=False, regex=True
    )
    financeiro = financeiro[
        (~financeiro["Liquidado"]) &
        financeiro["Cliente"].ne("") &
        financeiro["Vencimento"].notna() &
        financeiro["Valor"].gt(0)
    ].copy()

    hoje = pd.Timestamp(date.today())
    financeiro["Dias_para_vencer"] = (
        financeiro["Vencimento"].dt.normalize() - hoje
    ).dt.days
    financeiro["Vencida"] = (
        financeiro["Dias_para_vencer"].lt(0) |
        financeiro["Situacao"].str.contains("ATRASADO|VENCIDO", na=False, regex=True)
    )
    financeiro["Dias_atraso"] = (
        -financeiro["Dias_para_vencer"]
    ).clip(lower=0)

    def faixa(row):
        dias = int(row["Dias_para_vencer"])
        if row["Vencida"]:
            atraso = int(row["Dias_atraso"])
            if atraso <= 7:
                return "Vencido até 7 dias"
            if atraso <= 15:
                return "Vencido de 8 a 15 dias"
            if atraso <= 30:
                return "Vencido de 16 a 30 dias"
            if atraso <= 60:
                return "Vencido de 31 a 60 dias"
            return "Vencido acima de 60 dias"
        if dias <= 7:
            return "A vencer em até 7 dias"
        if dias <= 15:
            return "A vencer de 8 a 15 dias"
        if dias <= 30:
            return "A vencer de 16 a 30 dias"
        if dias <= 60:
            return "A vencer de 31 a 60 dias"
        return "A vencer acima de 60 dias"

    financeiro["Faixa"] = financeiro.apply(faixa, axis=1)
    return financeiro.sort_values(["Vencida", "Vencimento"], ascending=[False, True])

def calcular_metricas_financeiras(financeiro):
    vazio = {
        "total_aberto": 0.0,
        "total_vencido": 0.0,
        "percentual_vencido": 0.0,
        "vence_7": 0.0,
        "vence_15": 0.0,
        "vence_30": 0.0,
        "prazo_medio": 0.0,
        "concentracao_top5": 0.0,
        "clientes_devedores": 0,
    }
    if financeiro is None or financeiro.empty:
        return vazio

    total = float(financeiro["Valor"].sum())
    vencido = float(financeiro.loc[financeiro["Vencida"], "Valor"].sum())
    futuro = financeiro[~financeiro["Vencida"]]
    por_cliente = financeiro.groupby("Cliente")["Valor"].sum().sort_values(ascending=False)
    metricas = dict(vazio)
    metricas.update({
        "total_aberto": total,
        "total_vencido": vencido,
        "percentual_vencido": (vencido / total * 100) if total else 0.0,
        "vence_7": float(futuro.loc[futuro["Dias_para_vencer"].between(0, 7), "Valor"].sum()),
        "vence_15": float(futuro.loc[futuro["Dias_para_vencer"].between(8, 15), "Valor"].sum()),
        "vence_30": float(futuro.loc[futuro["Dias_para_vencer"].between(16, 30), "Valor"].sum()),
        "prazo_medio": float(
            (futuro["Dias_para_vencer"] * futuro["Valor"]).sum() /
            futuro["Valor"].sum()
        ) if futuro["Valor"].sum() else 0.0,
        "concentracao_top5": (
            float(por_cliente.head(5).sum()) / total * 100
        ) if total else 0.0,
        "clientes_devedores": int(
            financeiro.loc[financeiro["Vencida"], "Cliente"].nunique()
        ),
    })
    return metricas

def preparar_contas_pagar(pagamentos):
    colunas = [
        "Fornecedor", "Descricao", "Vencimento", "Valor", "Situacao",
        "Dias_para_vencer", "Vencida", "Dias_atraso", "Faixa",
        "Plano_conta", "Forma_pagamento"
    ]
    if not pagamentos:
        return pd.DataFrame(columns=colunas)

    pagar = pd.DataFrame([{
        "Fornecedor": (
            item.get("nome_fornecedor")
            or item.get("nome_transportadora")
            or item.get("nome_funcionario")
            or item.get("nome_cliente")
            or "Sem fornecedor informado"
        ),
        "Descricao": item.get("descricao") or "",
        "Vencimento": pd.to_datetime(
            item.get("data_vencimento"), format="%Y-%m-%d", errors="coerce"
        ),
        "Valor": item.get("valor_total") or item.get("valor") or 0,
        "Situacao": item.get("_status_financeiro") or "EM ABERTO",
        "Plano_conta": item.get("nome_plano_conta") or "",
        "Forma_pagamento": item.get("nome_forma_pagamento") or "",
    } for item in pagamentos])
    pagar["Valor"] = numero_coluna(pagar["Valor"])
    pagar = pagar[
        pagar["Vencimento"].notna() & pagar["Valor"].gt(0)
    ].copy()
    hoje = pd.Timestamp(date.today())
    pagar["Dias_para_vencer"] = (
        pagar["Vencimento"].dt.normalize() - hoje
    ).dt.days
    pagar["Vencida"] = (
        pagar["Dias_para_vencer"].lt(0) |
        pagar["Situacao"].str.upper().str.contains(
            "ATRASADO|VENCIDO", na=False, regex=True
        )
    )
    pagar["Dias_atraso"] = (-pagar["Dias_para_vencer"]).clip(lower=0)

    def faixa(row):
        dias = int(row["Dias_para_vencer"])
        if row["Vencida"]:
            atraso = int(row["Dias_atraso"])
            if atraso <= 7:
                return "Vencido até 7 dias"
            if atraso <= 30:
                return "Vencido de 8 a 30 dias"
            if atraso <= 60:
                return "Vencido de 31 a 60 dias"
            return "Vencido acima de 60 dias"
        if dias <= 7:
            return "A pagar em até 7 dias"
        if dias <= 15:
            return "A pagar de 8 a 15 dias"
        if dias <= 30:
            return "A pagar de 16 a 30 dias"
        return "A pagar acima de 30 dias"

    pagar["Faixa"] = pagar.apply(faixa, axis=1)
    return pagar.sort_values(["Vencida", "Vencimento"], ascending=[False, True])

def total_movimentos_liquidados(movimentos):
    if not movimentos:
        return 0.0
    valores = pd.Series([
        item.get("valor_total") or item.get("valor") or 0
        for item in movimentos
    ])
    return float(numero_coluna(valores).sum())

def calcular_resultado_financeiro(financeiro, contas_pagar, recebido_mes, pago_mes):
    receber = calcular_metricas_financeiras(financeiro)
    total_pagar = (
        float(contas_pagar["Valor"].sum())
        if contas_pagar is not None and not contas_pagar.empty else 0.0
    )
    pagar_vencido = (
        float(contas_pagar.loc[contas_pagar["Vencida"], "Valor"].sum())
        if contas_pagar is not None and not contas_pagar.empty else 0.0
    )
    pagar_7 = (
        float(contas_pagar.loc[
            (~contas_pagar["Vencida"]) &
            contas_pagar["Dias_para_vencer"].between(0, 7),
            "Valor"
        ].sum())
        if contas_pagar is not None and not contas_pagar.empty else 0.0
    )
    pagar_30 = (
        float(contas_pagar.loc[
            (~contas_pagar["Vencida"]) &
            contas_pagar["Dias_para_vencer"].between(0, 30),
            "Valor"
        ].sum())
        if contas_pagar is not None and not contas_pagar.empty else 0.0
    )
    receber_30 = receber["vence_7"] + receber["vence_15"] + receber["vence_30"]
    resultado_mes = float(recebido_mes) - float(pago_mes)
    margem_caixa = (
        resultado_mes / float(recebido_mes) * 100
        if recebido_mes else 0.0
    )
    return {
        **receber,
        "total_pagar": total_pagar,
        "pagar_vencido": pagar_vencido,
        "pagar_7": pagar_7,
        "pagar_30": pagar_30,
        "saldo_carteira": receber["total_aberto"] - total_pagar,
        "saldo_30_dias": receber_30 - pagar_30,
        "recebido_mes": float(recebido_mes),
        "pago_mes": float(pago_mes),
        "resultado_mes": resultado_mes,
        "margem_caixa": margem_caixa,
    }

def estrategia_financeira(metricas):
    resultado = metricas["resultado_mes"]
    saldo_30 = metricas["saldo_30_dias"]
    vencido_pct = metricas["percentual_vencido"]
    pagar_vencido = metricas["pagar_vencido"]
    dicas = []
    if resultado < 0:
        dicas.append(
            "O mês apresenta prejuízo financeiro: pagamentos liquidados superam "
            "os recebimentos. Congele despesas não essenciais e renegocie vencimentos."
        )
    elif resultado > 0:
        dicas.append(
            "O mês apresenta lucro financeiro. Preserve uma parcela como reserva "
            "antes de ampliar compras, despesas ou retiradas."
        )
    else:
        dicas.append(
            "O resultado financeiro mensal está no ponto de equilíbrio. "
            "Evite novos compromissos fixos até formar margem de segurança."
        )
    if saldo_30 < 0:
        dicas.append(
            f"Há déficit projetado de {fmt(abs(saldo_30))} para os próximos 30 dias. "
            "Antecipe cobranças e negocie fornecedores antes dos vencimentos."
        )
    else:
        dicas.append(
            f"A projeção de 30 dias indica sobra de {fmt(saldo_30)} entre entradas "
            "e saídas já registradas."
        )
    if vencido_pct >= 15:
        dicas.append(
            "A inadimplência está pressionando o caixa. Priorize cobranças por valor, "
            "idade da dívida e probabilidade de recuperação."
        )
    if pagar_vencido > 0:
        dicas.append(
            f"Existem {fmt(pagar_vencido)} em contas a pagar vencidas; regularize "
            "primeiro obrigações críticas para operação e crédito."
        )
    return dicas

def calcular_financeiro_real(dados, configuracao):
    vendas = dados.get("vendas_validas", pd.DataFrame()).copy()
    if vendas.empty:
        return {}
    data_col = achar_coluna(vendas, ["data"])
    valor_col = achar_coluna(vendas, ["valor"])
    custo_col = achar_coluna(vendas, ["custo"])
    fim = pd.Timestamp(dados.get("periodo_fim", date.today()))
    inicio_mes = fim.replace(day=1)
    vendas_mes = vendas[
        (vendas[data_col] >= inicio_mes) & (vendas[data_col] <= fim)
    ].copy()
    receita = float(vendas_mes[valor_col].sum())
    custo = float(vendas_mes[custo_col].sum()) if custo_col else 0.0
    lucro_bruto = receita - custo
    impostos = receita * float(configuracao.get("impostos_pct", 0)) / 100
    folha = float(configuracao.get("folha_mensal", 0))
    despesas_fixas = float(configuracao.get("despesas_fixas", 0))
    outras_despesas = float(configuracao.get("outras_despesas", 0))
    lucro_operacional = (
        lucro_bruto - impostos - folha - despesas_fixas - outras_despesas
    )
    margem_bruta = lucro_bruto / receita * 100 if receita else 0
    margem_operacional = lucro_operacional / receita * 100 if receita else 0

    financeiro = dados.get("financeiro", pd.DataFrame())
    pagar = dados.get("contas_pagar", pd.DataFrame())
    base_caixa = calcular_resultado_financeiro(
        financeiro, pagar, dados.get("recebido_mes", 0), dados.get("pago_mes", 0)
    )
    saldo_inicial = float(configuracao.get("saldo_inicial", 0))
    caixa_projetado_30 = saldo_inicial + base_caixa["saldo_30_dias"]

    cenarios = {}
    for nome, fator_receber in (
        ("Conservador", 0.70), ("Provável", 0.90), ("Otimista", 1.00)
    ):
        cenarios[nome] = (
            saldo_inicial +
            base_caixa["total_aberto"] * fator_receber -
            base_caixa["total_pagar"]
        )
    return {
        "receita_mes": receita,
        "custo_mes": custo,
        "lucro_bruto": lucro_bruto,
        "impostos_estimados": impostos,
        "folha": folha,
        "despesas_fixas": despesas_fixas,
        "outras_despesas": outras_despesas,
        "lucro_operacional": lucro_operacional,
        "margem_bruta": margem_bruta,
        "margem_operacional": margem_operacional,
        "saldo_inicial": saldo_inicial,
        "caixa_projetado_30": caixa_projetado_30,
        "cenarios": cenarios,
        "custos_disponiveis": bool(custo_col and vendas_mes[custo_col].gt(0).any()),
    }

def calcular_gestao_comercial(dados, configuracao):
    vendas = dados.get("vendas_validas", pd.DataFrame()).copy()
    orcamentos = dados.get("orcamentos_todos", pd.DataFrame()).copy()
    if vendas.empty:
        return {}, pd.DataFrame()
    data_col = achar_coluna(vendas, ["data"])
    valor_col = achar_coluna(vendas, ["valor"])
    custo_col = achar_coluna(vendas, ["custo"])
    vendedor_col = achar_coluna(vendas, ["vendedor"])
    fim = pd.Timestamp(dados.get("periodo_fim", date.today()))
    inicio_mes = fim.replace(day=1)
    vendas_mes = vendas[
        (vendas[data_col] >= inicio_mes) & (vendas[data_col] <= fim)
    ].copy()
    if not vendedor_col:
        vendas_mes["Vendedor"] = "Sem vendedor"
        vendedor_col = "Vendedor"
    resumo = vendas_mes.groupby(vendedor_col).agg(
        Faturamento=(valor_col, "sum"),
        Vendas=(valor_col, "count"),
        Ticket_medio=(valor_col, "mean"),
    )
    if custo_col:
        custos = vendas_mes.groupby(vendedor_col)[custo_col].sum()
        resumo["Custo"] = resumo.index.map(custos).fillna(0)
    else:
        resumo["Custo"] = 0.0
    resumo["Margem"] = resumo["Faturamento"] - resumo["Custo"]
    resumo["Margem_pct"] = (
        resumo["Margem"] / resumo["Faturamento"].replace(0, pd.NA) * 100
    ).fillna(0)

    meta_geral = float(configuracao.get("meta_geral", 0))
    metas_vendedor = configuracao.get("metas_vendedor", {})
    resumo["Meta"] = [
        float(metas_vendedor.get(str(vendedor), 0))
        for vendedor in resumo.index
    ]
    resumo["Atingimento_pct"] = (
        resumo["Faturamento"] / resumo["Meta"].replace(0, pd.NA) * 100
    ).fillna(0)
    resumo["Distancia_meta"] = (resumo["Meta"] - resumo["Faturamento"]).clip(lower=0)

    dias_decorridos = max(1, fim.day)
    dias_mes = int((fim + pd.offsets.MonthEnd(0)).day)
    projecao = float(vendas_mes[valor_col].sum()) / dias_decorridos * dias_mes

    conversao = 0.0
    perdidos = 0
    idade_media_abertos = 0.0
    motivos_perda = pd.DataFrame()
    total_orc = len(orcamentos)
    if total_orc:
        status_col = achar_coluna(orcamentos, ["situacao", "status"])
        data_orc_col = achar_coluna(orcamentos, ["data"])
        status = orcamentos[status_col].astype(str).str.upper()
        convertidos = status.str.contains(
            "CONCRETIZ|FATURAD|VENDID|FECHAD|CONFIRMAD", na=False, regex=True
        ).sum()
        perdidos = status.str.contains(
            "PERDID|CANCEL|REPROV", na=False, regex=True
        ).sum()
        conversao = convertidos / total_orc * 100
        abertos = ~status.str.contains(
            "CONCRETIZ|FATURAD|VENDID|FECHAD|CONFIRMAD|PERDID|CANCEL|REPROV",
            na=False, regex=True
        )
        if data_orc_col and abertos.any():
            idade_media_abertos = float(
                (fim - pd.to_datetime(
                    orcamentos.loc[abertos, data_orc_col], errors="coerce"
                )).dt.days.mean()
            )
        notas_col = achar_coluna(orcamentos, ["observacoes internas", "observacoes"])
        if notas_col and perdidos:
            perdas = orcamentos[status.str.contains(
                "PERDID|CANCEL|REPROV", na=False, regex=True
            )].copy()
            perdas["Motivo informado"] = perdas[notas_col].astype(str).str.strip()
            perdas.loc[
                perdas["Motivo informado"].isin(["", "nan", "None"]),
                "Motivo informado"
            ] = "Não informado"
            motivos_perda = (
                perdas["Motivo informado"].value_counts()
                .head(10).rename_axis("Motivo").reset_index(name="Quantidade")
            )
    indicadores = {
        "meta_geral": meta_geral,
        "realizado": float(vendas_mes[valor_col].sum()),
        "projecao": projecao,
        "distancia_meta": max(0, meta_geral - float(vendas_mes[valor_col].sum())),
        "conversao_orcamentos": conversao,
        "orcamentos_total": total_orc,
        "orcamentos_perdidos": int(perdidos),
        "idade_media_abertos": idade_media_abertos,
        "motivos_perda": motivos_perda,
    }
    return indicadores, resumo.reset_index().rename(columns={vendedor_col: "Vendedor"})

def calcular_churn_avancado(dados):
    clientes = dados["clientes"].copy()
    vendas = dados.get("vendas_validas", pd.DataFrame()).copy()
    if "Cliente ID" not in clientes.columns:
        clientes["Cliente ID"] = clientes["Cliente"].map(norm)
    churn = listar_clientes_churn(clientes)
    faturamento_total = float(clientes["faturamento"].sum())
    churn_ponderado = (
        float(churn["faturamento"].sum()) / faturamento_total * 100
        if faturamento_total else 0.0
    )
    migrando = clientes[
        (clientes["intervalo"] > 0) &
        (clientes["dias_sem_comprar"] > clientes["intervalo"] * 1.2) &
        (clientes["dias_sem_comprar"] <= clientes["intervalo"] * 2)
    ].copy()

    recuperados = set()
    sazonais = set()
    tendencia = []
    if not vendas.empty:
        data_col = achar_coluna(vendas, ["data"])
        for chave, grupo in vendas.sort_values(data_col).groupby("_cliente_chave"):
            intervalos = grupo[data_col].diff().dt.days.dropna()
            if len(intervalos) >= 3:
                media = intervalos.mean()
                desvio = intervalos.std()
                if media > 0 and desvio / media > 0.65:
                    sazonais.add(str(chave))
                if any(intervalos.iloc[:-1] > media * 2):
                    recuperados.add(str(chave))
        fim = pd.Timestamp(dados.get("periodo_fim", date.today()))
        for meses_atras in range(5, -1, -1):
            referencia = (fim - pd.DateOffset(months=meses_atras)) + pd.offsets.MonthEnd(0)
            base = vendas[vendas[data_col] <= referencia]
            conhecidos = 0
            churn_mes = 0
            for _chave, grupo in base.sort_values(data_col).groupby("_cliente_chave"):
                datas = grupo[data_col].dropna()
                if len(datas) < 2:
                    continue
                intervalo = datas.diff().dt.days.dropna().mean()
                if intervalo <= 0:
                    continue
                conhecidos += 1
                if (referencia - datas.max()).days > intervalo * 2:
                    churn_mes += 1
            tendencia.append({
                "Mês": referencia.strftime("%m/%Y"),
                "Churn %": churn_mes / conhecidos * 100 if conhecidos else 0.0
            })
    clientes["sazonal"] = clientes["Cliente ID"].astype(str).isin(sazonais)
    if "Cliente ID" not in churn.columns:
        churn["Cliente ID"] = churn["Cliente"].map(norm)
    churn["sazonal"] = churn["Cliente ID"].astype(str).isin(sazonais)
    return {
        "churn_ponderado": churn_ponderado,
        "migrando": migrando,
        "recuperados_historicos": len(recuperados),
        "taxa_recuperacao_historica": (
            len(recuperados) / max(1, int((clientes["intervalo"] > 0).sum())) * 100
        ),
        "sazonais": len(sazonais),
        "tendencia_mensal": pd.DataFrame(tendencia),
        "clientes_churn": churn,
        "clientes": clientes,
    }

def processar_dataframes(vendas, orc, contas):
    hoje = datetime.now()

    cv_cli = achar_coluna(vendas, ["cliente"])
    cv_cli_id = achar_coluna(vendas, ["cliente id"])
    cv_data = achar_coluna(vendas, ["data"])
    cv_valor = achar_coluna(vendas, ["valor"])
    cv_custo = achar_coluna(vendas, ["custo"])
    cv_status = achar_coluna(vendas, ["situacao", "status"])
    co_num = achar_coluna(orc, ["nº", "n°", "numero", "número"])
    co_cli = achar_coluna(orc, ["cliente"])
    co_data = achar_coluna(orc, ["data"])
    co_status = achar_coluna(orc, ["situação", "situacao", "status"])
    co_valor = achar_coluna(orc, ["valor"])
    cc_cli = achar_coluna(contas, ["cliente", "destinado"])
    cc_cli_id = achar_coluna(contas, ["cliente id"])
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
    if cv_custo:
        vendas[cv_custo] = numero_coluna(vendas[cv_custo])
    vendas = vendas.dropna(subset=[cv_cli, cv_data])
    vendas["_cliente_chave"] = (
        vendas[cv_cli_id].astype(str).str.strip()
        if cv_cli_id
        else vendas[cv_cli].map(norm)
    )
    vendas.loc[
        vendas["_cliente_chave"].isin(["", "none", "nan"]),
        "_cliente_chave"
    ] = vendas.loc[
        vendas["_cliente_chave"].isin(["", "none", "nan"]), cv_cli
    ].map(norm)
    vendas["_cliente_nome"] = vendas[cv_cli].astype(str).str.strip()

    vendas_canceladas = pd.DataFrame()
    if cv_status:
        cancelada = vendas[cv_status].astype(str).str.upper().str.contains(
            "CANCEL|DEVOL|ESTORN|REPROV|PERDID", na=False, regex=True
        )
        vendas_canceladas = vendas[cancelada].copy()
        vendas = vendas[~cancelada].copy()

    orc[co_data] = data_coluna(orc[co_data])
    if co_valor:
        orc[co_valor] = numero_coluna(orc[co_valor])

    contas[cc_valor] = numero_coluna(contas[cc_valor])
    if cc_venc:
        contas[cc_venc] = data_coluna(contas[cc_venc])
    financeiro = preparar_financeiro(
        contas, cc_cli, cc_venc, cc_valor, cc_status
    )

    contas["_cliente_chave"] = (
        contas[cc_cli_id].astype(str).str.strip()
        if cc_cli_id
        else contas[cc_cli].map(norm)
    )
    contas.loc[
        contas["_cliente_chave"].isin(["", "none", "nan"]),
        "_cliente_chave"
    ] = contas.loc[
        contas["_cliente_chave"].isin(["", "none", "nan"]), cc_cli
    ].map(norm)

    clientes = vendas.groupby("_cliente_chave").agg({
        "_cliente_nome": "last",
        cv_data: ["max", "count"],
        cv_valor: "sum"
    })
    clientes.columns = ["Cliente", "ultima_compra", "qtd_compras", "faturamento"]
    clientes = clientes.reset_index().rename(columns={"_cliente_chave": "Cliente ID"})

    intervalo = vendas.sort_values(cv_data).groupby("_cliente_chave")[cv_data].apply(
        lambda x: x.diff().mean().days if len(x.dropna()) > 1 else 0
    )

    clientes["intervalo"] = clientes["Cliente ID"].map(intervalo).fillna(0)
    clientes["dias_sem_comprar"] = (hoje - clientes["ultima_compra"]).dt.days
    clientes["ticket_medio"] = clientes["faturamento"] / clientes["qtd_compras"]

    data_limite_3m = hoje - pd.DateOffset(months=3)
    vendas_3m = vendas[vendas[cv_data] >= data_limite_3m].copy()
    potencial_3m = vendas_3m.groupby("_cliente_chave")[cv_valor].sum() / 3
    clientes["potencial_mensal"] = clientes["Cliente ID"].map(potencial_3m).fillna(0)

    orcamentos_todos = orc.copy()
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

    inad = contas_atraso.groupby("_cliente_chave")[cc_valor].sum() if not contas_atraso.empty else pd.Series(dtype=float)

    if not contas_atraso.empty and "dias_atraso" in contas_atraso:
        media_atraso = contas_atraso.groupby("_cliente_chave")["dias_atraso"].mean()
    clientes["inadimplencia"] = clientes["Cliente ID"].map(inad).fillna(0)
    clientes["media_dias_atraso"] = clientes["Cliente ID"].map(media_atraso).fillna(0)
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

    nomes_duplicados = (
        vendas.groupby(vendas["_cliente_nome"].map(norm))["_cliente_chave"]
        .nunique()
    )
    nomes_duplicados = nomes_duplicados[nomes_duplicados > 1]
    qualidade = {
        "vendas_canceladas": len(vendas_canceladas),
        "vendas_sem_cliente_id": int(
            vendas[cv_cli_id].isna().sum() if cv_cli_id else len(vendas)
        ),
        "clientes_nomes_duplicados": int(len(nomes_duplicados)),
        "vendas_sem_custo": int(
            vendas[cv_custo].le(0).sum() if cv_custo else len(vendas)
        ),
        "vendas_sem_vendedor": int(
            vendas[achar_coluna(vendas, ["vendedor"])].astype(str)
            .str.strip().isin(["", "Sem vendedor", "nan"]).sum()
            if achar_coluna(vendas, ["vendedor"]) else len(vendas)
        ),
    }

    return {
        "clientes": clientes,
        "orc_aberto": orc_aberto,
        "orcamentos_todos": orcamentos_todos,
        "co_num": co_num,
        "co_cli": co_cli,
        "co_data": co_data,
        "co_valor": co_valor,
        "financeiro": financeiro,
        "vendas_validas": vendas,
        "qualidade_dados": qualidade,
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
    dados["resultado_financeiro_disponivel"] = False
    return dados

def api_para_dataframes(vendas_api, orcamentos_api, recebimentos_api, vendedor_id=None):
    vendas_api = deduplicar_registros(vendas_api)
    orcamentos_api = deduplicar_registros(orcamentos_api)
    recebimentos_api = deduplicar_registros(recebimentos_api)
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
        "Cliente ID": item.get("cliente_id"),
        "Data": pd.to_datetime(item.get("data"), format="%Y-%m-%d", errors="coerce"),
        "Valor": item.get("valor_total") or 0,
        "Custo": custo_total_venda(item),
        "Situacao": item.get("nome_situacao") or "",
        "Vendedor": item.get("nome_vendedor") or "Sem vendedor",
        "Observacoes": item.get("observacoes") or "",
        "Observacoes internas": item.get("observacoes_interna") or "",
        "Vendedor ID": item.get("vendedor_id"),
        "_venda_id": item.get("id"),
    } for item in vendas_api])

    orcamentos = pd.DataFrame([{
        "Numero": item.get("codigo") or item.get("id"),
        "Cliente": item.get("nome_cliente") or "Cliente sem nome",
        "Cliente ID": item.get("cliente_id"),
        "Data": pd.to_datetime(item.get("data"), format="%Y-%m-%d", errors="coerce"),
        "Situacao": item.get("nome_situacao") or "",
        "Valor": item.get("valor_total") or 0,
        "Vendedor": item.get("nome_vendedor") or "Sem vendedor",
        "_orcamento_id": item.get("id"),
        "_observacoes_interna": item.get("observacoes_interna") or "",
    } for item in orcamentos_api])

    contas = pd.DataFrame([{
        "Cliente": item.get("nome_cliente") or "Cliente sem nome",
        "Cliente ID": item.get("cliente_id"),
        "Vencimento": pd.to_datetime(
            item.get("data_vencimento"), format="%Y-%m-%d", errors="coerce"
        ),
        "Valor Total": item.get("valor_total") or item.get("valor") or 0,
        "Situacao": item.get("_status_financeiro") or "EM ABERTO",
        "Juros": item.get("juros") or 0,
        "Desconto": item.get("desconto") or 0,
        "Forma Pagamento": item.get("nome_forma_pagamento") or "",
        "Loja": item.get("nome_loja") or "",
        "_recebimento_id": item.get("id"),
    } for item in recebimentos_api])

    if vendas.empty:
        vendas = pd.DataFrame(columns=[
            "Cliente", "Cliente ID", "Data", "Valor", "Custo", "Situacao",
            "Vendedor", "Vendedor ID", "_venda_id"
        ])
    if orcamentos.empty:
        orcamentos = pd.DataFrame(columns=[
            "Numero", "Cliente", "Cliente ID", "Data", "Situacao", "Valor", "Vendedor",
            "Observacoes", "Observacoes internas",
            "_orcamento_id", "_observacoes_interna"
        ])
    if contas.empty:
        contas = pd.DataFrame(columns=[
            "Cliente", "Cliente ID", "Vencimento", "Valor Total", "Situacao", "Juros",
            "Desconto", "Forma Pagamento", "Loja", "_recebimento_id"
        ])
    return vendas, orcamentos, contas

def processar_api(
    api, inicio, fim, loja_id, vendedor_id=None, vendedor_nome="Todos",
    configuracao=None
):
    vendas_api = api.sales(inicio, fim, loja_id)
    orcamentos_api = api.budgets(inicio, fim, loja_id)
    recebimentos_api = api.open_receivables(loja_id)
    pagamentos_api = api.open_payables(loja_id)
    inicio_mes = fim.replace(day=1)
    recebidos_mes = api.settled_movements(
        "/recebimentos", inicio_mes, fim, loja_id
    )
    pagos_mes = api.settled_movements(
        "/pagamentos", inicio_mes, fim, loja_id
    )
    vendas, orcamentos, contas = api_para_dataframes(
        vendas_api, orcamentos_api, recebimentos_api, vendedor_id
    )
    if vendas.empty:
        raise RuntimeError("Nenhuma venda foi encontrada para os filtros selecionados.")

    dados = processar_dataframes(vendas, orcamentos, contas)
    contas_pagar = preparar_contas_pagar(pagamentos_api)
    recebido_mes = total_movimentos_liquidados(recebidos_mes)
    pago_mes = total_movimentos_liquidados(pagos_mes)
    dados.update({
        "origem": "api",
        "loja_id": str(loja_id),
        "vendedor_id": str(vendedor_id or ""),
        "vendedor_nome": vendedor_nome,
        "atualizado_em": datetime.now(),
        "contas_pagar": contas_pagar,
        "recebido_mes": recebido_mes,
        "pago_mes": pago_mes,
        "mes_resultado": fim.strftime("%m/%Y"),
        "resultado_financeiro_disponivel": True,
        "configuracao": configuracao or {},
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

def chave_widget(valor):
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", str(valor)).strip("_") or "sem_id"

def identificador_cliente(row, fallback=""):
    cliente_id = str(row.get("Cliente ID", "")).strip()
    if cliente_id and cliente_id.lower() not in {"nan", "none"}:
        return cliente_id
    return f"{norm(row.get('Cliente', 'cliente'))}_{fallback}"

def card_cliente(row, tipo, posicao):
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

    cliente_uid = chave_widget(identificador_cliente(row, posicao))
    if st.button(
        f"✅ Já liguei - {row['Cliente']}",
        key=f"liguei_{tipo}_{cliente_uid}_{posicao}"
    ):
        st.session_state.clientes_ligados.add(row["Cliente"])
        salvar_cliente_ligado(row["Cliente"], tipo)
        st.rerun()

def gerar_texto_email(
    prioridade, orc_aberto, clientes, clientes_churn,
    co_num, co_cli, co_valor, periodo_inicio, periodo_fim,
    financeiro=None, contas_pagar=None, recebido_mes=0, pago_mes=0
):
    hoje_txt = datetime.now().strftime("%d/%m/%Y")
    taxa_churn, qtd_churn, base_churn = calcular_churn(clientes)
    periodo = f"{periodo_inicio:%d/%m/%Y} a {periodo_fim:%d/%m/%Y}"
    orc_urgentes = orc_aberto[orc_aberto["dias_no_sistema"] >= 2].sort_values(
        "dias_no_sistema", ascending=False
    )
    temperaturas = clientes["temperatura"].value_counts()
    metricas_fin = calcular_resultado_financeiro(
        financeiro, contas_pagar, recebido_mes, pago_mes
    )

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
        "FINANCEIRO",
        f"- Carteira a receber: {fmt(metricas_fin['total_aberto'])}",
        f"- Total vencido: {fmt(metricas_fin['total_vencido'])} ({metricas_fin['percentual_vencido']:.1f}%)",
        f"- Entradas previstas em até 7 dias: {fmt(metricas_fin['vence_7'])}",
        f"- Entradas previstas de 8 a 15 dias: {fmt(metricas_fin['vence_15'])}",
        f"- Entradas previstas de 16 a 30 dias: {fmt(metricas_fin['vence_30'])}",
        f"- Concentração nos 5 maiores clientes: {metricas_fin['concentracao_top5']:.1f}%",
        f"- Contas a pagar: {fmt(metricas_fin['total_pagar'])}",
        f"- Saldo total projetado: {fmt(metricas_fin['saldo_carteira'])}",
        f"- Sobra projetada em 30 dias: {fmt(metricas_fin['saldo_30_dias'])}",
        f"- Resultado financeiro do mês: {fmt(metricas_fin['resultado_mes'])}",
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
    co_num, co_cli, co_valor, periodo_inicio, periodo_fim,
    financeiro=None, contas_pagar=None, recebido_mes=0, pago_mes=0
):
    if not REPORTLAB_OK:
        return None

    taxa_churn, qtd_churn, base_churn = calcular_churn(clientes)
    periodo = f"{periodo_inicio:%d/%m/%Y} a {periodo_fim:%d/%m/%Y}"
    orc_urgentes = orc_aberto[orc_aberto["dias_no_sistema"] >= 2].sort_values(
        "dias_no_sistema", ascending=False
    )
    metricas_fin = calcular_resultado_financeiro(
        financeiro, contas_pagar, recebido_mes, pago_mes
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
        [p("Carteira a receber"), p(fmt(metricas_fin["total_aberto"])), p("Total de recebimentos ainda em aberto.")],
        [p("Percentual vencido"), p(f"{metricas_fin['percentual_vencido']:.1f}%"), p("Participação dos títulos vencidos na carteira aberta.")],
        [p("Receber em até 7 dias"), p(fmt(metricas_fin["vence_7"])), p("Entradas previstas no curto prazo.")],
        [p("Contas a pagar"), p(fmt(metricas_fin["total_pagar"])), p("Obrigações ainda em aberto.")],
        [p("Sobra em 30 dias"), p(fmt(metricas_fin["saldo_30_dias"])), p("Entradas previstas menos saídas previstas.")],
        [p("Resultado financeiro mensal"), p(fmt(metricas_fin["resultado_mes"])), p("Recebimentos liquidados menos pagamentos liquidados.")],
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

    elementos.append(Paragraph("7. Carteira financeira", styles["SecaoCEO"]))
    if financeiro is None or financeiro.empty:
        elementos.append(Paragraph("Nenhum recebimento em aberto identificado.", styles["Normal"]))
    else:
        fin_clientes = (
            financeiro.groupby("Cliente")["Valor"].sum()
            .sort_values(ascending=False)
            .head(15)
        )
        fin_pdf = [[p("Cliente"), p("Total a receber"), p("% da carteira")]]
        for cliente, valor in fin_clientes.items():
            participacao = (
                float(valor) / metricas_fin["total_aberto"] * 100
                if metricas_fin["total_aberto"] else 0
            )
            fin_pdf.append([
                p(cliente), p(fmt(valor)), p(f"{participacao:.1f}%")
            ])
        elementos.append(tabela(fin_pdf, [80 * mm, 42 * mm, 38 * mm]))

    elementos.append(Paragraph("8. Contas a pagar por fornecedor", styles["SecaoCEO"]))
    if contas_pagar is None or contas_pagar.empty:
        elementos.append(Paragraph("Nenhuma conta a pagar em aberto identificada.", styles["Normal"]))
    else:
        pagar_fornecedor = (
            contas_pagar.groupby("Fornecedor")["Valor"].sum()
            .sort_values(ascending=False)
            .head(15)
        )
        fornecedores_pdf = [[p("Fornecedor"), p("Total a pagar"), p("% das obrigações")]]
        for fornecedor, valor in pagar_fornecedor.items():
            participacao = (
                float(valor) / metricas_fin["total_pagar"] * 100
                if metricas_fin["total_pagar"] else 0
            )
            fornecedores_pdf.append([
                p(fornecedor), p(fmt(valor)), p(f"{participacao:.1f}%")
            ])
        elementos.append(tabela(fornecedores_pdf, [80 * mm, 42 * mm, 38 * mm]))

    elementos.append(Paragraph("9. Análise e plano de ação", styles["SecaoCEO"]))
    dicas_financeiras = estrategia_financeira(metricas_fin)
    elementos.append(Paragraph(
        f"<b>Hoje:</b> realizar {len(prioridade)} contatos prioritários e retornar "
        f"{len(orc_urgentes)} orçamentos urgentes.<br/>"
        f"<b>Próximos 7 dias:</b> acompanhar clientes em atenção e propostas ainda abertas.<br/>"
        f"<b>Recuperação:</b> abordar primeiro os {min(len(clientes_churn), 10)} clientes "
        "em churn com maior potencial mensal e tratar pendências financeiras antes de uma nova oferta.<br/>"
        f"<b>Financeiro:</b> {' '.join(dicas_financeiras)}",
        styles["BodyText"]
    ))

    elementos.append(Paragraph("10. Metodologia", styles["SecaoCEO"]))
    elementos.append(Paragraph(
        "<b>Churn estimado:</b> clientes com ciclo conhecido e mais de duas vezes o intervalo "
        "médio sem comprar, dividido pela quantidade de clientes com ciclo conhecido.<br/>"
        "<b>Potencial mensal:</b> compras dos últimos três meses divididas por três.<br/>"
        "<b>Capacidade das prioridades:</b> soma dos tickets médios dos clientes quentes; "
        "não representa promessa de venda.<br/>"
        "<b>Percentual vencido:</b> valor vencido dividido pela carteira total ainda em aberto.<br/>"
        "<b>Concentração:</b> participação dos cinco maiores clientes no total a receber.<br/>"
        "<b>Resultado financeiro mensal:</b> recebimentos liquidados menos pagamentos liquidados; "
        "não equivale necessariamente ao lucro contábil.<br/>"
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

def renderizar_financeiro_ceo(
    financeiro, contas_pagar, recebido_mes, pago_mes,
    mes_resultado, resultado_disponivel, clientes, clientes_churn
):
    st.subheader("Financeiro CEO")
    st.caption(
        "Visão estratégica da carteira de recebimentos em aberto. "
        "Os valores representam entradas previstas, não saldo bancário disponível."
    )
    metricas = calcular_resultado_financeiro(
        financeiro, contas_pagar, recebido_mes, pago_mes
    )

    linha1 = st.columns(4)
    linha1[0].metric("Carteira a receber", fmt(metricas["total_aberto"]))
    linha1[1].metric("Contas a pagar", fmt(metricas["total_pagar"]))
    linha1[2].metric(
        "Saldo total projetado",
        fmt(metricas["saldo_carteira"]),
        "Receber menos pagar"
    )
    if resultado_disponivel:
        linha1[3].metric(
            f"Resultado financeiro {mes_resultado}",
            fmt(metricas["resultado_mes"]),
            "Lucro" if metricas["resultado_mes"] >= 0 else "Prejuízo",
            delta_color="normal"
        )
    else:
        linha1[3].metric("Resultado financeiro mensal", "Indisponível")

    linha2 = st.columns(4)
    linha2[0].metric(
        "Total vencido",
        fmt(metricas["total_vencido"]),
        f"{metricas['percentual_vencido']:.1f}% da carteira"
    )
    linha2[1].metric("Contas a pagar vencidas", fmt(metricas["pagar_vencido"]))
    linha2[2].metric("Sobra projetada em 30 dias", fmt(metricas["saldo_30_dias"]))
    linha2[3].metric(
        "Margem financeira do mês",
        f"{metricas['margem_caixa']:.1f}%"
    )

    with st.expander("Como o resultado e a sobra são calculados?"):
        st.markdown(
            f"""
            **Resultado financeiro de {mes_resultado}**

            `Recebimentos liquidados - pagamentos liquidados`

            {fmt(metricas['recebido_mes'])} - {fmt(metricas['pago_mes'])}
            = **{fmt(metricas['resultado_mes']) if resultado_disponivel else 'Indisponível no modo Excel'}**

            **Sobra projetada em 30 dias**

            `Contas a receber nos próximos 30 dias - contas a pagar nos próximos 30 dias`

            Este resultado é uma visão de caixa. Não inclui automaticamente estoque,
            depreciação, impostos provisionados ou despesas que ainda não foram lançadas.
            """
        )

    linha3 = st.columns(4)
    linha3[0].metric("Receber em até 7 dias", fmt(metricas["vence_7"]))
    linha3[1].metric("Pagar em até 7 dias", fmt(metricas["pagar_7"]))
    linha3[2].metric("Prazo médio a receber", f"{metricas['prazo_medio']:.0f} dias")
    linha3[3].metric("Concentração nos 5 maiores", f"{metricas['concentracao_top5']:.1f}%")

    if financeiro is None or financeiro.empty:
        st.info("Nenhuma conta em aberto foi encontrada para montar a visão financeira.")
        if contas_pagar is None or contas_pagar.empty:
            return

    potencial_churn = float(clientes_churn["potencial_mensal"].sum())
    receita_em_risco = metricas["total_vencido"] + potencial_churn
    st.metric(
        "Exposição estratégica estimada",
        fmt(receita_em_risco),
        help=(
            "Soma do valor vencido com o potencial mensal dos clientes em churn. "
            "É um indicador de exposição, não uma perda contábil confirmada."
        )
    )

    st.markdown("#### Alertas estratégicos")
    alertas = []
    if metricas["percentual_vencido"] >= 25:
        alertas.append(
            f"CRÍTICO: {metricas['percentual_vencido']:.1f}% da carteira está vencida."
        )
    elif metricas["percentual_vencido"] >= 10:
        alertas.append(
            f"ATENÇÃO: {metricas['percentual_vencido']:.1f}% da carteira está vencida."
        )
    if metricas["concentracao_top5"] >= 50:
        alertas.append(
            "A carteira está concentrada: os cinco maiores clientes representam "
            f"{metricas['concentracao_top5']:.1f}% do total a receber."
        )
    vencido_60 = float(financeiro.loc[
        financeiro["Dias_atraso"] > 60, "Valor"
    ].sum())
    if vencido_60 > 0:
        alertas.append(
            f"Existem {fmt(vencido_60)} vencidos há mais de 60 dias."
        )
    if metricas["vence_7"] > 0:
        alertas.append(
            f"Há {fmt(metricas['vence_7'])} previstos para entrar nos próximos 7 dias."
        )
    if metricas["saldo_30_dias"] < 0:
        alertas.append(
            f"Déficit projetado de {fmt(abs(metricas['saldo_30_dias']))} "
            "para os próximos 30 dias."
        )
    if not alertas:
        st.success("Nenhum alerta financeiro relevante pelos critérios atuais.")
    else:
        for alerta in alertas:
            st.warning(alerta)

    col_aging, col_fluxo = st.columns(2)
    ordem_faixas = [
        "Vencido acima de 60 dias",
        "Vencido de 31 a 60 dias",
        "Vencido de 16 a 30 dias",
        "Vencido de 8 a 15 dias",
        "Vencido até 7 dias",
        "A vencer em até 7 dias",
        "A vencer de 8 a 15 dias",
        "A vencer de 16 a 30 dias",
        "A vencer de 31 a 60 dias",
        "A vencer acima de 60 dias",
    ]
    with col_aging:
        st.markdown("#### Carteira por faixa de vencimento")
        aging = (
            financeiro.groupby("Faixa")["Valor"].sum()
            .reindex(ordem_faixas, fill_value=0)
        )
        st.bar_chart(aging)

    with col_fluxo:
        st.markdown("#### Entradas previstas por mês")
        futuro = financeiro[~financeiro["Vencida"]].copy()
        if futuro.empty:
            st.info("Não há recebimentos futuros na carteira consultada.")
        else:
            futuro["Mês"] = futuro["Vencimento"].dt.strftime("%m/%Y")
            fluxo = futuro.groupby("Mês", sort=False)["Valor"].sum()
            st.bar_chart(fluxo)

    st.markdown("#### Maiores clientes na carteira")
    ranking = (
        financeiro.groupby("Cliente")
        .agg(
            Total=("Valor", "sum"),
            Vencido=("Valor", lambda s: s[financeiro.loc[s.index, "Vencida"]].sum()),
            Titulos=("Valor", "count"),
            Maior_atraso=("Dias_atraso", "max"),
        )
        .sort_values("Total", ascending=False)
        .head(20)
        .reset_index()
    )
    ranking["Total"] = ranking["Total"].map(fmt)
    ranking["Vencido"] = ranking["Vencido"].map(fmt)
    ranking = ranking.rename(columns={
        "Titulos": "Títulos",
        "Maior_atraso": "Maior atraso (dias)"
    })
    st.dataframe(ranking, use_container_width=True, hide_index=True)

    st.markdown("#### Contas a pagar por fornecedor")
    if contas_pagar is None or contas_pagar.empty:
        st.info(
            "Contas a pagar não estão disponíveis. No modo API, atualize os dados; "
            "no modo Excel, seria necessário um quarto arquivo de contas a pagar."
        )
    else:
        fornecedores = (
            contas_pagar.groupby("Fornecedor")
            .agg(
                Total=("Valor", "sum"),
                Vencido=("Valor", lambda s: s[contas_pagar.loc[s.index, "Vencida"]].sum()),
                Titulos=("Valor", "count"),
                Proximo_vencimento=("Vencimento", "min"),
            )
            .sort_values("Total", ascending=False)
            .reset_index()
        )
        fornecedores["Total"] = fornecedores["Total"].map(fmt)
        fornecedores["Vencido"] = fornecedores["Vencido"].map(fmt)
        fornecedores["Proximo_vencimento"] = fornecedores[
            "Proximo_vencimento"
        ].dt.strftime("%d/%m/%Y")
        fornecedores = fornecedores.rename(columns={
            "Titulos": "Títulos",
            "Proximo_vencimento": "Próximo vencimento"
        })
        st.dataframe(
            fornecedores.head(25), use_container_width=True, hide_index=True
        )

        st.markdown("#### Agenda de pagamentos")
        agenda = contas_pagar[[
            "Fornecedor", "Descricao", "Vencimento", "Valor",
            "Situacao", "Dias_para_vencer"
        ]].head(30).copy()
        agenda["Vencimento"] = agenda["Vencimento"].dt.strftime("%d/%m/%Y")
        agenda["Valor"] = agenda["Valor"].map(fmt)
        agenda = agenda.rename(columns={
            "Descricao": "Descrição",
            "Situacao": "Situação",
            "Dias_para_vencer": "Dias para vencer"
        })
        st.dataframe(agenda, use_container_width=True, hide_index=True)

    st.markdown("#### Análise e estratégia financeira")
    if not resultado_disponivel:
        st.info(
            "O lucro ou prejuízo mensal exige os movimentos liquidados de recebimentos "
            "e pagamentos. Esse cálculo fica disponível automaticamente pelo modo API."
        )
    elif metricas["resultado_mes"] > 0:
        st.success(
            f"Há lucro financeiro de {fmt(metricas['resultado_mes'])} em "
            f"{mes_resultado}."
        )
    elif metricas["resultado_mes"] < 0:
        st.error(
            f"Há prejuízo financeiro de {fmt(abs(metricas['resultado_mes']))} em "
            f"{mes_resultado}."
        )
    else:
        st.warning(f"O resultado financeiro de {mes_resultado} está equilibrado.")
    if resultado_disponivel:
        for dica in estrategia_financeira(metricas):
            st.write(f"- {dica}")

def renderizar_financeiro_real(dados):
    configuracao = dados.get("configuracao", {})
    real = calcular_financeiro_real(dados, configuracao)
    st.markdown("---")
    st.subheader("Resultado econômico e cenários")
    if not real:
        st.info(
            "Os dados desta sessão foram carregados por uma versão anterior. "
            "Clique em 'Atualizar dados do GestãoClick' para calcular custos, "
            "margens e resultado econômico."
        )
        return
    cols = st.columns(4)
    cols[0].metric("Receita do mês", fmt(real["receita_mes"]))
    cols[1].metric("Custo das vendas", fmt(real["custo_mes"]))
    cols[2].metric("Lucro bruto", fmt(real["lucro_bruto"]))
    cols[3].metric("Margem bruta", f"{real['margem_bruta']:.1f}%")
    cols2 = st.columns(4)
    cols2[0].metric("Impostos estimados", fmt(real["impostos_estimados"]))
    cols2[1].metric("Folha + despesas", fmt(
        real["folha"] + real["despesas_fixas"] + real["outras_despesas"]
    ))
    cols2[2].metric(
        "Lucro operacional estimado",
        fmt(real["lucro_operacional"]),
        "Lucro" if real["lucro_operacional"] >= 0 else "Prejuízo"
    )
    cols2[3].metric("Margem operacional", f"{real['margem_operacional']:.1f}%")
    if not real["custos_disponiveis"]:
        st.warning(
            "Os custos das vendas não estão preenchidos na API. O lucro bruto e "
            "operacional podem estar superestimados."
        )
    st.markdown("#### Cenários de caixa")
    cenarios = pd.DataFrame([
        {"Cenário": nome, "Caixa projetado": valor}
        for nome, valor in real["cenarios"].items()
    ])
    cenarios["Caixa projetado"] = cenarios["Caixa projetado"].map(fmt)
    st.dataframe(cenarios, use_container_width=True, hide_index=True)
    st.caption(
        "Os cenários consideram 70%, 90% ou 100% da carteira a receber, "
        "menos todas as contas a pagar registradas."
    )

def renderizar_gestao_comercial(dados):
    indicadores, vendedores = calcular_gestao_comercial(
        dados, dados.get("configuracao", {})
    )
    st.subheader("Gestão Comercial")
    if not indicadores:
        st.info(
            "Os dados desta sessão foram carregados por uma versão anterior. "
            "Clique em 'Atualizar dados do GestãoClick' para calcular metas, "
            "margens e desempenho por vendedor."
        )
        return
    cols = st.columns(4)
    cols[0].metric("Meta geral", fmt(indicadores["meta_geral"]))
    cols[1].metric("Realizado no mês", fmt(indicadores["realizado"]))
    cols[2].metric("Projeção de fechamento", fmt(indicadores["projecao"]))
    cols[3].metric("Distância da meta", fmt(indicadores["distancia_meta"]))
    cols2 = st.columns(3)
    cols2[0].metric(
        "Conversão de orçamentos",
        f"{indicadores['conversao_orcamentos']:.1f}%"
    )
    cols2[1].metric("Orçamentos analisados", indicadores["orcamentos_total"])
    cols2[2].metric(
        "Idade média dos abertos",
        f"{indicadores['idade_media_abertos']:.0f} dias"
    )
    st.caption(
        "A conversão usa as situações dos orçamentos. Sem vínculo direto entre "
        "orçamento e venda, o tempo exato até fechamento não pode ser afirmado."
    )
    if not vendedores.empty:
        exibir = vendedores.copy()
        for col in ("Faturamento", "Custo", "Margem", "Meta", "Distancia_meta"):
            exibir[col] = exibir[col].map(fmt)
        exibir["Ticket_medio"] = exibir["Ticket_medio"].map(fmt)
        exibir["Margem_pct"] = exibir["Margem_pct"].map(lambda v: f"{v:.1f}%")
        exibir["Atingimento_pct"] = exibir["Atingimento_pct"].map(
            lambda v: f"{v:.1f}%"
        )
        exibir = exibir.rename(columns={
            "Ticket_medio": "Ticket médio",
            "Margem_pct": "Margem %",
            "Atingimento_pct": "Atingimento %",
            "Distancia_meta": "Distância da meta",
        })
        st.dataframe(exibir, use_container_width=True, hide_index=True)
    st.markdown("#### Motivos de perda")
    if indicadores["motivos_perda"].empty:
        st.info(
            "Nenhum motivo de perda foi encontrado nas observações dos orçamentos."
        )
    else:
        st.dataframe(
            indicadores["motivos_perda"],
            use_container_width=True,
            hide_index=True
        )

def renderizar_qualidade_dados(dados):
    qualidade = dados.get("qualidade_dados", {})
    clientes = dados.get("clientes", pd.DataFrame()).copy()
    st.subheader("Qualidade dos Dados")
    cols = st.columns(5)
    cols[0].metric("Vendas excluídas", qualidade.get("vendas_canceladas", 0))
    cols[1].metric("Sem cliente ID", qualidade.get("vendas_sem_cliente_id", 0))
    cols[2].metric("Nomes duplicados", qualidade.get("clientes_nomes_duplicados", 0))
    cols[3].metric("Sem custo", qualidade.get("vendas_sem_custo", 0))
    cols[4].metric("Sem vendedor", qualidade.get("vendas_sem_vendedor", 0))
    problemas = sum(int(v) for v in qualidade.values())
    if problemas:
        st.warning(
            "Há registros que podem reduzir a precisão dos indicadores. "
            "Vendas canceladas e devolvidas foram excluídas automaticamente."
        )
    else:
        st.success("Nenhum problema relevante foi detectado na base consultada.")
    st.markdown(
        """
        **Regras aplicadas**

        - clientes são consolidados por `cliente_id`; o nome é apenas para exibição;
        - vendas canceladas, devolvidas, estornadas, reprovadas ou perdidas são excluídas;
        - registros duplicados da API são removidos pelo ID;
        - custos, vendedor e identificação ausentes são sinalizados;
        - contas futuras não entram na inadimplência antes do vencimento.
        """
    )

    st.markdown("#### Clientes ativos com pendências")
    if clientes.empty or "inadimplencia" not in clientes.columns:
        st.info(
            "Atualize os dados do GestãoClick para analisar clientes com pendências."
        )
        return
    ativos_inadimplentes = clientes[
        clientes["inadimplencia"] > 0
    ].sort_values("inadimplencia", ascending=False).copy()
    if ativos_inadimplentes.empty:
        st.success("Nenhum cliente da carteira comercial possui pendência identificada.")
    else:
        tabela_ativos = ativos_inadimplentes[[
            "Cliente", "ultima_compra", "inadimplencia",
            "media_dias_atraso", "temperatura"
        ]].head(20).copy()
        tabela_ativos["ultima_compra"] = tabela_ativos["ultima_compra"].dt.strftime("%d/%m/%Y")
        tabela_ativos["inadimplencia"] = tabela_ativos["inadimplencia"].map(fmt)
        tabela_ativos = tabela_ativos.rename(columns={
            "ultima_compra": "Última compra",
            "inadimplencia": "Valor vencido",
            "media_dias_atraso": "Média de atraso",
            "temperatura": "Situação comercial",
        })
        st.dataframe(tabela_ativos, use_container_width=True, hide_index=True)

def renderizar():
    dados = st.session_state.dados_processados
    clientes = dados["clientes"]
    orc_aberto = dados["orc_aberto"]
    co_num = dados["co_num"]
    co_cli = dados["co_cli"]
    co_valor = dados["co_valor"]
    periodo_inicio = dados.get("periodo_inicio", clientes["ultima_compra"].min())
    periodo_fim = dados.get("periodo_fim", clientes["ultima_compra"].max())
    financeiro = dados.get("financeiro", pd.DataFrame())
    contas_pagar = dados.get("contas_pagar", pd.DataFrame())
    recebido_mes = float(dados.get("recebido_mes", 0))
    pago_mes = float(dados.get("pago_mes", 0))
    mes_resultado = dados.get("mes_resultado", datetime.now().strftime("%m/%Y"))
    resultado_disponivel = bool(
        dados.get("resultado_financeiro_disponivel", False)
    )

    prioridade = montar_prioridade(clientes)
    resumo = montar_resumo(clientes)
    taxa_churn, qtd_churn, base_churn = calcular_churn(clientes)
    clientes_churn = listar_clientes_churn(clientes)
    churn_avancado = calcular_churn_avancado(dados)

    if dados.get("origem") == "api":
        atualizado = dados.get("atualizado_em")
        texto_atualizacao = atualizado.strftime("%d/%m/%Y %H:%M") if atualizado else "agora"
        st.success(
            f"Dados carregados pela API do GestãoClick | "
            f"Vendedor: {dados.get('vendedor_nome', 'Todos')} | "
            f"Período: {periodo_inicio:%d/%m/%Y} a {periodo_fim:%d/%m/%Y} | "
            f"Atualizado em {texto_atualizacao}"
        )
    else:
        st.info("Dados carregados por arquivos Excel.")

    aba_ceo, aba_financeiro, aba_comercial, aba_churn, aba_prioridade, aba_resumo, aba_orc, aba_gestao, aba_qualidade, aba_base, aba_email, aba_relatorio = st.tabs([
        "👑 CEO", "💰 Financeiro CEO", "🎯 Gestão Comercial", "📉 Churn",
        "🔥 Prioridade", "📋 Resumo", "📄 Orçamentos", "🧠 Gestão",
        "✅ Qualidade", "📊 Base", "✉️ Resumo E-mail", "📧 Relatório Comercial"
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

    with aba_financeiro:
        renderizar_financeiro_ceo(
            financeiro, contas_pagar, recebido_mes, pago_mes,
            mes_resultado, resultado_disponivel, clientes, clientes_churn
        )
        renderizar_financeiro_real(dados)

    with aba_comercial:
        renderizar_gestao_comercial(dados)

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
        avancado = st.columns(4)
        avancado[0].metric(
            "Churn ponderado por valor",
            f"{churn_avancado['churn_ponderado']:.1f}%"
        )
        avancado[1].metric(
            "Migrando para churn",
            len(churn_avancado["migrando"])
        )
        avancado[2].metric(
            "Recuperações históricas",
            churn_avancado["recuperados_historicos"],
            f"{churn_avancado['taxa_recuperacao_historica']:.1f}% da base recorrente"
        )
        avancado[3].metric(
            "Clientes sazonais",
            churn_avancado["sazonais"]
        )
        st.caption(
            "O churn ponderado considera o faturamento dos clientes perdidos. "
            "Clientes sazonais são sinalizados separadamente por apresentarem ciclos irregulares."
        )
        if not churn_avancado["tendencia_mensal"].empty:
            st.markdown("#### Evolução mensal do churn")
            tendencia = churn_avancado["tendencia_mensal"].set_index("Mês")
            st.line_chart(tendencia)
        if not churn_avancado["migrando"].empty:
            st.markdown("#### Clientes migrando para churn")
            migrando = churn_avancado["migrando"][[
                "Cliente", "dias_sem_comprar", "intervalo",
                "potencial_mensal", "temperatura"
            ]].copy()
            migrando["potencial_mensal"] = migrando["potencial_mensal"].map(fmt)
            migrando = migrando.rename(columns={
                "dias_sem_comprar": "Dias sem comprar",
                "intervalo": "Ciclo médio",
                "potencial_mensal": "Potencial mensal",
                "temperatura": "Situação",
            })
            st.dataframe(migrando, use_container_width=True, hide_index=True)

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
            for j, (indice, row) in enumerate(cards[i:i+3]):
                with cols[j]:
                    card_cliente(row, "prioridade", f"{indice}_{i}_{j}")

    with aba_resumo:
        st.subheader("📋 Resumo Comercial")
        st.markdown(f"**Clientes para ação:** **{len(resumo)}**")
        st.markdown(f"**Capacidade de venda do resumo:** **{fmt(resumo['ticket_medio'].sum())}**")
        st.markdown(f"**Potencial recuperável:** **{fmt(resumo['potencial_recuperavel'].sum())}**")
        cards = list(resumo.iterrows())
        for i in range(0, len(cards), 3):
            cols = st.columns(3)
            for j, (indice, row) in enumerate(cards[i:i+3]):
                with cols[j]:
                    card_cliente(row, "resumo", f"{indice}_{i}_{j}")

    with aba_orc:
        st.subheader("📄 Orçamentos em aberto para retorno")
        if orc_aberto.empty:
            st.info("Nenhum orçamento em aberto nos últimos 30 dias.")
        else:
            cards = list(orc_aberto.iterrows())
            for i in range(0, len(cards), 3):
                cols = st.columns(3)
                for j, (indice, r) in enumerate(cards[i:i+3]):
                    with cols[j]:
                        valor_txt = fmt_html(r[co_valor]) if co_valor else "Sem valor"
                        num_orc = str(r[co_num])
                        orcamento_id = str(r.get("_orcamento_id", "")).strip()
                        orcamento_uid = chave_widget(
                            orcamento_id or f"{num_orc}_{indice}_{i}_{j}"
                        )
                        chave_obs = f"obs_orc_{orcamento_uid}"
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

                        if st.button(
                            f"💾 Salvar observação {num_orc}",
                            key=f"salvar_obs_{orcamento_uid}"
                        ):
                            try:
                                if not obs.strip():
                                    raise RuntimeError("Digite uma observação antes de salvar.")
                                if dados.get("origem") == "api":
                                    orcamento_id = str(r.get("_orcamento_id") or "").strip()
                                    if not orcamento_id:
                                        raise RuntimeError("ID interno do orçamento não encontrado.")
                                    st.session_state.alteracao_gestaoclick_pendente = {
                                        "tipo": "observacao_orcamento",
                                        "numero": num_orc,
                                        "orcamento_id": orcamento_id,
                                        "loja_id": dados["loja_id"],
                                        "cliente": str(r[co_cli]),
                                        "observacao": obs,
                                    }
                                    st.rerun()
                                else:
                                    st.session_state.observacoes_orc[num_orc] = obs
                                    salvar_observacao_orcamento(num_orc, r[co_cli], obs)
                                    st.success("Observação salva no Google Sheets.")
                            except Exception as e:
                                st.error(f"Não foi possível salvar a observação: {e}")

                        pendente = st.session_state.alteracao_gestaoclick_pendente
                        if (
                            dados.get("origem") == "api" and pendente and
                            pendente.get("numero") == num_orc and
                            pendente.get("orcamento_id") == orcamento_id
                        ):
                            st.warning(
                                "Confirme a alteração no GestãoClick.\n\n"
                                f"Orçamento: {num_orc}\n\n"
                                f"Cliente: {pendente['cliente']}\n\n"
                                f"Nova observação: {pendente['observacao']}"
                            )
                            confirmado = st.checkbox(
                                "Revisei os dados e autorizo a gravação no GestãoClick.",
                                key=f"confirmar_gc_{orcamento_uid}"
                            )
                            col_confirmar, col_cancelar = st.columns(2)
                            if col_confirmar.button(
                                "Confirmar gravação",
                                key=f"executar_gc_{orcamento_uid}",
                                disabled=not confirmado,
                                type="primary"
                            ):
                                try:
                                    api_gestaoclick().append_budget_note(
                                        pendente["orcamento_id"],
                                        pendente["loja_id"],
                                        pendente["observacao"],
                                        st.session_state.get(
                                            "gc_usuario_nome", USUARIO_PADRAO
                                        )
                                    )
                                    st.session_state.observacoes_orc[num_orc] = ""
                                    st.session_state.alteracao_gestaoclick_pendente = None
                                    st.success(
                                        "Alteração confirmada e gravada no GestãoClick."
                                    )
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Falha ao gravar no GestãoClick: {e}")
                            if col_cancelar.button(
                                "Cancelar",
                                key=f"cancelar_gc_{orcamento_uid}"
                            ):
                                st.session_state.alteracao_gestaoclick_pendente = None
                                st.rerun()

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

    with aba_qualidade:
        renderizar_qualidade_dados(dados)

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
            co_num, co_cli, co_valor, periodo_inicio, periodo_fim,
            financeiro, contas_pagar, recebido_mes, pago_mes
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
            co_num, co_cli, co_valor, periodo_inicio, periodo_fim,
            financeiro, contas_pagar, recebido_mes, pago_mes
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

        with st.sidebar.expander("Metas e premissas financeiras"):
            meta_geral = st.number_input(
                "Meta geral mensal",
                min_value=0.0,
                value=float(st.session_state.get("meta_geral", 0.0)),
                step=1000.0
            )
            st.session_state.meta_geral = meta_geral
            vendedor_nome_config = vendedor.get("nome") or "Todos"
            if vendedor_nome_config != "Todos":
                meta_vendedor = st.number_input(
                    f"Meta de {vendedor_nome_config}",
                    min_value=0.0,
                    value=float(
                        st.session_state.metas_vendedor.get(
                            vendedor_nome_config, 0.0
                        )
                    ),
                    step=500.0
                )
                st.session_state.metas_vendedor[vendedor_nome_config] = meta_vendedor
            saldo_inicial = st.number_input(
                "Saldo bancário inicial",
                value=float(st.session_state.get("saldo_inicial", 0.0)),
                step=1000.0
            )
            impostos_pct = st.number_input(
                "Impostos estimados sobre vendas (%)",
                min_value=0.0,
                max_value=100.0,
                value=float(st.session_state.get("impostos_pct", 0.0)),
                step=0.5
            )
            folha_mensal = st.number_input(
                "Folha mensal",
                min_value=0.0,
                value=float(st.session_state.get("folha_mensal", 0.0)),
                step=1000.0
            )
            despesas_fixas = st.number_input(
                "Despesas fixas mensais não lançadas",
                min_value=0.0,
                value=float(st.session_state.get("despesas_fixas", 0.0)),
                step=1000.0
            )
            outras_despesas = st.number_input(
                "Outras despesas mensais não lançadas",
                min_value=0.0,
                value=float(st.session_state.get("outras_despesas", 0.0)),
                step=500.0
            )
            st.session_state.saldo_inicial = saldo_inicial
            st.session_state.impostos_pct = impostos_pct
            st.session_state.folha_mensal = folha_mensal
            st.session_state.despesas_fixas = despesas_fixas
            st.session_state.outras_despesas = outras_despesas

        if st.sidebar.button("Atualizar dados do GestãoClick", type="primary"):
            try:
                with st.spinner(
                    "Buscando vendas, orçamentos, contas a receber, contas a pagar e movimentos do mês..."
                ):
                    st.session_state.clientes_ligados = carregar_clientes_ligados_hoje()
                    st.session_state.observacoes_orc = {}
                    st.session_state.dados_processados = processar_api(
                        api_gestaoclick(),
                        inicio_api,
                        fim_api,
                        loja_id,
                        vendedor.get("id") or None,
                        vendedor.get("nome") or "Todos",
                        {
                            "meta_geral": meta_geral,
                            "metas_vendedor": dict(st.session_state.metas_vendedor),
                            "saldo_inicial": saldo_inicial,
                            "impostos_pct": impostos_pct,
                            "folha_mensal": folha_mensal,
                            "despesas_fixas": despesas_fixas,
                            "outras_despesas": outras_despesas,
                        }
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
