import streamlit as st
import pandas as pd
from datetime import datetime
from io import BytesIO
import gspread
from google.oauth2.service_account import Credentials

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

def fmt_html(v):
    return fmt(v).replace("$", "&#36;")

def conectar_google_sheets():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=scope
    )

    gc = gspread.authorize(creds)
    planilha = gc.open("CRM_HISTORICO_LUKATONER")

    return planilha

# Mantenha o restante do seu código exatamente igual daqui para baixo.
