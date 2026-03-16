import os
import json
import re
from datetime import date
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ─── TOKEN ────────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("BOT_TOKEN", "")

# ─── BANCO DE DADOS (arquivo JSON local) ─────────────────────────────────────
DB_FILE = "obras.json"

def carregar():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def salvar(dados):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)

def get_user(dados, uid):
    uid = str(uid)
    if uid not in dados:
        dados[uid] = {"obras": {}, "obra_atual": None}
    return dados[uid]

# ─── FORMATAÇÃO ───────────────────────────────────────────────────────────────
def fmt(v):
    return f"R$ {float(v):,.0f}".replace(",", ".")

def hoje():
    return date.today().strftime("%d/%m/%Y")

# ─── INTERPRETADOR IA LOCAL ──────────────────────────────────────────────────
PALAVRAS_MAO   = ["serralheiro","instalador","ajudante","montador","pedreiro","pintor",
                  "mao de obra","mão de obra","hora homem","diaria","diária","dias",
                  "funcionario","funcionário","trabalhador","servico","serviço",
                  "instalacao","instalação","fabricacao","fabricação"]
PALAVRAS_IMP   = ["imposto","nota fiscal","nf ","inss","iss","icms","simples",
                  "tributo","taxa","encargo"]
PALAVRAS_MAT   = ["aluminio","alumínio","vidro","acessorio","acessório","ferragem",
                  "perfil","borracha","silicone","parafuso","chapa","kit","material",
                  "insumo","fita","fechadura","trilho","roldana","massa","selante",
                  "esquadria","espelho"]
TARIFAS        = {"serralheiro":250,"instalador":220,"ajudante":150,
                  "montador":200,"pintor":180,"pedreiro":200}

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

def extrair_categoria(texto):
    t = texto.lower()
    for p in PALAVRAS_MAO:
        if p in t: return "mao"
    for p in PALAVRAS_IMP:
        if p in t: return "imposto"
    for p in PALAVRAS_MAT:
        if p in t: return "material"
    return "outros"

def extrair_fornecedor(texto):
    m = re.search(r'(?:na|da|do|no|em|pela|pelo|empresa|fornecedor)\s+([A-ZÁÉÍÓÚÂÊÔÃÕ][A-Za-záéíóúâêôãõüç\s&\-]{1,30}?)(?:\s+por|\s+no\s+valor|,|\.|\s+[rR]\$|$)', texto)
    if m: return m.group(1).strip()
    m = re.search(r'([A-ZÁÉÍÓÚÂÊÔÃÕ][A-Za-záéíóúâêôãõüç\s&\-]{2,25}?)\s+por\s+[rR]?\$?\s*\d', texto)
    if m: return m.group(1).strip()
    return ""

def calcular_mao_obra(texto):
    t = texto.lower()
    md = re.search(r'(\d+)\s*dias?', t)
    dias = int(md.group(1)) if md else 1
    total, itens = 0, []
    for func, tarifa in TARIFAS.items():
        if func in t:
            v = dias * tarifa
            total += v
            itens.append(f"{func} ({dias}d × R${tarifa})")
    return total, " + ".join(itens)

def processar_mensagem(texto):
    cat   = extrair_categoria(texto)
    valor = extrair_valor(texto)
    forn  = extrair_fornecedor(texto)
    extra = ""

    if cat == "mao" and valor == 0:
        valor, resumo = calcular_mao_obra(texto)
        if valor > 0:
            extra = f"\n📊 Calculado: {resumo} = {fmt(valor)}"

    if valor <= 0:
        return None, None, None, None, \
               "⚠️ Não encontrei o *valor* na mensagem.\n\nExemplo: _Alumínio BPA por R$ 40.000_"

    t = texto.lower()
    if cat == "material":
        mats = ["aluminio","alumínio","vidro","acessorio","acessório","perfil",
                "ferragem","silicone","parafuso","chapa","borracha","kit"]
        desc = next((m for m in mats if m in t), "Material")
        desc = desc.capitalize()
        if forn: desc += f" — {forn}"
    elif cat == "mao":
        funcs = [f.capitalize() for f in TARIFAS if f in t]
        desc  = " + ".join(funcs) if funcs else "Mão de Obra"
        md = re.search(r'(\d+)\s*dias?', t)
        if md: desc += f" — {md.group(1)} dia(s)"
    elif cat == "imposto":
        desc = "Imposto / Nota Fiscal"
    else:
        desc = texto[:50] + ("..." if len(texto) > 50 else "")

    return cat, valor, forn, desc, extra

# ─── HANDLERS ─────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    teclado = ReplyKeyboardMarkup(
        [[KeyboardButton("/resumo"), KeyboardButton("/obras")],
         [KeyboardButton("/nova_obra"), KeyboardButton("/relatorio")]],
        resize_keyboard=True
    )
    await update.message.reply_text(
        "🏗️ *ObraControl Bot* — Bem-vindo!\n\n"
        "Me mande os gastos da obra em linguagem natural:\n\n"
        "• _Alumínio BPA por R$ 40.000_\n"
        "• _Vidros Divinal 10 mil_\n"
        "• _Serralheiro e ajudante 5 dias_\n"
        "• _Imposto NF R$ 3.500_\n\n"
        "Comandos:\n"
        "/nova\\_obra Nome — 100000 → criar obra\n"
        "/trocar Nome → mudar obra ativa\n"
        "/resumo → ver custos e lucro\n"
        "/obras → listar todas as obras\n"
        "/relatorio → receber CSV\n"
        "/apagar\\_ultimo → remover último lançamento",
        parse_mode="Markdown",
        reply_markup=teclado
    )

async def nova_obra(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    dados = carregar()
    user  = get_user(dados, uid)
    texto = " ".join(ctx.args) if ctx.args else ""

    # Formato: /nova_obra Nome da Obra — 100000
    partes = re.split(r'\s*[-—–]\s*', texto, 1)
    if len(partes) < 2:
        await update.message.reply_text(
            "Formato: /nova\\_obra *Nome da Obra — Valor*\n\nExemplo:\n`/nova_obra Residência João — 100000`",
            parse_mode="Markdown"
        )
        return

    nome  = partes[0].strip()
    valor = extrair_valor(partes[1])
    if not nome or valor <= 0:
        await update.message.reply_text("Nome ou valor inválido. Tente novamente.")
        return

    oid = re.sub(r'\s+', '_', nome.lower())[:20]
    user["obras"][oid] = {"nome": nome, "valor": valor, "lancamentos": []}
    user["obra_atual"] = oid
    salvar(dados)

    await update.message.reply_text(
        f"✅ Obra *{nome}* criada!\n"
        f"💰 Contrato: *{fmt(valor)}*\n\n"
        f"Agora me mande os gastos!",
        parse_mode="Markdown"
    )

async def trocar_obra(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    dados = carregar()
    user  = get_user(dados, uid)
    busca = " ".join(ctx.args).lower().strip() if ctx.args else ""

    if not busca:
        await listar_obras(update, ctx)
        return

    for oid, obra in user["obras"].items():
        if busca in obra["nome"].lower() or busca == oid:
            user["obra_atual"] = oid
            salvar(dados)
            await update.message.reply_text(
                f"✅ Obra ativa: *{obra['nome']}*\n💰 Contrato: {fmt(obra['valor'])}",
                parse_mode="Markdown"
            )
            return

    await update.message.reply_text("Obra não encontrada. Use /obras para ver a lista.")

async def listar_obras(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    dados = carregar()
    user  = get_user(dados, uid)

    if not user["obras"]:
        await update.message.reply_text("Nenhuma obra cadastrada.\nUse: /nova\\_obra Nome — Valor", parse_mode="Markdown")
        return

    txt = "📋 *Suas obras:*\n\n"
    for oid, obra in user["obras"].items():
        ativo = " ← ativa" if oid == user["obra_atual"] else ""
        total = sum(l["valor"] for l in obra["lancamentos"])
        lucro = obra["valor"] - total
        txt += f"🏗️ *{obra['nome']}*{ativo}\n"
        txt += f"   Contrato: {fmt(obra['valor'])} | Gasto: {fmt(total)} | Lucro: {fmt(lucro)}\n"
        txt += f"   Para ativar: `/trocar {obra['nome']}`\n\n"

    await update.message.reply_text(txt, parse_mode="Markdown")

async def resumo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    dados = carregar()
    user  = get_user(dados, uid)
    oid   = user.get("obra_atual")

    if not oid or oid not in user["obras"]:
        await update.message.reply_text("Nenhuma obra ativa. Use /obras para selecionar.")
        return

    obra = user["obras"][oid]
    lans = obra["lancamentos"]
    total = sum(l["valor"] for l in lans)
    mat   = sum(l["valor"] for l in lans if l["cat"] == "material")
    mao   = sum(l["valor"] for l in lans if l["cat"] == "mao")
    imp   = sum(l["valor"] for l in lans if l["cat"] == "imposto")
    out   = sum(l["valor"] for l in lans if l["cat"] == "outros")
    lucro = obra["valor"] - total
    pct   = (total / obra["valor"] * 100) if obra["valor"] > 0 else 0
    mgm   = (lucro / obra["valor"] * 100) if obra["valor"] > 0 else 0

    # Barra de progresso visual
    blocos = int(pct / 10)
    barra  = "█" * blocos + "░" * (10 - blocos)

    txt  = f"📊 *{obra['nome']}*\n"
    txt += f"{'─'*30}\n"
    txt += f"💼 Contrato fechado: *{fmt(obra['valor'])}*\n\n"
    txt += f"*Gastos por categoria:*\n"
    txt += f"🔩 Materiais:    {fmt(mat)}\n"
    txt += f"👷 Mão de Obra:  {fmt(mao)}\n"
    txt += f"🧾 Impostos:     {fmt(imp)}\n"
    txt += f"📦 Outros:       {fmt(out)}\n"
    txt += f"{'─'*30}\n"
    txt += f"💸 Total gasto:  *{fmt(total)}* ({pct:.1f}%)\n"
    txt += f"[{barra}] {pct:.0f}%\n\n"
    txt += f"{'✅' if lucro >= 0 else '🔴'} Lucro estimado: *{fmt(lucro)}*\n"
    txt += f"📈 Margem: *{mgm:.1f}%*\n\n"
    txt += f"_Últimos lançamentos:_\n"

    for l in reversed(lans[-5:]):
        txt += f"• {l['data']} — {l['desc']} — {fmt(l['valor'])}\n"

    await update.message.reply_text(txt, parse_mode="Markdown")

async def apagar_ultimo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    dados = carregar()
    user  = get_user(dados, uid)
    oid   = user.get("obra_atual")

    if not oid or oid not in user["obras"]:
        await update.message.reply_text("Nenhuma obra ativa.")
        return

    lans = user["obras"][oid]["lancamentos"]
    if not lans:
        await update.message.reply_text("Não há lançamentos para remover.")
        return

    ultimo = lans.pop()
    salvar(dados)
    await update.message.reply_text(
        f"🗑️ Removido: *{ultimo['desc']}* — {fmt(ultimo['valor'])}",
        parse_mode="Markdown"
    )

async def relatorio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    dados = carregar()
    user  = get_user(dados, uid)
    oid   = user.get("obra_atual")

    if not oid or oid not in user["obras"]:
        await update.message.reply_text("Nenhuma obra ativa.")
        return

    obra  = user["obras"][oid]
    lans  = obra["lancamentos"]
    total = sum(l["valor"] for l in lans)
    lucro = obra["valor"] - total

    linhas = ["Data,Descricao,Fornecedor,Categoria,Valor"]
    labels = {"material":"Material","mao":"Mao de Obra","imposto":"Imposto","outros":"Outros"}
    for l in lans:
        linhas.append(f"{l['data']},\"{l['desc']}\",\"{l.get('forn','')}\",{labels.get(l['cat'],l['cat'])},{l['valor']}")
    linhas.append("")
    linhas.append(f",,Contrato,,{obra['valor']}")
    linhas.append(f",,Total Gasto,,{total}")
    linhas.append(f",,Lucro Estimado,,{lucro}")
    linhas.append(f",,Margem,,{(lucro/obra['valor']*100):.1f}%" if obra['valor']>0 else ",,Margem,,0%")

    nome_arquivo = obra["nome"].replace(" ","_") + "_custos.csv"
    with open(nome_arquivo, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(linhas))

    await update.message.reply_document(
        document=open(nome_arquivo, "rb"),
        filename=nome_arquivo,
        caption=f"📊 Relatório: *{obra['nome']}*\nContrato: {fmt(obra['valor'])} | Gasto: {fmt(total)} | Lucro: {fmt(lucro)}",
        parse_mode="Markdown"
    )

async def receber_mensagem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    dados = carregar()
    user  = get_user(dados, uid)
    oid   = user.get("obra_atual")
    texto = update.message.text

    if not oid or oid not in user["obras"]:
        await update.message.reply_text(
            "⚠️ Nenhuma obra ativa!\n\n"
            "Crie uma obra primeiro:\n"
            "`/nova_obra Nome da Obra — 100000`",
            parse_mode="Markdown"
        )
        return

    cat, valor, forn, desc, extra = processar_mensagem(texto)

    if cat is None:
        await update.message.reply_text(extra, parse_mode="Markdown")
        return

    obra = user["obras"][oid]
    obra["lancamentos"].append({
        "data": hoje(), "desc": desc, "forn": forn,
        "cat": cat, "valor": valor
    })
    salvar(dados)

    total = sum(l["valor"] for l in obra["lancamentos"])
    lucro = obra["valor"] - total
    pct   = (total / obra["valor"] * 100) if obra["valor"] > 0 else 0
    mgm   = (lucro / obra["valor"] * 100) if obra["valor"] > 0 else 0
    labels = {"material":"🔩 Material","mao":"👷 Mão de Obra","imposto":"🧾 Imposto","outros":"📦 Outros"}

    txt  = f"✅ *{labels.get(cat,'📦')}* lançado!{extra}\n\n"
    txt += f"📌 {desc}"
    if forn: txt += f" — _{forn}_"
    txt += f"\n💰 Valor: *{fmt(valor)}*\n\n"
    txt += f"📊 *Resumo — {obra['nome']}:*\n"
    txt += f"Total gasto: {fmt(total)} ({pct:.1f}%)\n"
    txt += f"{'✅' if lucro>=0 else '🔴'} Lucro: *{fmt(lucro)}* (margem {mgm:.1f}%)"

    await update.message.reply_text(txt, parse_mode="Markdown")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    if not TOKEN:
        print("ERRO: variável BOT_TOKEN não definida!")
        return
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",         start))
    app.add_handler(CommandHandler("nova_obra",     nova_obra))
    app.add_handler(CommandHandler("trocar",        trocar_obra))
    app.add_handler(CommandHandler("obras",         listar_obras))
    app.add_handler(CommandHandler("resumo",        resumo))
    app.add_handler(CommandHandler("relatorio",     relatorio))
    app.add_handler(CommandHandler("apagar_ultimo", apagar_ultimo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receber_mensagem))
    print("Bot rodando...")
    app.run_polling()

if __name__ == "__main__":
    main()
