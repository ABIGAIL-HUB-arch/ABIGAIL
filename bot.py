import os
import json
import re
from datetime import date
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import firebase_admin
from firebase_admin import credentials, firestore

# ─── TOKEN E FIREBASE ────────────────────────────────────────────────────────
TOKEN = os.environ.get("BOT_TOKEN", "")

cred_json = os.environ.get("FIREBASE_CREDENTIALS", "")
if cred_json:
    cred_dict = json.loads(cred_json)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
else:
    print("ERRO: variável FIREBASE_CREDENTIALS não definida!")

db = firestore.client()

# ─── FUNÇÕES FIREBASE ─────────────────────────────────────────────────────────
def carregar(uid):
    uid = str(uid)
    doc = db.collection("usuarios").document(uid).get()
    if doc.exists:
        return doc.to_dict()
    return {"obras": {}, "obra_atual": None, "funcionarios": {}}

def salvar(uid, dados):
    uid = str(uid)
    db.collection("usuarios").document(uid).set(dados)

# ─── FORMATAÇÃO ───────────────────────────────────────────────────────────────
def fmt(v):
    return f"R$ {float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def hoje():
    return date.today().strftime("%d/%m/%Y")

DIAS_UTEIS_MES = 22

# ─── FUNCIONÁRIOS PADRÃO ─────────────────────────────────────────────────────
FUNCOES_PADRAO = {
    "serralheiro": 2200,
    "ajudante":    1600,
    "instalador":  2000,
    "cortador" : 2700,
}

ALIASES_FUNCAO = {
    "serralheiro": ["serralheiro", "serralheiros"],
    "ajudante":    ["ajudante", "ajudantes", "auxiliar", "auxiliares"],
    "instalador":  ["instalador", "instaladores", "montador", "montadores"],
}

def get_funcionarios(dados):
    return dados.get("funcionarios") or FUNCOES_PADRAO.copy()

def custo_dia(salario_mensal):
    return salario_mensal / DIAS_UTEIS_MES

# ─── EXTRATORES ───────────────────────────────────────────────────────────────
def extrair_valor(texto):
    t = texto.lower()
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*mil', t)
    if m: return float(m.group(1).replace(',', '.')) * 1000
    m = re.search(r'(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?)', t)
    if m: return float(m.group(1).replace('.', '').replace(',', '.'))
    m = re.search(r'r\$\s*(\d+(?:[.,]\d+)?)', t)
    if m: return float(m.group(1).replace(',', '.'))
    m = re.search(r'(\d{4,})', t)
    if m: return float(m.group(1))
    m = re.search(r'(\d+(?:[.,]\d+)?)', t)
    if m: return float(m.group(1).replace(',', '.'))
    return 0

def extrair_dias(texto):
    t = texto.lower()
    m = re.search(r'(\d+)\s*dias?', t)
    if m: return int(m.group(1))
    palavras = {"um": 1, "uma": 1, "dois": 2, "duas": 2, "três": 3, "tres": 3,
                "quatro": 4, "cinco": 5, "seis": 6, "sete": 7, "oito": 8}
    for p, v in palavras.items():
        if p + " dia" in t: return v
    return 1

def is_fabricacao(texto):
    t = texto.lower()
    fab_words = ["fabrica", "fábrica", "fabricação", "fabricacao", "oficina",
                 "producao", "produção", "montagem", "corte", "solda"]
    inst_words = ["instalação", "instalacao", "instala", "obra", "campo", "cliente"]
    for w in fab_words:
        if w in t: return True
    for w in inst_words:
        if w in t: return False
    return True

def extrair_funcoes_texto(texto, funcionarios):
    t = texto.lower()
    encontradas = []
    for funcao, aliases in ALIASES_FUNCAO.items():
        for alias in aliases:
            if alias in t:
                encontradas.append(funcao)
                break
    return encontradas

def extrair_fornecedor(texto):
    m = re.search(
        r'(?:na|da|do|no|em|pela|pelo|empresa|fornecedor)\s+'
        r'([A-ZÁÉÍÓÚÂÊÔÃÕ][A-Za-záéíóúâêôãõüç\s&\-]{1,30}?)'
        r'(?:\s+por|\s+no\s+valor|,|\.|\s+[rR]\$|$)', texto)
    if m: return m.group(1).strip()
    m = re.search(
        r'([A-ZÁÉÍÓÚÂÊÔÃÕ][A-Za-záéíóúâêôãõüç\s&\-]{2,25}?)'
        r'\s+por\s+[rR]?\$?\s*\d', texto)
    if m: return m.group(1).strip()
    return ""

PALAVRAS_MAO = ["serralheiro", "ajudante", "instalador", "montador",
                "mao de obra", "mão de obra", "hora homem",
                "diaria", "diária", "dias de", "dia de",
                "funcionario", "funcionário", "fabricação", "fabricacao",
                "instalação", "instalacao", "fábrica", "fabrica"]
PALAVRAS_IMP = ["imposto", "nota fiscal", "nf ", "inss", "iss", "icms",
                "simples", "tributo", "taxa", "encargo", "das"]
PALAVRAS_MAT = ["aluminio", "alumínio", "vidro", "acessorio", "acessório",
                "ferragem", "perfil", "borracha", "silicone", "parafuso",
                "chapa", "kit", "material", "insumo", "fita", "fechadura",
                "trilho", "roldana", "massa", "selante", "esquadria", "espelho"]

def extrair_categoria(texto):
    t = texto.lower()
    for p in PALAVRAS_MAO:
        if p in t: return "mao"
    for p in PALAVRAS_IMP:
        if p in t: return "imposto"
    for p in PALAVRAS_MAT:
        if p in t: return "material"
    return "outros"

def processar_mensagem(texto, funcionarios):
    cat = extrair_categoria(texto)
    t = texto.lower()

    if cat == "mao":
        dias = extrair_dias(texto)
        funcoes = extrair_funcoes_texto(texto, funcionarios)
        fab = is_fabricacao(texto)
        cat_final = "hh_fabricacao" if fab else "hh_instalacao"

        # Verifica valor explícito
        valor_direto = 0
        if re.search(r'r\$\s*\d', t) or re.search(r'\d+\s*mil', t):
            valor_direto = extrair_valor(texto)

        if funcoes and valor_direto == 0:
            total = 0
            detalhes = []
            for funcao in funcoes:
                salario = funcionarios.get(funcao, FUNCOES_PADRAO.get(funcao, 0))
                if salario > 0:
                    custo = custo_dia(salario) * dias
                    total += custo
                    detalhes.append(f"{funcao.capitalize()} ({dias}d × {fmt(custo_dia(salario))}/dia)")
            if total > 0:
                tipo_str = "Fabricação" if fab else "Instalação"
                funcs_str = " + ".join(f.capitalize() for f in funcoes)
                desc = f"{funcs_str} — {dias} dia(s) {tipo_str}"
                extra = f"\n📊 {' + '.join(detalhes)} = {fmt(total)}"
                return cat_final, total, "", desc, extra
            else:
                return None, None, None, None, \
                    "⚠️ Função não cadastrada. Use /funcionarios para ver as funções disponíveis."
        elif valor_direto > 0:
            tipo_str = "Fabricação" if fab else "Instalação"
            funcs_str = " + ".join(f.capitalize() for f in funcoes) if funcoes else "Mão de Obra"
            desc = f"{funcs_str} — {dias} dia(s) {tipo_str}"
            return cat_final, valor_direto, "", desc, ""
        else:
            return None, None, None, None, \
                "⚠️ Não identifiquei a função.\n\nExemplos:\n_Serralheiro 2 dias fábrica_\n_Instalador e ajudante 3 dias instalação_"

    if cat == "material":
        valor = extrair_valor(texto)
        forn = extrair_fornecedor(texto)
        if valor <= 0:
            return None, None, None, None, "⚠️ Não encontrei o valor. Ex: _Alumínio BPA por R$ 40.000_"
        mats = ["aluminio", "alumínio", "vidro", "acessorio", "acessório", "perfil",
                "ferragem", "silicone", "parafuso", "chapa", "borracha", "kit"]
        desc = next((m.capitalize() for m in mats if m in t), "Material")
        if forn: desc += f" — {forn}"
        return "material", valor, forn, desc, ""

    if cat == "imposto":
        valor = extrair_valor(texto)
        if valor <= 0:
            return None, None, None, None, "⚠️ Não encontrei o valor do imposto."
        return "imposto", valor, "", "Imposto / Nota Fiscal", ""

    valor = extrair_valor(texto)
    if valor <= 0:
        return None, None, None, None, "⚠️ Não entendi. Tente novamente."
    desc = texto[:50] + ("..." if len(texto) > 50 else "")
    return "outros", valor, "", desc, ""

# ─── HANDLERS ─────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    teclado = ReplyKeyboardMarkup(
        [[KeyboardButton("/resumo"), KeyboardButton("/obras")],
         [KeyboardButton("/nova_obra"), KeyboardButton("/funcionarios")],
         [KeyboardButton("/relatorio"), KeyboardButton("/apagar_ultimo")]],
        resize_keyboard=True
    )
    await update.message.reply_text(
        "🏗️ *ObraControl Bot* — Bem-vindo!\n\n"
        "Me mande os gastos em linguagem natural:\n\n"
        "• _Alumínio BPA por R$ 40.000_\n"
        "• _Serralheiro 2 dias fábrica_\n"
        "• _Instalador e ajudante 3 dias instalação_\n"
        "• _Imposto NF R$ 3.500_\n\n"
        "⚙️ Configure seus funcionários: /funcionarios\n\n"
        "Comandos:\n"
        "/nova\\_obra Nome — 100000\n"
        "/trocar Nome → mudar obra ativa\n"
        "/resumo → ver custos e lucro\n"
        "/obras → listar obras\n"
        "/funcionarios → ver/configurar salários\n"
        "/relatorio → receber CSV\n"
        "/apagar\\_ultimo → remover último lançamento",
        parse_mode="Markdown",
        reply_markup=teclado
    )

async def funcionarios_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    funcs = get_funcionarios(dados)
    args = ctx.args

    if len(args) == 2:
        funcao_input = args[0].lower()
        funcao = funcao_input
        for f, aliases in ALIASES_FUNCAO.items():
            if funcao_input in aliases or funcao_input == f:
                funcao = f
                break
        try:
            salario = float(args[1].replace(",", "."))
            if "funcionarios" not in dados:
                dados["funcionarios"] = FUNCOES_PADRAO.copy()
            dados["funcionarios"][funcao] = salario
            salvar(uid, dados)
            custo = custo_dia(salario)
            await update.message.reply_text(
                f"✅ *{funcao.capitalize()}* atualizado!\n"
                f"💰 Salário: {fmt(salario)}/mês\n"
                f"📅 Custo/dia: *{fmt(custo)}*\n"
                f"_(base {DIAS_UTEIS_MES} dias úteis/mês)_",
                parse_mode="Markdown"
            )
        except:
            await update.message.reply_text(
                "Formato inválido.\nUse: `/funcionarios serralheiro 2500`",
                parse_mode="Markdown"
            )
        return

    txt = f"👷 *Seus Funcionários*\n_Base: {DIAS_UTEIS_MES} dias úteis/mês_\n\n"
    for funcao, salario in funcs.items():
        custo = custo_dia(salario)
        txt += f"🔧 *{funcao.capitalize()}*\n"
        txt += f"   Salário: {fmt(salario)}/mês → {fmt(custo)}/dia\n\n"
    txt += "Para atualizar o salário:\n"
    txt += "`/funcionarios serralheiro 2500`\n"
    txt += "`/funcionarios ajudante 1800`\n"
    txt += "`/funcionarios instalador 2200`"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def nova_obra(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    texto = " ".join(ctx.args) if ctx.args else ""
    partes = re.split(r'\s*[-—–]\s*', texto, 1)
    if len(partes) < 2:
        await update.message.reply_text(
            "Formato: /nova\\_obra *Nome — Valor*\n\nEx:\n`/nova_obra Residência João — 100000`",
            parse_mode="Markdown"
        )
        return
    nome = partes[0].strip()
    valor = extrair_valor(partes[1])
    if not nome or valor <= 0:
        await update.message.reply_text("Nome ou valor inválido.")
        return
    oid = re.sub(r'\s+', '_', nome.lower())[:20]
    if "obras" not in dados: dados["obras"] = {}
    dados["obras"][oid] = {"nome": nome, "valor": valor, "lancamentos": []}
    dados["obra_atual"] = oid
    salvar(uid, dados)
    await update.message.reply_text(
        f"✅ Obra *{nome}* criada!\n💰 Contrato: *{fmt(valor)}*\n\nAgora me mande os gastos!",
        parse_mode="Markdown"
    )

async def trocar_obra(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    busca = " ".join(ctx.args).lower().strip() if ctx.args else ""
    if not busca:
        await listar_obras(update, ctx)
        return
    for oid, obra in dados.get("obras", {}).items():
        if busca in obra["nome"].lower() or busca == oid:
            dados["obra_atual"] = oid
            salvar(uid, dados)
            await update.message.reply_text(
                f"✅ Obra ativa: *{obra['nome']}*\n💰 {fmt(obra['valor'])}",
                parse_mode="Markdown"
            )
            return
    await update.message.reply_text("Obra não encontrada. Use /obras.")

async def listar_obras(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    if not dados.get("obras"):
        await update.message.reply_text("Nenhuma obra.\nUse: /nova\\_obra Nome — Valor", parse_mode="Markdown")
        return
    txt = "📋 *Suas obras:*\n\n"
    for oid, obra in dados["obras"].items():
        ativo = " ← ativa" if oid == dados.get("obra_atual") else ""
        total = sum(l["valor"] for l in obra.get("lancamentos", []))
        lucro = obra["valor"] - total
        margem = (lucro / obra["valor"] * 100) if obra["valor"] > 0 else 0
        txt += f"🏗️ *{obra['nome']}*{ativo}\n"
        txt += f"   {fmt(obra['valor'])} | Gasto: {fmt(total)} | Lucro: {fmt(lucro)} ({margem:.1f}%)\n"
        txt += f"   `/trocar {obra['nome']}`\n\n"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def resumo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    oid = dados.get("obra_atual")
    if not oid or oid not in dados.get("obras", {}):
        await update.message.reply_text("Nenhuma obra ativa. Use /obras.")
        return
    obra = dados["obras"][oid]
    lans = obra.get("lancamentos", [])
    soma = lambda c: sum(l["valor"] for l in lans if l["cat"] == c)
    mat = soma("material")
    hhf = soma("hh_fabricacao")
    hhi = soma("hh_instalacao")
    imp = soma("imposto")
    out = soma("outros")
    total = mat + hhf + hhi + imp + out
    lucro = obra["valor"] - total
    pct = (total / obra["valor"] * 100) if obra["valor"] > 0 else 0
    mgm = (lucro / obra["valor"] * 100) if obra["valor"] > 0 else 0
    barra = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
    txt  = f"📊 *{obra['nome']}*\n{'─'*30}\n"
    txt += f"💼 Contrato: *{fmt(obra['valor'])}*\n\n"
    txt += f"🔩 Materiais:      {fmt(mat)}\n"
    txt += f"🏭 MO Fabricação:  {fmt(hhf)}\n"
    txt += f"🔧 MO Instalação:  {fmt(hhi)}\n"
    txt += f"🧾 Impostos:       {fmt(imp)}\n"
    if out > 0: txt += f"📦 Outros:         {fmt(out)}\n"
    txt += f"{'─'*30}\n"
    txt += f"💸 Total: *{fmt(total)}* ({pct:.1f}%)\n[{barra}]\n\n"
    txt += f"{'✅' if lucro >= 0 else '🔴'} Lucro: *{fmt(lucro)}* ({mgm:.1f}%)\n\n"
    txt += "_Últimos lançamentos:_\n"
    for l in reversed(lans[-5:]):
        txt += f"• {l['data']} — {l['desc']} — {fmt(l['valor'])}\n"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def apagar_ultimo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    oid = dados.get("obra_atual")
    if not oid or oid not in dados.get("obras", {}):
        await update.message.reply_text("Nenhuma obra ativa.")
        return
    lans = dados["obras"][oid].get("lancamentos", [])
    if not lans:
        await update.message.reply_text("Não há lançamentos.")
        return
    ultimo = lans.pop()
    dados["obras"][oid]["lancamentos"] = lans
    salvar(uid, dados)
    await update.message.reply_text(
        f"🗑️ Removido: *{ultimo['desc']}* — {fmt(ultimo['valor'])}",
        parse_mode="Markdown"
    )

async def relatorio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    oid = dados.get("obra_atual")
    if not oid or oid not in dados.get("obras", {}):
        await update.message.reply_text("Nenhuma obra ativa.")
        return
    obra = dados["obras"][oid]
    lans = obra.get("lancamentos", [])
    total = sum(l["valor"] for l in lans)
    lucro = obra["valor"] - total
    labels = {"material": "Material", "hh_fabricacao": "MO Fabricação",
              "hh_instalacao": "MO Instalação", "imposto": "Imposto", "outros": "Outros"}
    linhas = ["Data,Descricao,Fornecedor,Categoria,Valor"]
    for l in lans:
        linhas.append(f"{l['data']},\"{l['desc']}\",\"{l.get('forn','')}\",{labels.get(l['cat'],l['cat'])},{l['valor']:.2f}")
    linhas += ["", f",,Contrato,,{obra['valor']:.2f}",
               f",,Total,,{total:.2f}", f",,Lucro,,{lucro:.2f}",
               f",,Margem,,{(lucro/obra['valor']*100):.1f}%" if obra['valor'] > 0 else ",,Margem,,0%"]
    nome_arquivo = obra["nome"].replace(" ", "_") + "_custos.csv"
    with open(nome_arquivo, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(linhas))
    await update.message.reply_document(
        document=open(nome_arquivo, "rb"),
        filename=nome_arquivo,
        caption=f"📊 *{obra['nome']}*\n{fmt(obra['valor'])} | Gasto: {fmt(total)} | Lucro: {fmt(lucro)}",
        parse_mode="Markdown"
    )

async def receber_mensagem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dados = carregar(uid)
    oid = dados.get("obra_atual")
    texto = update.message.text
    if not oid or oid not in dados.get("obras", {}):
        await update.message.reply_text(
            "⚠️ Nenhuma obra ativa!\n\n`/nova_obra Nome — 100000`",
            parse_mode="Markdown"
        )
        return
    funcionarios = get_funcionarios(dados)
    cat, valor, forn, desc, extra = processar_mensagem(texto, funcionarios)
    if cat is None:
        await update.message.reply_text(extra, parse_mode="Markdown")
        return
    obra = dados["obras"][oid]
    if "lancamentos" not in obra: obra["lancamentos"] = []
    obra["lancamentos"].append({
        "data": hoje(), "desc": desc,
        "forn": forn or "", "cat": cat, "valor": round(valor, 2)
    })
    salvar(uid, dados)
    total = sum(l["valor"] for l in obra["lancamentos"])
    lucro = obra["valor"] - total
    pct = (total / obra["valor"] * 100) if obra["valor"] > 0 else 0
    mgm = (lucro / obra["valor"] * 100) if obra["valor"] > 0 else 0
    labels = {"material": "🔩 Material", "hh_fabricacao": "🏭 MO Fabricação",
              "hh_instalacao": "🔧 MO Instalação", "imposto": "🧾 Imposto", "outros": "📦 Outros"}
    txt  = f"✅ *{labels.get(cat,'📦')}* lançado!{extra}\n\n"
    txt += f"📌 {desc}\n💰 Valor: *{fmt(valor)}*\n\n"
    txt += f"📊 *{obra['nome']}:*\n"
    txt += f"Total: {fmt(total)} ({pct:.1f}%)\n"
    txt += f"{'✅' if lucro>=0 else '🔴'} Lucro: *{fmt(lucro)}* ({mgm:.1f}%)"
    await update.message.reply_text(txt, parse_mode="Markdown")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    if not TOKEN:
        print("ERRO: BOT_TOKEN não definido!")
        return
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",         start))
    app.add_handler(CommandHandler("nova_obra",     nova_obra))
    app.add_handler(CommandHandler("trocar",        trocar_obra))
    app.add_handler(CommandHandler("obras",         listar_obras))
    app.add_handler(CommandHandler("resumo",        resumo))
    app.add_handler(CommandHandler("relatorio",     relatorio))
    app.add_handler(CommandHandler("apagar_ultimo", apagar_ultimo))
    app.add_handler(CommandHandler("funcionarios",  funcionarios_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receber_mensagem))
    print("✅ Bot rodando com Firebase + Funcionários!")
    app.run_polling()

if __name__ == "__main__":
    main()
