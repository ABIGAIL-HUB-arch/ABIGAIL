import os
import json
import re
import pickle
import base64
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import firebase_admin
from firebase_admin import credentials, firestore
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ─── TOKENS ──────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("BOT_TOKEN", "")

# ─── FIREBASE ────────────────────────────────────────────────────────────────
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

GOOGLE_CLIENT_ID = "82433511683-s1liut9i7bpbqq2ou08nqmuuppondj0b.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

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
    return date.today().strftime("%d/%m/%Y")

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
                metadataHeaders=["From","Subject","Date"]).execute()
            h = {x["name"]: x["value"] for x in d["payload"]["headers"]}
            emails.append({"id": m["id"], "de": h.get("From",""), "assunto": h.get("Subject",""), "preview": d.get("snippet","")[:150]})
        return emails
    except: return []

# ─── CALENDAR HELPERS ─────────────────────────────────────────────────────────
def buscar_eventos(service, dias=1):
    try:
        agora = datetime.utcnow().isoformat() + "Z"
        fim = (datetime.utcnow() + timedelta(days=dias)).isoformat() + "Z"
        res = service.events().list(calendarId="primary", timeMin=agora, timeMax=fim,
            singleEvents=True, orderBy="startTime").execute()
        return res.get("items", [])
    except: return []

def criar_evento_cal(service, titulo, inicio_dt, fim_dt, descricao=""):
    try:
        ev = {
            "summary": titulo, "description": descricao,
            "start": {"dateTime": inicio_dt, "timeZone": "America/Sao_Paulo"},
            "end": {"dateTime": fim_dt, "timeZone": "America/Sao_Paulo"},
        }
        return service.events().insert(calendarId="primary", body=ev).execute().get("htmlLink","")
    except: return None

# ─── EXTRATORES ───────────────────────────────────────────────────────────────
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

PALAVRAS_MAO = ["serralheiro","ajudante","instalador","cortador","montador","mao de obra","mão de obra","fabricação","fabricacao","instalação","instalacao","fábrica","fabrica","dias de","dia de"]
PALAVRAS_IMP = ["imposto","nota fiscal","nf ","inss","iss","icms","simples","tributo","taxa","das"]
PALAVRAS_MAT = ["aluminio","alumínio","vidro","acessorio","acessório","ferragem","perfil","borracha","silicone","parafuso","chapa","kit","material","fita","fechadura","trilho","selante","espelho"]
PALAVRAS_EMAIL = ["email","e-mail","emails","mensagem","caixa","gmail","não lido","nao lido"]
PALAVRAS_AGENDA = ["agenda","reunião","reuniao","compromisso","evento","calendário","calendario","hoje","amanhã","amanha","agendar","marcar","horário"]
PALAVRAS_TAREFA = ["tarefa","lembrar","lembrete","cobrar","pendente","prazo"]

def cat_geral(texto):
    t = texto.lower()
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

# ─── HANDLERS ─────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = ReplyKeyboardMarkup([
        [KeyboardButton("/resumo"), KeyboardButton("/obras")],
        [KeyboardButton("/agenda"), KeyboardButton("/emails")],
        [KeyboardButton("/tarefas"), KeyboardButton("/funcionarios")],
        [KeyboardButton("/relatorio"), KeyboardButton("/apagar_ultimo")],
    ], resize_keyboard=True)
    await update.message.reply_text(
        "🏗️ *Abigail 2.0* — Assistente Executiva!\n\n"
        "📋 *Obras:* _Alumínio BPA R$ 40.000_ | _Serralheiro 2 dias fábrica_\n"
        "📅 *Agenda:* /agenda ou _ver minha agenda hoje_\n"
        "📧 *E-mails:* /emails ou _ver meus emails_\n"
        "✅ *Tarefas:* /tarefas ou _Tarefa: Pablo levantar material até sexta_\n"
        "👷 *Funcionários:* /funcionarios\n\n"
        "_Estou aqui pra te ajudar a focar no que importa!_ 💪",
        parse_mode="Markdown", reply_markup=kb
    )

async def agenda_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Buscando sua agenda...")
    svc = get_calendar()
    if not svc:
        await update.message.reply_text("⚠️ Google Agenda não conectado ainda.\n\nVou te guiar para conectar em breve!", parse_mode="Markdown")
        return
    eventos = buscar_eventos(svc, 1)
    if not eventos:
        await update.message.reply_text(f"📅 Nenhum compromisso hoje ({hoje()})! Dia livre ✅")
        return
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
        await update.message.reply_text("⚠️ Gmail não conectado ainda.\n\nVou te guiar para conectar em breve!", parse_mode="Markdown")
        return
    emails = buscar_emails(svc, 5)
    if not emails:
        await update.message.reply_text("📧 Nenhum e-mail não lido! Caixa limpa ✅")
        return
    txt = "📧 *E-mails não lidos:*\n\n"
    for i, e in enumerate(emails, 1):
        de = e["de"][:35]
        txt += f"*{i}.* {e['assunto'][:50]}\n   _{de}_\n   {e['preview'][:80]}...\n\n"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def tarefas_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    pendentes = [t for t in dados.get("tarefas", []) if not t.get("concluida")]
    if not pendentes:
        await update.message.reply_text("✅ Nenhuma tarefa pendente! Tudo em dia.")
        return
    txt = "✅ *Tarefas pendentes:*\n\n"
    for i, t in enumerate(pendentes, 1):
        resp = f" — *{t['responsavel']}*" if t.get("responsavel") else ""
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
        await update.message.reply_text("Formato: `/nova_obra Nome — Valor`\nEx: `/nova_obra João Silva — 100000`", parse_mode="Markdown")
        return
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
            await update.message.reply_text(f"✅ Obra ativa: *{obra['nome']}*", parse_mode="Markdown")
            return
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
        mgm = (lucro / obra["valor"] * 100) if obra["valor"] > 0 else 0
        txt += f"🏗️ *{obra['nome']}*{ativo}\n   {fmt(obra['valor'])} | Lucro: {fmt(lucro)} ({mgm:.1f}%)\n   `/trocar {obra['nome']}`\n\n"
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
    pct = (total/obra["valor"]*100) if obra["valor"] > 0 else 0
    mgm = (lucro/obra["valor"]*100) if obra["valor"] > 0 else 0
    bar = "█"*int(pct/10) + "░"*(10-int(pct/10))
    txt = f"📊 *{obra['nome']}*\n{'─'*28}\n💼 Contrato: *{fmt(obra['valor'])}*\n\n"
    txt += f"🔩 Materiais:     {fmt(mat)}\n🏭 MO Fabricação: {fmt(hhf)}\n🔧 MO Instalação: {fmt(hhi)}\n🧾 Impostos:      {fmt(imp)}\n"
    if out > 0: txt += f"📦 Outros:        {fmt(out)}\n"
    txt += f"{'─'*28}\n💸 Total: *{fmt(total)}* ({pct:.1f}%)\n[{bar}]\n\n"
    txt += f"{'✅' if lucro>=0 else '🔴'} Lucro: *{fmt(lucro)}* ({mgm:.1f}%)\n\n_Últimos lançamentos:_\n"
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
    linhas = ["Data,Descricao,Fornecedor,Categoria,Valor"] + [f"{l['data']},\"{l['desc']}\",\"{l.get('forn','')}\",{labels.get(l['cat'],l['cat'])},{l['valor']:.2f}" for l in lans]
    linhas += ["", f",,Contrato,,{obra['valor']:.2f}", f",,Total,,{total:.2f}", f",,Lucro,,{lucro:.2f}"]
    nome_arq = obra["nome"].replace(" ", "_") + "_custos.csv"
    with open(nome_arq, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(linhas))
    await update.message.reply_document(document=open(nome_arq,"rb"), filename=nome_arq,
        caption=f"📊 *{obra['nome']}*\n{fmt(obra['valor'])} | Gasto: {fmt(total)} | Lucro: {fmt(lucro)}", parse_mode="Markdown")

async def receber_mensagem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    texto = update.message.text
    cat = cat_geral(texto)

    if cat == "email":
        svc = get_gmail()
        if not svc:
            await update.message.reply_text("⚠️ Gmail não conectado ainda. Em breve te ajudo a conectar!"); return
        emails = buscar_emails(svc, 3)
        if not emails: await update.message.reply_text("📧 Nenhum e-mail não lido! ✅"); return
        txt = "📧 *E-mails recentes:*\n\n"
        for i, e in enumerate(emails, 1):
            txt += f"*{i}.* {e['assunto'][:50]}\n   _{e['de'][:35]}_\n   {e['preview'][:80]}\n\n"
        await update.message.reply_text(txt, parse_mode="Markdown"); return

    if cat == "agenda":
        svc = get_calendar()
        if not svc:
            await update.message.reply_text("⚠️ Agenda não conectada ainda. Em breve te ajudo a conectar!"); return
        eventos = buscar_eventos(svc, 1)
        if not eventos: await update.message.reply_text(f"📅 Nenhum compromisso hoje! ✅"); return
        txt = f"📅 *Agenda de hoje:*\n\n"
        for e in eventos:
            ini = e["start"].get("dateTime","")
            hora = ini[11:16] if "T" in ini else "Dia todo"
            txt += f"🕐 *{hora}* — {e.get('summary','')}\n"
        await update.message.reply_text(txt, parse_mode="Markdown"); return

    if cat == "tarefa":
        desc = texto.replace("tarefa:","").replace("Tarefa:","").strip()
        prazo_m = re.search(r'até\s+(\w+\s*\w*)', texto.lower())
        resp_m = re.search(r'(?:para|com|cobrar)\s+([A-Z][a-zA-Z]+)', texto)
        t = {"id": f"t_{int(datetime.now().timestamp())}", "descricao": desc[:100],
             "responsavel": resp_m.group(1) if resp_m else "",
             "prazo": prazo_m.group(1) if prazo_m else "",
             "criada": hoje(), "concluida": False}
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
    obra.setdefault("lancamentos", []).append({"data": hoje(), "desc": desc, "forn": forn or "", "cat": cat_obra, "valor": round(valor, 2)})
    salvar(uid, dados)

    total = sum(l["valor"] for l in obra["lancamentos"])
    lucro = obra["valor"] - total
    pct = (total/obra["valor"]*100) if obra["valor"] > 0 else 0
    mgm = (lucro/obra["valor"]*100) if obra["valor"] > 0 else 0
    labels = {"material":"🔩 Material","hh_fabricacao":"🏭 MO Fabricação","hh_instalacao":"🔧 MO Instalação","imposto":"🧾 Imposto","outros":"📦 Outros"}
    txt = f"✅ *{labels.get(cat_obra,'📦')}* lançado!{extra}\n\n📌 {desc}\n💰 *{fmt(valor)}*\n\n📊 *{obra['nome']}:*\nTotal: {fmt(total)} ({pct:.1f}%)\n{'✅' if lucro>=0 else '🔴'} Lucro: *{fmt(lucro)}* ({mgm:.1f}%)"
    await update.message.reply_text(txt, parse_mode="Markdown")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    if not TOKEN: print("ERRO: BOT_TOKEN não definido!"); return
    app = Application.builder().token(TOKEN).build()
    for cmd, handler in [
        ("start", start), ("nova_obra", nova_obra), ("trocar", trocar_obra),
        ("obras", listar_obras), ("resumo", resumo), ("relatorio", relatorio),
        ("apagar_ultimo", apagar_ultimo), ("funcionarios", funcionarios_cmd),
        ("agenda", agenda_cmd), ("emails", emails_cmd), ("tarefas", tarefas_cmd),
    ]:
        app.add_handler(CommandHandler(cmd, handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receber_mensagem))
    print("✅ Abigail 2.0 — Gmail + Calendar + Obras + Tarefas!")
    app.run_polling()

if __name__ == "__main__":
    main()
