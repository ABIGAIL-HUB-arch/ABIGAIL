import os
import json
import re
import base64
import pytz
import tempfile
import requests as http_requests
from openai import OpenAI
from datetime import date, datetime, timedelta, time as datetime_time
from email.mime.text import MIMEText
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import firebase_admin
from firebase_admin import credentials, firestore
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TOKEN    = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_CHAT_ID", "0"))
TZ       = pytz.timezone("America/Sao_Paulo")
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

# ─── Z-API — GLASS CARE ───────────────────────────────────────────────────────
ZAPI_INSTANCE    = "3F1C150EF6D64265EAE5B20DE66F3711"
ZAPI_TOKEN       = "D9A13E7A8251463E924D1F5D"
ZAPI_CLIENT_TOKEN= "F606b47239c674ca68ca604acb5f98c3eS"
ZAPI_URL         = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/send-text"
MEU_NUMERO       = "5531983039481"

def enviar_whatsapp(telefone: str, mensagem: str) -> bool:
    fone = "55" + re.sub(r"\D", "", telefone)
    try:
        r = http_requests.post(
            ZAPI_URL,
            headers={"Content-Type": "application/json", "client-token": ZAPI_CLIENT_TOKEN},
            json={"phone": fone, "message": mensagem},
            timeout=15
        )
        data = r.json()
        if data.get("error"):
            print(f"Z-API erro: {data['error']}")
            return False
        return True
    except Exception as e:
        print(f"Erro Z-API: {e}")
        return False

def notificar_samuel(mensagem: str) -> bool:
    return enviar_whatsapp(MEU_NUMERO, mensagem)

# ─── FIREBASE ─────────────────────────────────────────────────────────────────
cred_json = os.environ.get("FIREBASE_CREDENTIALS", "")
if cred_json:
    cred_dict = json.loads(cred_json)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
db = firestore.client()

# ─── GOOGLE OAUTH ─────────────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
]

def get_google_creds():
    try:
        token_json = os.environ.get("GOOGLE_TOKEN", "")
        if token_json:
            creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            return creds
    except Exception as e:
        print(f"Erro get creds: {e}")
    return None

def get_gmail():
    c = get_google_creds()
    return build("gmail", "v1", credentials=c) if c else None

def get_calendar():
    c = get_google_creds()
    return build("calendar", "v3", credentials=c) if c else None

# ─── FIREBASE HELPERS ─────────────────────────────────────────────────────────
def carregar(uid):
    doc = db.collection("usuarios").document(str(uid)).get()
    return doc.to_dict() if doc.exists else {"obras": {}, "obra_atual": None, "funcionarios": {}, "tarefas": []}

def salvar(uid, dados):
    db.collection("usuarios").document(str(uid)).set(dados)

# ─── FORMATAÇÃO ───────────────────────────────────────────────────────────────
def fmt(v):
    return f"R$ {float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def hoje():
    return datetime.now(TZ).strftime("%d/%m/%Y")

DIAS_UTEIS_MES = 22

FUNCOES_PADRAO = {
    "serralheiro": 2200, "ajudante": 1600,
    "instalador": 2000, "cortador": 2000,
}

ALIASES_FUNCAO = {
    "serralheiro": ["serralheiro", "serralheiros"],
    "ajudante": ["ajudante", "ajudantes", "auxiliar"],
    "instalador": ["instalador", "instaladores", "montador"],
    "cortador": ["cortador", "cortadores"],
}

def get_funcionarios(dados):
    return dados.get("funcionarios") or FUNCOES_PADRAO.copy()

def custo_dia(sal):
    return sal / DIAS_UTEIS_MES

# ─── GMAIL HELPERS ────────────────────────────────────────────────────────────
def buscar_emails(service, max_r=5):
    try:
        res = service.users().messages().list(userId="me", labelIds=["INBOX","UNREAD"], maxResults=max_r).execute()
        msgs = res.get("messages", [])
        emails = []
        for m in msgs:
            d = service.users().messages().get(userId="me", id=m["id"], format="metadata",
                metadataHeaders=["From","Subject","Date","Reply-To"]).execute()
            h = {x["name"]: x["value"] for x in d["payload"]["headers"]}
            emails.append({
                "id": m["id"],
                "de": h.get("From",""),
                "reply_to": h.get("Reply-To", h.get("From","")),
                "assunto": h.get("Subject",""),
                "preview": d.get("snippet","")[:150]
            })
        return emails
    except Exception as e:
        print(f"Erro buscar emails: {e}")
        return []

def enviar_email(service, para, assunto, corpo):
    try:
        msg = MIMEText(corpo)
        msg["to"] = para
        msg["subject"] = assunto
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True
    except Exception as e:
        print(f"Erro enviar email: {e}")
        return False

# ─── CALENDAR HELPERS ─────────────────────────────────────────────────────────
def buscar_eventos(service, dias=1):
    try:
        agora = datetime.now(TZ).isoformat()
        fim = (datetime.now(TZ) + timedelta(days=dias)).isoformat()
        res = service.events().list(calendarId="primary", timeMin=agora, timeMax=fim,
            singleEvents=True, orderBy="startTime").execute()
        return res.get("items", [])
    except Exception as e:
        print(f"Erro buscar eventos: {e}")
        return []

def buscar_proximos_eventos(service, minutos=40):
    try:
        agora = datetime.now(TZ)
        fim = agora + timedelta(minutes=minutos)
        res = service.events().list(calendarId="primary",
            timeMin=agora.isoformat(), timeMax=fim.isoformat(),
            singleEvents=True, orderBy="startTime").execute()
        return res.get("items", [])
    except:
        return []

def criar_evento_cal(service, titulo, inicio_dt, fim_dt, descricao=""):
    try:
        ev = {
            "summary": titulo, "description": descricao,
            "start": {"dateTime": inicio_dt, "timeZone": "America/Sao_Paulo"},
            "end": {"dateTime": fim_dt, "timeZone": "America/Sao_Paulo"},
        }
        return service.events().insert(calendarId="primary", body=ev).execute().get("htmlLink","")
    except Exception as e:
        print(f"Erro criar evento: {e}")
        return None

# ─── PARSER DE DATAS ──────────────────────────────────────────────────────────
DIAS_SEMANA = {
    "segunda": 0, "terca": 1, "terça": 1, "quarta": 2,
    "quinta": 3, "sexta": 4, "sabado": 5, "sábado": 5, "domingo": 6
}

def parse_data_hora(texto):
    t = texto.lower()
    agora = datetime.now(TZ)
    hora, minuto = 9, 0
    hora_m = re.search(r'(\d{1,2})h(\d{2})', t)
    if hora_m:
        hora = int(hora_m.group(1)); minuto = int(hora_m.group(2))
    else:
        hora_m = re.search(r'(\d{1,2})h', t)
        if hora_m:
            hora = int(hora_m.group(1)); minuto = 0
        else:
            hora_m = re.search(r'(\d{1,2}):(\d{2})', t)
            if hora_m:
                hora = int(hora_m.group(1)); minuto = int(hora_m.group(2))
    if "hoje" in t:
        data = agora.date()
    elif "amanhã" in t or "amanha" in t:
        data = (agora + timedelta(days=1)).date()
    else:
        data = None
        for dia_nome, dia_num in DIAS_SEMANA.items():
            if dia_nome in t:
                dias_frente = (dia_num - agora.weekday()) % 7
                if dias_frente == 0: dias_frente = 7
                data = (agora + timedelta(days=dias_frente)).date()
                break
        dm = re.search(r'(\d{1,2})/(\d{1,2})', t)
        if dm:
            data = date(agora.year, int(dm.group(2)), int(dm.group(1)))
        if not data:
            data = (agora + timedelta(days=1)).date()
    inicio = TZ.localize(datetime(data.year, data.month, data.day, hora, minuto))
    fim = inicio + timedelta(hours=1)
    return inicio.isoformat(), fim.isoformat()

def extrair_titulo_evento(texto):
    t = texto
    for w in ["agendar","agenda","criar evento","marcar","reunião com","reuniao com","compromisso com"]:
        t = re.sub(w, "", t, flags=re.IGNORECASE)
    t = re.sub(r'\b(hoje|amanhã|amanha|segunda|terça|terca|quarta|quinta|sexta|sábado|sabado|domingo)\b', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\d{1,2}h\d{0,2}', '', t)
    t = re.sub(r'\d{1,2}/\d{1,2}', '', t)
    return t.strip(" —-,.")[:60] or "Compromisso"

# ─── EXTRATORES DE OBRA ───────────────────────────────────────────────────────
def extrair_valor(texto):
    t = texto.lower()
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*mil', t)
    if m: return float(m.group(1).replace(',','.')) * 1000
    m = re.search(r'(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?)', t)
    if m: return float(m.group(1).replace('.','').replace(',','.'))
    m = re.search(r'r\$\s*(\d+(?:[.,]\d+)?)', t)
    if m: return float(m.group(1).replace(',','.'))
    m = re.search(r'(\d{4,})', t)
    if m: return float(m.group(1))
    m = re.search(r'(\d+(?:[.,]\d+)?)', t)
    if m: return float(m.group(1).replace(',','.'))
    return 0

def extrair_dias(texto):
    m = re.search(r'(\d+)\s*dias?', texto.lower())
    return int(m.group(1)) if m else 1

def is_fab(texto):
    t = texto.lower()
    for w in ["fabrica","fábrica","fabricação","fabricacao","oficina","producao"]:
        if w in t: return True
    for w in ["instalação","instalacao","instala","obra","campo"]:
        if w in t: return False
    return True

def extrair_funcoes(texto, funcionarios):
    t = texto.lower()
    return [f for f, als in ALIASES_FUNCAO.items() if any(a in t for a in als)]

def extrair_forn(texto):
    m = re.search(r'(?:na|da|do|no|em|pela|pelo|empresa)\s+([A-ZÁÉÍÓÚÂÊÔÃÕ][A-Za-záéíóúâêôãõüç\s&\-]{1,30}?)(?:\s+por|,|\.|\s+[rR]\$|$)', texto)
    return m.group(1).strip() if m else ""

PALAVRAS_MAO  = ["serralheiro","ajudante","instalador","cortador","montador","mao de obra","mão de obra","fabricação","fabricacao","instalação","instalacao","fábrica","fabrica","dias de","dia de"]
PALAVRAS_IMP  = ["imposto","nota fiscal","nf ","inss","iss","icms","simples","tributo","taxa","das"]
PALAVRAS_MAT  = ["aluminio","alumínio","vidro","acessorio","acessório","ferragem","perfil","borracha","silicone","parafuso","chapa","kit","material","fita","fechadura","trilho","selante","espelho"]
PALAVRAS_EMAIL= ["email","e-mail","emails","mensagem","caixa","gmail","não lido","nao lido","responde","responder","responda"]
PALAVRAS_AGENDA=["agenda","reunião","reuniao","compromisso","evento","calendário","calendario","hoje","amanhã","amanha","agendar","marcar","horário"]
PALAVRAS_TAREFA=["tarefa","lembrar","lembrete","cobrar","pendente","prazo"]

def cat_geral(texto):
    t = texto.lower()
    if any(p in t for p in ["agendar","criar reunião","criar evento","marcar reunião","marcar evento"]):
        return "criar_evento"
    if any(p in t for p in ["responde","responder","responda"]) and any(p in t for p in ["email","e-mail","dizendo","falando"]):
        return "responder_email"
    for p in PALAVRAS_EMAIL:
        if p in t: return "email"
    for p in PALAVRAS_AGENDA:
        if p in t: return "agenda"
    for p in PALAVRAS_TAREFA:
        if p in t: return "tarefa"
    for p in PALAVRAS_MAO:
        if p in t: return "mao"
    for p in PALAVRAS_IMP:
        if p in t: return "imposto"
    for p in PALAVRAS_MAT:
        if p in t: return "material"
    return "outros"

def processar_lancamento(texto, funcionarios):
    cat = cat_geral(texto)
    t = texto.lower()
    if cat == "mao":
        dias = extrair_dias(texto)
        funcoes = extrair_funcoes(texto, funcionarios)
        fab = is_fab(texto)
        cat_f = "hh_fabricacao" if fab else "hh_instalacao"
        v_direto = extrair_valor(texto) if re.search(r'r\$|\d{4,}', t) else 0
        if funcoes and v_direto == 0:
            total, dets = 0, []
            for f in funcoes:
                sal = funcionarios.get(f, FUNCOES_PADRAO.get(f, 0))
                if sal > 0:
                    c = custo_dia(sal) * dias
                    total += c
                    dets.append(f"{f.capitalize()} ({dias}d × {fmt(custo_dia(sal))}/dia)")
            if total > 0:
                tipo = "Fabricação" if fab else "Instalação"
                desc = f"{' + '.join(f.capitalize() for f in funcoes)} — {dias} dia(s) {tipo}"
                return cat_f, total, "", desc, f"\n📊 {' + '.join(dets)} = {fmt(total)}"
        elif v_direto > 0:
            tipo = "Fabricação" if fab else "Instalação"
            funcs = " + ".join(f.capitalize() for f in funcoes) if funcoes else "Mão de Obra"
            return cat_f, v_direto, "", f"{funcs} — {dias} dia(s) {tipo}", ""
        return None,None,None,None,"⚠️ Função não identificada.\n\nEx: _Serralheiro 2 dias fábrica_"
    if cat == "material":
        v = extrair_valor(texto)
        if v <= 0: return None,None,None,None,"⚠️ Valor não encontrado."
        forn = extrair_forn(texto)
        mats = ["aluminio","alumínio","vidro","acessorio","perfil","ferragem","silicone","parafuso","chapa","borracha","kit"]
        desc = next((m.capitalize() for m in mats if m in t), "Material")
        if forn: desc += f" — {forn}"
        return "material", v, forn, desc, ""
    if cat == "imposto":
        v = extrair_valor(texto)
        if v <= 0: return None,None,None,None,"⚠️ Valor não encontrado."
        return "imposto", v, "", "Imposto / Nota Fiscal", ""
    v = extrair_valor(texto)
    if v <= 0: return None,None,None,None,"⚠️ Não entendi. Tente novamente."
    return "outros", v, "", texto[:50], ""

# ─── JOBS AGENDADOS ───────────────────────────────────────────────────────────
async def job_bom_dia(context):
    """Todo dia às 7h — bom dia + agenda + alertas"""
    svc = get_calendar()
    eventos = buscar_eventos(svc, 1) if svc else []
    txt = f"☀️ *Bom dia, Samuel!*\n📅 *{hoje()}*\n\n"
    if eventos:
        txt += "📋 *Sua agenda hoje:*\n"
        for e in eventos:
            ini = e["start"].get("dateTime","")
            hora = ini[11:16] if "T" in ini else "Dia todo"
            txt += f"🕐 *{hora}* — {e.get('summary','')}\n"
    else:
        txt += "📅 Agenda livre hoje! ✅\n"
    dados = carregar(OWNER_ID)
    pendentes = [t for t in dados.get("tarefas",[]) if not t.get("concluida")]
    if pendentes:
        txt += f"\n⚠️ *{len(pendentes)} tarefa(s) pendente(s)* — /tarefas"
    alertas = []
    for oid, obra in dados.get("obras",{}).items():
        lans = obra.get("lancamentos",[])
        total = sum(l["valor"] for l in lans)
        pct = (total/obra["valor"]*100) if obra["valor"] > 0 else 0
        if pct >= 80:
            alertas.append(f"🔴 *{obra['nome']}*: {pct:.0f}% do orçamento usado")
    if alertas:
        txt += "\n\n🚨 *Alertas de obra:*\n" + "\n".join(alertas)
    txt += "\n\n_Tenha um ótimo dia!_ 💪"
    await context.bot.send_message(chat_id=OWNER_ID, text=txt, parse_mode="Markdown")

async def job_lembrete_agenda(context):
    """A cada 15 min — lembra compromissos em ~30 min"""
    svc = get_calendar()
    if not svc: return
    agora = datetime.now(TZ)
    eventos = buscar_proximos_eventos(svc, 40)
    for e in eventos:
        ini_str = e["start"].get("dateTime","")
        if not ini_str: continue
        try:
            ini = datetime.fromisoformat(ini_str)
            if ini.tzinfo is None:
                ini = TZ.localize(ini)
            diff = (ini - agora).total_seconds() / 60
            if 25 <= diff <= 35:
                titulo = e.get("summary","Compromisso")
                hora = ini_str[11:16]
                await context.bot.send_message(
                    chat_id=OWNER_ID,
                    text=f"⏰ *Lembrete!*\n\n📅 *{titulo}*\nDaqui a ~30 minutos ({hora})\n\n_Prepare-se!_ 💼",
                    parse_mode="Markdown"
                )
        except Exception as ex:
            print(f"Erro lembrete: {ex}")

async def job_resumo_emails(context):
    """A cada 3h — resumo de e-mails importantes"""
    svc = get_gmail()
    if not svc: return
    emails = buscar_emails(svc, 8)
    if not emails: return
    spam = ["desconto","promoção","oferta","newsletter","unsubscribe","cupom","linkedin","pinterest","noreply"]
    importantes = [e for e in emails if not any(w in (e["assunto"]+e["de"]).lower() for w in spam)]
    if not importantes: return
    txt = f"📧 *{len(importantes)} e-mail(s) importante(s):*\n\n"
    for i, e in enumerate(importantes[:4], 1):
        txt += f"*{i}.* {e['assunto'][:50]}\n   _{e['de'][:35]}_\n   {e['preview'][:80]}\n\n"
    await context.bot.send_message(chat_id=OWNER_ID, text=txt, parse_mode="Markdown")

async def job_cobrar_tarefas(context):
    """Todo dia às 9h — cobra tarefas vencidas"""
    dados = carregar(OWNER_ID)
    hoje_dt = datetime.now(TZ).date()
    vencidas = []
    for t in dados.get("tarefas", []):
        if t.get("concluida"): continue
        prazo = t.get("prazo","")
        if prazo:
            for fmt_prazo in ["%d/%m/%Y", "%d/%m"]:
                try:
                    ano = hoje_dt.year
                    prazo_dt = datetime.strptime(prazo if "/" in prazo and len(prazo) > 5 else f"{prazo}/{ano}", "%d/%m/%Y").date()
                    if prazo_dt < hoje_dt:
                        vencidas.append(t)
                    break
                except: pass
    if not vencidas: return
    txt = "🚨 *Tarefas vencidas!*\n\n"
    for t in vencidas:
        resp = f" — *{t['responsavel']}*" if t.get("responsavel") else ""
        txt += f"• {t['descricao'][:60]}{resp}\n"
    txt += "\n_Use /tarefas para ver todas_ 📋"
    await context.bot.send_message(chat_id=OWNER_ID, text=txt, parse_mode="Markdown")

async def job_relatorio_semanal(context):
    """Toda segunda às 8h — resumo de obras"""
    if datetime.now(TZ).weekday() != 0: return
    dados = carregar(OWNER_ID)
    obras = dados.get("obras", {})
    if not obras: return
    txt = "📊 *Relatório Semanal — Obras*\n\n"
    obras_ord = sorted(obras.items(), key=lambda x: (
        (sum(l["valor"] for l in x[1].get("lancamentos",[])) / x[1]["valor"] * 100) if x[1]["valor"] > 0 else 0
    ), reverse=True)
    for oid, obra in obras_ord:
        lans = obra.get("lancamentos",[])
        total = sum(l["valor"] for l in lans)
        lucro = obra["valor"] - total
        pct = (total/obra["valor"]*100) if obra["valor"] > 0 else 0
        mgm = (lucro/obra["valor"]*100) if obra["valor"] > 0 else 0
        emoji = "🔴" if pct >= 80 else "🟡" if pct >= 60 else "🟢"
        txt += f"{emoji} *{obra['nome']}*\n   Contrato: {fmt(obra['valor'])} | Gasto: {pct:.0f}% | Margem: {mgm:.1f}%\n\n"
    await context.bot.send_message(chat_id=OWNER_ID, text=txt, parse_mode="Markdown")

async def job_glass_care(context):
    """Todo dia às 8h10 — verifica manutenções Glass Care no CRM Firebase"""
    hoje_dt = date.today()
    em_7_str  = (hoje_dt + timedelta(days=7)).isoformat()
    hoje_str  = hoje_dt.isoformat()
    alertas   = []

    try:
        for doc in db.collection("obras").stream():
            obra      = doc.to_dict()
            obra_id   = doc.id
            nome_obra = obra.get("nome", obra.get("cliente", "Obra sem nome"))
            cliente   = obra.get("nomeCliente", obra.get("cliente", "cliente"))
            whatsapp  = obra.get("whatsappCliente", "")
            entrega   = obra.get("dataEntregaReal", "")
            pos_obra  = obra.get("posObra", {})
            manut     = pos_obra.get("manutencaoGC", [])
            lem30     = pos_obra.get("lembrete30", False)

            # ── Pesquisa satisfação 30 dias ──────────────────────────────
            if entrega and not lem30 and whatsapp:
                try:
                    dias_desde = (hoje_dt - date.fromisoformat(entrega)).days
                    if dias_desde == 30:
                        msg = (
                            f"Olá {cliente}! 😊\n\n"
                            f"A *Fermet Esquadrias* passou 30 dias desde a entrega da sua obra "
                            f"e gostaríamos de saber:\n\n"
                            f"✅ Como estão funcionando as esquadrias?\n"
                            f"✅ Ficou satisfeito com o resultado?\n"
                            f"✅ Tem alguma dúvida ou necessidade de ajuste?\n\n"
                            f"Estamos à disposição! 🏗️\n\n"
                            f"*Fermet — 42 anos de excelência em esquadrias metálicas*"
                        )
                        if enviar_whatsapp(whatsapp, msg):
                            db.collection("obras").document(obra_id).update({"posObra.lembrete30": True})
                            alertas.append(f"📞 *Pesquisa 30 dias enviada!*\nObra: {nome_obra} | Cliente: {cliente}")
                except Exception as e:
                    print(f"Erro lembrete30 {obra_id}: {e}")

            # ── Manutenções próximas ──────────────────────────────────────
            atualizado = False
            for i, m in enumerate(manut):
                data_m    = m.get("data", "")
                concluida = m.get("concluida", False)
                wp_env    = m.get("whatsappEnviado", False)
                if concluida or not data_m: continue

                try:
                    data_m_dt  = date.fromisoformat(data_m)
                    data_m_fmt = data_m_dt.strftime("%d/%m/%Y")
                    dias_falt  = (data_m_dt - hoje_dt).days
                except:
                    continue

                # 7 dias antes — envia WhatsApp + cria evento no Google Calendar
                if data_m == em_7_str and not wp_env and whatsapp:
                    msg = (
                        f"Olá {cliente}! 👋\n\n"
                        f"A *Fermet Esquadrias* irá realizar a manutenção preventiva "
                        f"das suas esquadrias e limpeza dos vidros.\n\n"
                        f"📅 *Data prevista:* {data_m_fmt}\n\n"
                        f"Confirme sua disponibilidade respondendo "
                        f"*SIM* para confirmar ou *NÃO* para reagendarmos.\n\n"
                        f"Qualquer dúvida estamos à disposição! 🪟\n\n"
                        f"*Fermet — 42 anos de excelência em esquadrias metálicas*"
                    )
                    if enviar_whatsapp(whatsapp, msg):
                        manut[i]["whatsappEnviado"] = True
                        manut[i]["dataEnvio"] = hoje_str
                        atualizado = True

                        # Cria evento no Google Calendar
                        svc_cal = get_calendar()
                        link_cal = ""
                        if svc_cal:
                            ini_cal = TZ.localize(datetime(data_m_dt.year, data_m_dt.month, data_m_dt.day, 9, 0))
                            fim_cal = ini_cal + timedelta(hours=2)
                            link_cal = criar_evento_cal(
                                svc_cal,
                                f"🪟 Glass Care — {nome_obra}",
                                ini_cal.isoformat(),
                                fim_cal.isoformat(),
                                f"Manutenção preventiva Glass Care\nCliente: {cliente}\nTel: {whatsapp}"
                            )

                        alertas.append(
                            f"🪟 *Glass Care agendado!*\n"
                            f"Obra: {nome_obra} | {data_m_fmt}\n"
                            f"Cliente: {cliente}\n"
                            f"WhatsApp enviado ✅"
                            + (f"\n📅 Evento criado no Calendar!" if link_cal else "")
                        )

                # 1 dia antes — lembrete Telegram
                elif dias_falt == 1 and not concluida:
                    alertas.append(
                        f"⏰ *AMANHÃ — Glass Care!*\n"
                        f"Obra: {nome_obra} | {data_m_fmt}\n"
                        f"Cliente: {cliente} | 📞 {whatsapp or 'sem WhatsApp'}\n"
                        f"Confirme o técnico!"
                    )

                # No dia — alerta urgente
                elif dias_falt == 0 and not concluida:
                    alertas.append(
                        f"🚨 *HOJE — Glass Care!*\n"
                        f"Obra: {nome_obra}\n"
                        f"Cliente: {cliente} | 📞 {whatsapp or 'sem WhatsApp'}\n"
                        f"O técnico foi agendado?"
                    )

                # Vencida — alerta vermelho
                elif dias_falt < 0 and not concluida:
                    alertas.append(
                        f"🔴 *VENCIDA há {abs(dias_falt)} dias!*\n"
                        f"Obra: {nome_obra} | {data_m_fmt}\n"
                        f"Cliente: {cliente}"
                    )

            if atualizado:
                db.collection("obras").document(obra_id).update({"posObra.manutencaoGC": manut})

    except Exception as e:
        print(f"Erro job_glass_care: {e}")
        await context.bot.send_message(chat_id=OWNER_ID, text=f"⚠️ Erro Glass Care: {e}", parse_mode="Markdown")
        return

    if alertas:
        txt = "🪟 *Glass Care — Relatório Diário*\n\n" + "\n\n".join(alertas)
        await context.bot.send_message(chat_id=OWNER_ID, text=txt, parse_mode="Markdown")

# ─── COMANDO MANUAL /glasscare ────────────────────────────────────────────────
async def cmd_glasscare(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Roda o Glass Care manualmente agora"""
    if update.effective_user.id != OWNER_ID: return
    await update.message.reply_text("🪟 *Verificando Glass Care agora...*", parse_mode="Markdown")

    hoje_dt  = date.today()
    hoje_str = hoje_dt.isoformat()
    alertas  = []
    enviados = 0
    erros    = 0
    total_obras = 0

    try:
        for doc in db.collection("obras").stream():
            total_obras += 1
            obra      = doc.to_dict()
            obra_id   = doc.id
            nome_obra = obra.get("nome", obra.get("cliente", "Obra sem nome"))
            cliente   = obra.get("nomeCliente", obra.get("cliente", "cliente"))
            whatsapp  = obra.get("whatsappCliente", "")
            entrega   = obra.get("dataEntregaReal", "")
            pos_obra  = obra.get("posObra", {})
            manut     = pos_obra.get("manutencaoGC", [])
            lem30     = pos_obra.get("lembrete30", False)

            # Pesquisa 30 dias
            if entrega and not lem30 and whatsapp:
                try:
                    dias_desde = (hoje_dt - date.fromisoformat(entrega)).days
                    if dias_desde >= 28:
                        msg = (
                            f"Olá {cliente}! 😊\n\n"
                            f"A *Fermet Esquadrias* passou pelo prazo de 30 dias "
                            f"desde a entrega da sua obra e gostaríamos de saber:\n\n"
                            f"✅ Como estão funcionando as esquadrias?\n"
                            f"✅ Ficou satisfeito com o resultado?\n"
                            f"✅ Tem alguma dúvida ou necessidade de ajuste?\n\n"
                            f"Estamos à disposição! 🏗️\n\n"
                            f"*Fermet — 42 anos de excelência em esquadrias metálicas*"
                        )
                        if enviar_whatsapp(whatsapp, msg):
                            db.collection("obras").document(obra_id).update({"posObra.lembrete30": True})
                            enviados += 1
                            alertas.append(f"📞 *Pesquisa satisfação enviada*\nObra: {nome_obra} | {cliente}")
                        else:
                            erros += 1
                except Exception as e:
                    erros += 1
                    print(f"Erro lembrete30 {obra_id}: {e}")

            # Manutenções
            atualizado = False
            for i, m in enumerate(manut):
                data_m    = m.get("data", "")
                concluida = m.get("concluida", False)
                wp_env    = m.get("whatsappEnviado", False)
                if concluida or not data_m: continue

                try:
                    data_m_dt  = date.fromisoformat(data_m)
                    data_m_fmt = data_m_dt.strftime("%d/%m/%Y")
                    dias_falt  = (data_m_dt - hoje_dt).days
                except:
                    continue

                if dias_falt < 0:
                    alertas.append(f"🔴 *VENCIDA há {abs(dias_falt)} dias!*\nObra: {nome_obra} | {data_m_fmt}\nCliente: {cliente} | 📞 {whatsapp or 'sem WhatsApp'}")
                elif dias_falt == 0:
                    alertas.append(f"🚨 *HOJE — Glass Care!*\nObra: {nome_obra}\nCliente: {cliente} | 📞 {whatsapp or 'sem WhatsApp'}")
                elif dias_falt == 1:
                    alertas.append(f"⏰ *AMANHÃ — Glass Care!*\nObra: {nome_obra} | {data_m_fmt}\nCliente: {cliente}\nConfirme o técnico!")
                elif dias_falt <= 7 and not wp_env and whatsapp:
                    msg = (
                        f"Olá {cliente}! 👋\n\n"
                        f"A *Fermet Esquadrias* irá realizar a manutenção preventiva "
                        f"das suas esquadrias e limpeza dos vidros.\n\n"
                        f"📅 *Data prevista:* {data_m_fmt}\n\n"
                        f"Confirme respondendo *SIM* ou *NÃO* para reagendarmos.\n\n"
                        f"🪟 *Fermet — 42 anos de excelência em esquadrias metálicas*"
                    )
                    if enviar_whatsapp(whatsapp, msg):
                        manut[i]["whatsappEnviado"] = True
                        manut[i]["dataEnvio"] = hoje_str
                        atualizado = True
                        enviados += 1

                        # Google Calendar
                        svc_cal = get_calendar()
                        link_cal = ""
                        if svc_cal:
                            ini_cal = TZ.localize(datetime(data_m_dt.year, data_m_dt.month, data_m_dt.day, 9, 0))
                            fim_cal = ini_cal + timedelta(hours=2)
                            link_cal = criar_evento_cal(
                                svc_cal,
                                f"🪟 Glass Care — {nome_obra}",
                                ini_cal.isoformat(),
                                fim_cal.isoformat(),
                                f"Manutenção preventiva Glass Care\nCliente: {cliente}\nTel: {whatsapp}"
                            )

                        alertas.append(
                            f"✅ *WhatsApp enviado!*\n"
                            f"Obra: {nome_obra} | {data_m_fmt}\n"
                            f"Cliente: {cliente}"
                            + (f"\n📅 Evento criado no Calendar!" if link_cal else "")
                        )
                    else:
                        erros += 1
                        alertas.append(f"❌ *Erro ao enviar WhatsApp*\nObra: {nome_obra} | {cliente}\nNúmero: {whatsapp}")
                elif 8 <= dias_falt <= 30:
                    alertas.append(f"📅 *Glass Care em {dias_falt} dias*\nObra: {nome_obra} | {data_m_fmt}\nCliente: {cliente}")

            if atualizado:
                db.collection("obras").document(obra_id).update({"posObra.manutencaoGC": manut})

        if not alertas:
            await update.message.reply_text(
                f"✅ *Glass Care verificado!*\n\n"
                f"📊 {total_obras} obra(s) analisada(s)\n"
                f"Nenhuma manutenção próxima ou pendente.\n\n"
                f"_Próxima verificação automática: amanhã às 8h10_",
                parse_mode="Markdown"
            )
        else:
            txt = f"🪟 *Glass Care — Relatório Manual*\n"
            txt += f"📊 {total_obras} obra(s) | ✅ {enviados} enviado(s) | ❌ {erros} erro(s)\n\n"
            txt += "\n\n".join(alertas)
            await update.message.reply_text(txt, parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"❌ *Erro:*\n`{str(e)}`", parse_mode="Markdown")

# ─── WHISPER ──────────────────────────────────────────────────────────────────
async def transcrever_audio(file_path: str) -> str:
    try:
        with open(file_path, "rb") as audio_file:
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1", file=audio_file, language="pt"
            )
        return transcript.text
    except Exception as e:
        print(f"Erro Whisper: {e}")
        return ""

# ─── HANDLERS ─────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = ReplyKeyboardMarkup([
        [KeyboardButton("/resumo"), KeyboardButton("/obras")],
        [KeyboardButton("/agenda"), KeyboardButton("/emails")],
        [KeyboardButton("/tarefas"), KeyboardButton("/funcionarios")],
        [KeyboardButton("/relatorio"), KeyboardButton("/apagar_ultimo")],
        [KeyboardButton("/glasscare")],
    ], resize_keyboard=True)
    await update.message.reply_text(
        "🏗️ *Abigail 2.0* — Assistente Executiva!\n\n"
        "📋 *Obras:* _Alumínio BPA R$ 40.000_ | _Serralheiro 2 dias fábrica_\n"
        "📅 *Ver agenda:* /agenda\n"
        "📅 *Criar evento:* _Agendar reunião com Pablo sexta 14h_\n"
        "📧 *Ver e-mails:* /emails\n"
        "📧 *Responder:* _Responde Paulo dizendo que confirmo a reunião_\n"
        "✅ *Tarefas:* /tarefas\n"
        "🪟 *Glass Care:* /glasscare\n\n"
        "_Estou aqui pra te ajudar a focar no que importa!_ 💪",
        parse_mode="Markdown", reply_markup=kb
    )

async def agenda_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Buscando sua agenda...")
    svc = get_calendar()
    if not svc:
        await update.message.reply_text("⚠️ Google Agenda não conectado."); return
    eventos = buscar_eventos(svc, 1)
    if not eventos:
        await update.message.reply_text(f"📅 Nenhum compromisso hoje ({hoje()})! Dia livre ✅"); return
    txt = f"📅 *Agenda de hoje — {hoje()}:*\n\n"
    for e in eventos:
        ini = e["start"].get("dateTime", "")
        hora = ini[11:16] if "T" in ini else "Dia todo"
        txt += f"🕐 *{hora}* — {e.get('summary','Sem título')}\n"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def emails_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📧 Buscando e-mails não lidos...")
    svc = get_gmail()
    if not svc:
        await update.message.reply_text("⚠️ Gmail não conectado."); return
    emails = buscar_emails(svc, 5)
    if not emails:
        await update.message.reply_text("📧 Nenhum e-mail não lido! ✅"); return
    txt = "📧 *E-mails não lidos:*\n\n"
    for i, e in enumerate(emails, 1):
        txt += f"*{i}.* {e['assunto'][:50]}\n   _{e['de'][:35]}_\n   {e['preview'][:80]}...\n\n"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def tarefas_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    pendentes = [t for t in dados.get("tarefas", []) if not t.get("concluida")]
    if not pendentes:
        await update.message.reply_text("✅ Nenhuma tarefa pendente!"); return
    txt = "✅ *Tarefas pendentes:*\n\n"
    for i, t in enumerate(pendentes, 1):
        resp  = f" — *{t['responsavel']}*" if t.get("responsavel") else ""
        prazo = f" — até {t['prazo']}" if t.get("prazo") else ""
        txt += f"{i}. {t['descricao']}{resp}{prazo}\n"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def funcionarios_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    funcs = get_funcionarios(dados)
    args = ctx.args
    if len(args) == 2:
        fi = args[0].lower()
        f = next((k for k, als in ALIASES_FUNCAO.items() if fi in als or fi == k), fi)
        try:
            sal = float(args[1].replace(",", "."))
            dados.setdefault("funcionarios", FUNCOES_PADRAO.copy())[f] = sal
            salvar(uid, dados)
            await update.message.reply_text(f"✅ *{f.capitalize()}*: {fmt(sal)}/mês → {fmt(custo_dia(sal))}/dia", parse_mode="Markdown")
        except:
            await update.message.reply_text("Formato: `/funcionarios serralheiro 2500`", parse_mode="Markdown")
        return
    txt = f"👷 *Funcionários* _(base {DIAS_UTEIS_MES}d úteis/mês)_\n\n"
    for f, sal in funcs.items():
        txt += f"🔧 *{f.capitalize()}* — {fmt(sal)}/mês → *{fmt(custo_dia(sal))}/dia*\n"
    txt += "\nAtualizar: `/funcionarios serralheiro 2500`"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def nova_obra(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    texto = " ".join(ctx.args) if ctx.args else ""
    partes = re.split(r'\s*[-—–]\s*', texto, 1)
    if len(partes) < 2:
        await update.message.reply_text("Formato: `/nova_obra Nome — Valor`", parse_mode="Markdown"); return
    nome, valor = partes[0].strip(), extrair_valor(partes[1])
    if not nome or valor <= 0:
        await update.message.reply_text("Nome ou valor inválido."); return
    oid = re.sub(r'\s+', '_', nome.lower())[:20]
    dados.setdefault("obras", {})[oid] = {"nome": nome, "valor": valor, "lancamentos": []}
    dados["obra_atual"] = oid
    salvar(uid, dados)
    await update.message.reply_text(f"✅ Obra *{nome}* criada!\n💰 Contrato: *{fmt(valor)}*", parse_mode="Markdown")

async def trocar_obra(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    busca = " ".join(ctx.args).lower().strip() if ctx.args else ""
    if not busca: await listar_obras(update, ctx); return
    for oid, obra in dados.get("obras", {}).items():
        if busca in obra["nome"].lower():
            dados["obra_atual"] = oid
            salvar(uid, dados)
            await update.message.reply_text(f"✅ Obra ativa: *{obra['nome']}*", parse_mode="Markdown"); return
    await update.message.reply_text("Obra não encontrada. Use /obras.")

async def listar_obras(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    if not dados.get("obras"):
        await update.message.reply_text("Nenhuma obra.\nUse: `/nova_obra Nome — Valor`", parse_mode="Markdown"); return
    txt = "📋 *Suas obras:*\n\n"
    for oid, obra in dados["obras"].items():
        ativo = " ← ativa" if oid == dados.get("obra_atual") else ""
        total = sum(l["valor"] for l in obra.get("lancamentos", []))
        lucro = obra["valor"] - total
        mgm   = (lucro / obra["valor"] * 100) if obra["valor"] > 0 else 0
        txt  += f"🏗️ *{obra['nome']}*{ativo}\n   {fmt(obra['valor'])} | Lucro: {fmt(lucro)} ({mgm:.1f}%)\n   `/trocar {obra['nome']}`\n\n"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def resumo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    oid = dados.get("obra_atual")
    if not oid or oid not in dados.get("obras", {}):
        await update.message.reply_text("Nenhuma obra ativa. Use /obras."); return
    obra = dados["obras"][oid]
    lans = obra.get("lancamentos", [])
    s = lambda c: sum(l["valor"] for l in lans if l["cat"] == c)
    mat,hhf,hhi,imp,out = s("material"),s("hh_fabricacao"),s("hh_instalacao"),s("imposto"),s("outros")
    total = mat+hhf+hhi+imp+out
    lucro = obra["valor"] - total
    pct   = (total/obra["valor"]*100) if obra["valor"] > 0 else 0
    mgm   = (lucro/obra["valor"]*100) if obra["valor"] > 0 else 0
    bar   = "█"*int(pct/10) + "░"*(10-int(pct/10))
    txt   = f"📊 *{obra['nome']}*\n{'─'*28}\n💼 Contrato: *{fmt(obra['valor'])}*\n\n"
    txt  += f"🔩 Materiais:     {fmt(mat)}\n🏭 MO Fabricação: {fmt(hhf)}\n🔧 MO Instalação: {fmt(hhi)}\n🧾 Impostos:      {fmt(imp)}\n"
    if out > 0: txt += f"📦 Outros: {fmt(out)}\n"
    txt  += f"{'─'*28}\n💸 Total: *{fmt(total)}* ({pct:.1f}%)\n[{bar}]\n\n"
    txt  += f"{'✅' if lucro>=0 else '🔴'} Lucro: *{fmt(lucro)}* ({mgm:.1f}%)\n\n_Últimos lançamentos:_\n"
    for l in reversed(lans[-5:]):
        txt += f"• {l['data']} — {l['desc']} — {fmt(l['valor'])}\n"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def apagar_ultimo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    oid = dados.get("obra_atual")
    if not oid or oid not in dados.get("obras", {}): return
    lans = dados["obras"][oid].get("lancamentos", [])
    if not lans: await update.message.reply_text("Não há lançamentos."); return
    u = lans.pop()
    dados["obras"][oid]["lancamentos"] = lans
    salvar(uid, dados)
    await update.message.reply_text(f"🗑️ Removido: *{u['desc']}* — {fmt(u['valor'])}", parse_mode="Markdown")

async def relatorio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    oid = dados.get("obra_atual")
    if not oid or oid not in dados.get("obras", {}): return
    obra = dados["obras"][oid]
    lans = obra.get("lancamentos", [])
    total = sum(l["valor"] for l in lans)
    lucro = obra["valor"] - total
    labels = {"material":"Material","hh_fabricacao":"MO Fabricação","hh_instalacao":"MO Instalação","imposto":"Imposto","outros":"Outros"}
    linhas = ["Data,Descricao,Fornecedor,Categoria,Valor"] + \
             [f"{l['data']},\"{l['desc']}\",\"{l.get('forn','')}\",{labels.get(l['cat'],l['cat'])},{l['valor']:.2f}" for l in lans]
    linhas += ["", f",,Contrato,,{obra['valor']:.2f}", f",,Total,,{total:.2f}", f",,Lucro,,{lucro:.2f}"]
    nome_arq = obra["nome"].replace(" ", "_") + "_custos.csv"
    with open(nome_arq, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(linhas))
    await update.message.reply_document(
        document=open(nome_arq,"rb"), filename=nome_arq,
        caption=f"📊 *{obra['nome']}*\n{fmt(obra['valor'])} | Gasto: {fmt(total)} | Lucro: {fmt(lucro)}",
        parse_mode="Markdown"
    )

async def receber_mensagem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    texto = update.message.text
    cat = cat_geral(texto)

    if cat == "criar_evento":
        svc = get_calendar()
        if not svc:
            await update.message.reply_text("⚠️ Google Agenda não conectado."); return
        titulo = extrair_titulo_evento(texto)
        inicio, fim = parse_data_hora(texto)
        link = criar_evento_cal(svc, titulo, inicio, fim)
        hora = inicio[11:16]
        data_fmt = f"{inicio[8:10]}/{inicio[5:7]}/{inicio[:4]}"
        if link:
            await update.message.reply_text(
                f"📅 *Evento criado!*\n\n📌 *{titulo}*\n🗓️ {data_fmt} às {hora}\n\n✅ Adicionado ao Google Agenda!",
                parse_mode="Markdown")
        else:
            await update.message.reply_text("⚠️ Erro ao criar evento.")
        return

    if cat == "responder_email":
        svc = get_gmail()
        if not svc:
            await update.message.reply_text("⚠️ Gmail não conectado."); return
        m = re.search(r'(?:responde?|responda)\s+(?:o\s+|a\s+|ao\s+)?([A-Za-záéíóúâêôãõ]+)\s+(?:dizendo|falando|que|:)\s+(.+)', texto, re.IGNORECASE)
        if not m:
            await update.message.reply_text("Formato: _Responde Paulo dizendo que confirmo a reunião_", parse_mode="Markdown"); return
        nome_dest = m.group(1)
        corpo = m.group(2)
        emails = buscar_emails(svc, 10)
        dest = next((e for e in emails if nome_dest.lower() in e["de"].lower()), None)
        if not dest:
            await update.message.reply_text(f"⚠️ Não encontrei e-mail recente de *{nome_dest}*.", parse_mode="Markdown"); return
        assunto = f"Re: {dest['assunto']}"
        email_m = re.search(r'<(.+?)>', dest["reply_to"])
        para = email_m.group(1) if email_m else dest["reply_to"]
        ok = enviar_email(svc, para, assunto, corpo)
        if ok:
            await update.message.reply_text(f"📧 *E-mail enviado!*\n\nPara: _{para}_\nAssunto: _{assunto}_", parse_mode="Markdown")
        else:
            await update.message.reply_text("⚠️ Erro ao enviar e-mail.")
        return

    if cat == "email":
        svc = get_gmail()
        if not svc:
            await update.message.reply_text("⚠️ Gmail não conectado."); return
        emails = buscar_emails(svc, 3)
        if not emails: await update.message.reply_text("📧 Nenhum e-mail não lido! ✅"); return
        txt = "📧 *E-mails recentes:*\n\n"
        for i, e in enumerate(emails, 1):
            txt += f"*{i}.* {e['assunto'][:50]}\n   _{e['de'][:35]}_\n   {e['preview'][:80]}\n\n"
        await update.message.reply_text(txt, parse_mode="Markdown"); return

    if cat == "agenda":
        svc = get_calendar()
        if not svc:
            await update.message.reply_text("⚠️ Agenda não conectada."); return
        eventos = buscar_eventos(svc, 1)
        if not eventos: await update.message.reply_text(f"📅 Nenhum compromisso hoje! ✅"); return
        txt = f"📅 *Agenda de hoje:*\n\n"
        for e in eventos:
            ini = e["start"].get("dateTime","")
            hora = ini[11:16] if "T" in ini else "Dia todo"
            txt += f"🕐 *{hora}* — {e.get('summary','')}\n"
        await update.message.reply_text(txt, parse_mode="Markdown"); return

    if cat == "tarefa":
        desc    = texto.replace("tarefa:","").replace("Tarefa:","").strip()
        prazo_m = re.search(r'até\s+(\S+(?:\s+\S+)?)', texto.lower())
        resp_m  = re.search(r'(?:para|com|cobrar)\s+([A-Z][a-zA-Z]+)', texto)
        t = {
            "id": f"t_{int(datetime.now().timestamp())}",
            "descricao": desc[:100],
            "responsavel": resp_m.group(1) if resp_m else "",
            "prazo": prazo_m.group(1) if prazo_m else "",
            "criada": hoje(), "concluida": False
        }
        dados.setdefault("tarefas", []).append(t)
        salvar(uid, dados)
        r = f" para *{t['responsavel']}*" if t["responsavel"] else ""
        p = f" até *{t['prazo']}*" if t["prazo"] else ""
        await update.message.reply_text(f"✅ Tarefa criada{r}{p}!\n📌 _{desc[:80]}_", parse_mode="Markdown"); return

    oid = dados.get("obra_atual")
    if not oid or oid not in dados.get("obras", {}):
        await update.message.reply_text("⚠️ Nenhuma obra ativa!\n\n`/nova_obra Nome — 100000`", parse_mode="Markdown"); return

    cat_obra, valor, forn, desc, extra = processar_lancamento(texto, get_funcionarios(dados))
    if cat_obra is None:
        await update.message.reply_text(extra or "⚠️ Não entendi.", parse_mode="Markdown"); return

    obra = dados["obras"][oid]
    obra.setdefault("lancamentos", []).append({
        "data": hoje(), "desc": desc, "forn": forn or "", "cat": cat_obra, "valor": round(valor, 2)
    })
    salvar(uid, dados)

    total = sum(l["valor"] for l in obra["lancamentos"])
    lucro = obra["valor"] - total
    pct   = (total/obra["valor"]*100) if obra["valor"] > 0 else 0
    mgm   = (lucro/obra["valor"]*100) if obra["valor"] > 0 else 0
    labels = {"material":"🔩 Material","hh_fabricacao":"🏭 MO Fabricação","hh_instalacao":"🔧 MO Instalação","imposto":"🧾 Imposto","outros":"📦 Outros"}
    txt = f"✅ *{labels.get(cat_obra,'📦')}* lançado!{extra}\n\n📌 {desc}\n💰 *{fmt(valor)}*\n\n📊 *{obra['nome']}:*\nTotal: {fmt(total)} ({pct:.1f}%)\n{'✅' if lucro>=0 else '🔴'} Lucro: *{fmt(lucro)}* ({mgm:.1f}%)"
    if pct >= 80:
        txt += f"\n\n🚨 *ATENÇÃO: Obra em {pct:.0f}% do orçamento!*"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def receber_audio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎤 Transcrevendo seu áudio...")
    try:
        audio = update.message.voice or update.message.audio
        file  = await ctx.bot.get_file(audio.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            with open(tmp.name, "rb") as f:
                transcript = openai_client.audio.transcriptions.create(
                    model="whisper-1", file=f, language="pt"
                )
        texto = transcript.text
        await update.message.reply_text(f"🎤 _\"{texto}\"_", parse_mode="Markdown")
        update.message.text = texto
        await receber_mensagem(update, ctx)
    except Exception as e:
        print(f"Erro áudio: {e}")
        await update.message.reply_text("⚠️ Erro ao transcrever áudio. Tente novamente.")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    if not TOKEN:
        print("ERRO: BOT_TOKEN não definido!"); return

    app = Application.builder().token(TOKEN).build()

    # Comandos
    for cmd, handler in [
        ("start",        start),
        ("nova_obra",    nova_obra),
        ("trocar",       trocar_obra),
        ("obras",        listar_obras),
        ("resumo",       resumo),
        ("relatorio",    relatorio),
        ("apagar_ultimo",apagar_ultimo),
        ("funcionarios", funcionarios_cmd),
        ("agenda",       agenda_cmd),
        ("emails",       emails_cmd),
        ("tarefas",      tarefas_cmd),
        ("glasscare",    cmd_glasscare),   # ← NOVO
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receber_mensagem))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, receber_audio))

    # Jobs agendados
    if OWNER_ID:
        jq = app.job_queue
        # Bom dia 7h BH (10h UTC)
        jq.run_daily(job_bom_dia,         time=datetime_time(10, 0,  tzinfo=pytz.utc))
        # Lembrete agenda a cada 15 min
        jq.run_repeating(job_lembrete_agenda, interval=900, first=60)
        # Resumo e-mails a cada 3h
        jq.run_repeating(job_resumo_emails,   interval=10800, first=300)
        # Cobrar tarefas 9h BH (12h UTC)
        jq.run_daily(job_cobrar_tarefas,  time=datetime_time(12, 0,  tzinfo=pytz.utc))
        # Relatório semanal segunda 8h BH (11h UTC)
        jq.run_daily(job_relatorio_semanal, time=datetime_time(11, 0, tzinfo=pytz.utc))
        # Glass Care 8h10 BH (11h10 UTC) ← NOVO
        jq.run_daily(job_glass_care,      time=datetime_time(11, 10, tzinfo=pytz.utc))
        print(f"✅ Jobs agendados — OWNER_ID: {OWNER_ID}")

    print("✅ Abigail 2.0 + Glass Care — Online!")
    app.run_polling()

if __name__ == "__main__":
    main()
