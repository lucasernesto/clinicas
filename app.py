"""Sistema da clínica multiprofissional: agenda, pacientes, prontuário e financeiro."""

import os
import re
import secrets
import sqlite3
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from urllib.parse import quote

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

BASE = Path(__file__).resolve().parent
INSTANCE = BASE / "instance"
DB_PATH = INSTANCE / "clinica.db"

NOME_CLINICA = os.environ.get("CLINICA_NOME", "Clínica")
COBRAR_FALTA = os.environ.get("CLINICA_COBRAR_FALTA", "1") == "1"
WHATSAPP_CLINICA = os.environ.get("CLINICA_WHATSAPP", "")
# Agendamento online: antecedência mínima e até quantos dias à frente se pode marcar.
ANTECEDENCIA_HORAS = int(os.environ.get("CLINICA_ANTECEDENCIA_HORAS", "12"))
DIAS_ONLINE = int(os.environ.get("CLINICA_DIAS_ONLINE", "21"))

PAPEIS = {"admin": "Administração", "profissional": "Profissional de saúde", "recepcao": "Recepção"}
# As chaves ficam como estão para não mexer em dados antigos; os rótulos servem a todas as áreas.
TIPOS = {
    "terapia": "Sessão",
    "avaliacao": "Avaliação / 1ª consulta",
    "reavaliacao": "Reavaliação / retorno",
    "devolutiva": "Devolutiva",
    "orientacao": "Orientação familiar",
}
ESPECIALIDADES_INICIAIS = ["Fonoaudiologia", "Psicologia", "Psicomotricidade", "Nutrição", "Musicoterapia"]
STATUS = {
    "agendado": "Agendado",
    "confirmado": "Confirmado",
    "realizado": "Realizado",
    "falta": "Falta",
    "falta_justificada": "Falta justificada",
    "cancelado": "Cancelado",
}
# Status que liberam o horário para outro agendamento.
LIBERAM_HORARIO = ("cancelado", "falta_justificada")
COBRAVEIS = ("realizado", "falta") if COBRAR_FALTA else ("realizado",)
COBRAVEIS_SQL = ",".join(f"'{s}'" for s in COBRAVEIS)
# Sessões cobradas do paciente: as particulares. As do plano são faturadas para o convênio.
def cobra_paciente_sql(alias="a"):
    return f"{alias}.status IN ({COBRAVEIS_SQL}) AND {alias}.convenio_id IS NULL"


# Produção do mês: particulares cobráveis + sessões de plano realizadas e não glosadas.
PRODUZIDO_SQL = f"""CASE
    WHEN a.convenio_id IS NULL AND a.status IN ({COBRAVEIS_SQL}) THEN a.valor_centavos
    WHEN a.convenio_id IS NOT NULL AND a.status = 'realizado' AND COALESCE(a.faturamento, '') != 'glosado'
        THEN a.valor_centavos
    ELSE 0 END"""
# Formas de pagamento do paciente (o que o plano paga fica no faturamento de convênios).
FORMAS = {
    "pix": "Pix",
    "dinheiro": "Dinheiro",
    "debito": "Cartão de débito",
    "credito": "Cartão de crédito",
    "transferencia": "Transferência",
}
ROTULO_FORMAS = {**FORMAS, "convenio": "Convênio"}  # inclui formas antigas, só para exibir
FATURAMENTO = {
    "": "A faturar",
    "faturado": "Faturado",
    "pago": "Pago",
    "glosado": "Glosado",
}
CONVENIOS_INICIAIS = ["Assim", "Klini", "Appai"]
DURACOES = [30, 40, 45, 50, 60, 90]
CORES = ["#2f7d6d", "#3b6fb6", "#a4508b", "#c0672c", "#6b8e23", "#7a5bb5", "#b8434b", "#2c8ca3"]
STATUS_SOLICITACAO = {
    "pendente": "Aguardando confirmação",
    "aprovada": "Confirmada",
    "recusada": "Não confirmada",
    "cancelada": "Cancelada",
}
# Solicitações pendentes por telefone / por IP nas últimas 24 h, contra abuso do formulário público.
LIMITE_SOLIC_TELEFONE = 2
LIMITE_SOLIC_IP = 5
DIAS_SEMANA = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
MAX_SEMANAS_REPETICAO = 52
TENTATIVAS_LOGIN = 5
MINUTOS_BLOQUEIO = 15
SENHA_MINIMA = 8


def carregar_secret_key():
    INSTANCE.mkdir(exist_ok=True)
    arquivo = INSTANCE / "secret_key"
    if not arquivo.exists():
        arquivo.write_text(secrets.token_hex(32), encoding="utf-8")
    return arquivo.read_text(encoding="utf-8").strip()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("CLINICA_SECRET_KEY") or carregar_secret_key(),
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)


# ---------------------------------------------------------------- banco de dados

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def fechar_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Colunas acrescentadas depois da primeira versão: (tabela, coluna, definição).
MIGRACOES = [
    ("usuarios", "agenda_online", "INTEGER NOT NULL DEFAULT 0"),
    ("usuarios", "duracao_online", "INTEGER NOT NULL DEFAULT 45"),
    ("agendamentos", "especialidade_id", "INTEGER REFERENCES especialidades (id)"),
    ("evolucoes", "especialidade_id", "INTEGER REFERENCES especialidades (id)"),
    ("evolucoes", "sigilosa", "INTEGER NOT NULL DEFAULT 0"),
    ("solicitacoes", "especialidade_id", "INTEGER REFERENCES especialidades (id)"),
    ("pacientes", "convenio_id", "INTEGER REFERENCES convenios (id)"),
    ("agendamentos", "convenio_id", "INTEGER REFERENCES convenios (id)"),
    ("agendamentos", "guia", "TEXT"),
    ("agendamentos", "faturamento", "TEXT"),
    ("solicitacoes", "convenio_id", "INTEGER REFERENCES convenios (id)"),
    ("solicitacoes", "carteirinha", "TEXT"),
]


def migrar(db):
    for tabela, coluna, definicao in MIGRACOES:
        existentes = {r[1] for r in db.execute(f"PRAGMA table_info({tabela})")}
        if existentes and coluna not in existentes:  # tabela vazia de colunas = ainda não existe
            db.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}")


def init_db():
    INSTANCE.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    migrar(db)
    db.executescript((BASE / "schema.sql").read_text(encoding="utf-8"))
    if db.execute("SELECT COUNT(*) FROM especialidades").fetchone()[0] == 0:
        db.executemany("INSERT INTO especialidades (nome) VALUES (?)", [(n,) for n in ESPECIALIDADES_INICIAIS])
    if db.execute("SELECT COUNT(*) FROM convenios").fetchone()[0] == 0:
        db.executemany("INSERT INTO convenios (nome) VALUES (?)", [(n,) for n in CONVENIOS_INICIAIS])
        # Começa cobrindo todas as especialidades, com valor a definir na tela de Convênios.
        db.execute("INSERT INTO convenio_especialidades (convenio_id, especialidade_id) "
                   "SELECT c.id, e.id FROM convenios c, especialidades e")
        # Bancos antigos guardavam o convênio do paciente como texto livre.
        if "convenio" in {r[1] for r in db.execute("PRAGMA table_info(pacientes)")}:
            db.execute("UPDATE pacientes SET convenio_id = (SELECT c.id FROM convenios c "
                       "WHERE c.nome = trim(pacientes.convenio)) WHERE convenio_id IS NULL")
    if db.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
        email = os.environ.get("CLINICA_ADMIN_EMAIL", "admin@clinica.local")
        senha = os.environ.get("CLINICA_ADMIN_SENHA", "admin123")
        db.execute(
            "INSERT INTO usuarios (nome, email, senha_hash, papel, trocar_senha) "
            "VALUES ('Administração', ?, ?, 'admin', 1)",
            (email, generate_password_hash(senha)),
        )
        print(f"Usuário inicial criado: {email}. A senha deverá ser trocada no primeiro acesso.")
    db.commit()
    db.close()


# ---------------------------------------------------------------- datas e formatação

FMT_DATAHORA = "%Y-%m-%d %H:%M"


def ler_data(texto):
    try:
        return date.fromisoformat((texto or "").strip())
    except ValueError:
        return None


def ler_hora(texto):
    try:
        return datetime.strptime((texto or "").strip(), "%H:%M").time()
    except ValueError:
        return None


def agora_txt():
    return datetime.now().strftime(FMT_DATAHORA)


@app.template_filter("brl")
def formatar_brl(centavos):
    valor = f"{(centavos or 0) / 100:,.2f}"
    return "R$ " + valor.replace(",", "_").replace(".", ",").replace("_", ".")


@app.template_filter("data_br")
def formatar_data(texto):
    # "2026-10-10" ou "2026-10-10 14:32" -> "10/10/2026"
    if not texto:
        return ""
    ano, mes, dia = texto[:10].split("-")
    return f"{dia}/{mes}/{ano}"


@app.template_filter("datahora_br")
def formatar_datahora(texto):
    if not texto:
        return ""
    return f"{formatar_data(texto)} {texto[11:16]}"


@app.template_filter("hora")
def formatar_hora(texto):
    return texto[11:16] if texto else ""


@app.template_filter("dia_semana")
def formatar_dia_semana(texto):
    return DIAS_SEMANA[date.fromisoformat(texto[:10]).weekday()]


@app.template_filter("idade")
def calcular_idade(nascimento):
    nasc = ler_data(nascimento)
    if nasc is None:
        return ""
    hoje = date.today()
    meses = (hoje.year - nasc.year) * 12 + hoje.month - nasc.month - (hoje.day < nasc.day)
    anos, resto = divmod(max(meses, 0), 12)
    if anos == 0:
        return f"{resto} {'mês' if resto == 1 else 'meses'}"
    texto = f"{anos} {'ano' if anos == 1 else 'anos'}"
    # Em crianças pequenas os meses importam para o desenvolvimento da fala.
    if anos < 12 and resto:
        texto += f" e {resto} {'mês' if resto == 1 else 'meses'}"
    return texto


@app.template_filter("centavos_form")
def centavos_para_form(centavos):
    return f"{(centavos or 0) / 100:.2f}".replace(".", ",")


def ler_preco(texto):
    """Aceita "120,00", "120.00", "1.234,56" ou "R$ 120"; devolve centavos ou None."""
    texto = (texto or "").replace("R$", "").strip()
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        valor = Decimal(texto)
    except InvalidOperation:
        return None
    if valor < 0 or valor >= 1_000_000:
        return None
    return int((valor * 100).quantize(Decimal("1")))


def so_digitos(texto):
    return re.sub(r"\D", "", texto or "")


def link_whatsapp(telefone, mensagem=""):
    numero = so_digitos(telefone)
    if len(numero) in (10, 11):
        numero = "55" + numero
    if len(numero) < 12:
        return None
    return f"https://wa.me/{numero}" + (f"?text={quote(mensagem)}" if mensagem else "")


def link_confirmacao(ag):
    """Link de WhatsApp com a mensagem de confirmação da sessão, para o responsável ou paciente."""
    telefone = ag["responsavel_telefone"] or ag["paciente_telefone"]
    primeiro_nome = ag["paciente_nome"].split()[0]
    dia = formatar_dia_semana(ag["inicio"]).lower()
    area = f" de {ag['especialidade_nome']}" if ag["especialidade_nome"] else ""
    mensagem = (
        f"Olá! Aqui é da {NOME_CLINICA}. Passando para confirmar o atendimento{area} de "
        f"{primeiro_nome} na {dia}, {formatar_data(ag['inicio'])[:5]}, às {ag['inicio'][11:16]}, "
        f"com {ag['profissional_nome']}. Podemos confirmar?"
    )
    return link_whatsapp(telefone, mensagem)


def destino_seguro(url, padrao):
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return padrao


# ---------------------------------------------------------------- sessão e permissões

def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


def pode_prontuario():
    return bool(g.get("usuario") and g.usuario["atende"])


def pode_financeiro_geral():
    return bool(g.get("usuario") and g.usuario["papel"] in ("admin", "recepcao"))


app.jinja_env.globals.update(
    csrf_token=csrf_token,
    pode_prontuario=pode_prontuario,
    pode_financeiro_geral=pode_financeiro_geral,
    link_confirmacao=link_confirmacao,
    link_whatsapp=link_whatsapp,
    listar_convenios=lambda: convenios(),
    NOME_CLINICA=NOME_CLINICA,
    PAPEIS=PAPEIS,
    TIPOS=TIPOS,
    STATUS=STATUS,
    FORMAS=FORMAS,
    ROTULO_FORMAS=ROTULO_FORMAS,
    FATURAMENTO=FATURAMENTO,
    DURACOES=DURACOES,
    CORES=CORES,
    COBRAR_FALTA=COBRAR_FALTA,
    STATUS_SOLICITACAO=STATUS_SOLICITACAO,
    DIAS_SEMANA=DIAS_SEMANA,
    WHATSAPP_CLINICA=WHATSAPP_CLINICA,
)


@app.context_processor
def contadores_menu():
    if not g.get("usuario"):
        return {}
    pendentes = get_db().execute(
        "SELECT COUNT(*) FROM solicitacoes WHERE status = 'pendente' AND inicio >= ?", (agora_txt(),)
    ).fetchone()[0]
    return {"solicitacoes_pendentes": pendentes}


@app.before_request
def verificar_csrf():
    if request.method == "POST":
        enviado = request.form.get("csrf_token", "")
        esperado = session.get("csrf_token", "")
        if not esperado or not secrets.compare_digest(enviado, esperado):
            abort(400, "Sessão expirada. Recarregue a página e tente novamente.")


def versao_senha(usuario):
    # Muda quando a senha muda, derrubando as sessões abertas com a senha antiga.
    return usuario["senha_hash"][-16:]


@app.before_request
def carregar_usuario():
    g.usuario = None
    if request.endpoint == "static":
        return
    uid = session.get("uid")
    if uid is None:
        return
    usuario = get_db().execute("SELECT * FROM usuarios WHERE id = ? AND ativo = 1", (uid,)).fetchone()
    if usuario is None or session.get("ver") != versao_senha(usuario):
        session.clear()
        return
    g.usuario = usuario


def requer(*papeis):
    """Exige login; se papéis forem informados, só eles entram."""

    def decorador(view):
        @wraps(view)
        def envolvida(*args, **kwargs):
            if g.usuario is None:
                return redirect(url_for("login", next=request.full_path.rstrip("?")))
            if g.usuario["trocar_senha"] and request.endpoint != "trocar_senha":
                return redirect(url_for("trocar_senha"))
            if papeis and g.usuario["papel"] not in papeis:
                abort(403)
            return view(*args, **kwargs)

        return envolvida

    return decorador


def exigir_prontuario():
    if not pode_prontuario():
        abort(403)


# ---------------------------------------------------------------- consultas comuns

SQL_AGENDA = """
    SELECT a.*, p.nome AS paciente_nome, p.telefone AS paciente_telefone,
           p.responsavel_nome, p.responsavel_telefone,
           u.nome AS profissional_nome, u.cor AS profissional_cor, es.nome AS especialidade_nome,
           cv.nome AS convenio_nome, p.carteirinha,
           (SELECT e.id FROM evolucoes e WHERE e.agendamento_id = a.id) AS evolucao_id
    FROM agendamentos a
    JOIN pacientes p ON p.id = a.paciente_id
    JOIN usuarios u ON u.id = a.profissional_id
    LEFT JOIN especialidades es ON es.id = a.especialidade_id
    LEFT JOIN convenios cv ON cv.id = a.convenio_id
"""


SQL_SOLICITACOES = """
    SELECT s.*, u.nome AS profissional_nome, u.cor AS profissional_cor, es.nome AS especialidade_nome,
           cv.nome AS convenio_nome
    FROM solicitacoes s JOIN usuarios u ON u.id = s.profissional_id
    LEFT JOIN especialidades es ON es.id = s.especialidade_id
    LEFT JOIN convenios cv ON cv.id = s.convenio_id
"""


def convenios(so_ativos=True):
    sql = "SELECT * FROM convenios" + (" WHERE ativo = 1" if so_ativos else "")
    return get_db().execute(sql + " ORDER BY nome").fetchall()


def cobertura(db, convenio_id, especialidade_id):
    """Linha de convenio_especialidades (com o valor pago) ou None se o plano não cobre a área."""
    return db.execute(
        "SELECT ce.*, c.nome AS convenio_nome FROM convenio_especialidades ce "
        "JOIN convenios c ON c.id = ce.convenio_id "
        "WHERE ce.convenio_id = ? AND ce.especialidade_id = ? AND c.ativo = 1",
        (convenio_id, especialidade_id),
    ).fetchone()


def convenios_que_cobrem(especialidade_id):
    return get_db().execute(
        "SELECT c.* FROM convenios c JOIN convenio_especialidades ce ON ce.convenio_id = c.id "
        "WHERE c.ativo = 1 AND ce.especialidade_id = ? ORDER BY c.nome",
        (especialidade_id,),
    ).fetchall()

SQL_ESPECIALIDADES_DO_USUARIO = """
    (SELECT group_concat(es.nome, ', ') FROM profissional_especialidades pe
     JOIN especialidades es ON es.id = pe.especialidade_id WHERE pe.usuario_id = usuarios.id)
"""


def profissionais(incluir_inativos=False):
    sql = f"SELECT usuarios.*, {SQL_ESPECIALIDADES_DO_USUARIO} AS especialidades_txt FROM usuarios WHERE atende = 1"
    if not incluir_inativos:
        sql += " AND ativo = 1"
    return get_db().execute(sql + " ORDER BY nome").fetchall()


def especialidades(so_ativas=True):
    sql = "SELECT * FROM especialidades" + (" WHERE ativo = 1" if so_ativas else "")
    return get_db().execute(sql + " ORDER BY nome").fetchall()


def atendimentos_possiveis():
    """Pares profissional + especialidade para os selects da agenda, agrupados por especialidade.

    Profissional sem especialidade cadastrada aparece em "Sem especialidade" (especialidade None).
    """
    linhas = get_db().execute(
        """SELECT u.id AS profissional_id, u.nome AS profissional_nome,
                  es.id AS especialidade_id, COALESCE(es.nome, 'Sem especialidade') AS especialidade_nome
           FROM usuarios u
           LEFT JOIN profissional_especialidades pe ON pe.usuario_id = u.id
           LEFT JOIN especialidades es ON es.id = pe.especialidade_id
           WHERE u.atende = 1 AND u.ativo = 1 AND (es.id IS NULL OR es.ativo = 1)
           ORDER BY es.id IS NULL, es.nome, u.nome"""
    ).fetchall()
    grupos = {}
    for linha in linhas:
        grupos.setdefault(linha["especialidade_nome"], []).append(linha)
    return grupos


def chave_atendimento(prof_id, esp_id):
    return f"{prof_id}:{esp_id or ''}"


def especialidade_padrao(prof_id):
    linha = get_db().execute(
        "SELECT pe.especialidade_id FROM profissional_especialidades pe "
        "JOIN especialidades es ON es.id = pe.especialidade_id "
        "WHERE pe.usuario_id = ? ORDER BY es.ativo DESC, es.nome LIMIT 1",
        (prof_id,),
    ).fetchone()
    return linha[0] if linha else None


def pacientes_ativos():
    return get_db().execute(
        "SELECT id, nome, valor_sessao_centavos, profissional_id FROM pacientes "
        "WHERE ativo = 1 ORDER BY nome"
    ).fetchall()


def filtro_profissional():
    """Lê ?prof= : número = um profissional, 'todos' = todos; sem parâmetro = o próprio, se atende."""
    valor = request.args.get("prof")
    if valor is None:
        return g.usuario["id"] if g.usuario["atende"] else None
    return int(valor) if valor.isdigit() else None


def agendamentos_entre(inicio, fim, prof_id=None):
    sql = SQL_AGENDA + " WHERE a.inicio >= ? AND a.inicio < ?"
    params = [inicio.isoformat(), fim.isoformat()]
    if prof_id:
        sql += " AND a.profissional_id = ?"
        params.append(prof_id)
    return get_db().execute(sql + " ORDER BY a.inicio, u.nome", params).fetchall()


def buscar_conflitos(db, prof_id, pac_id, inicio, fim, ignorar_id=None):
    marcadores = ",".join("?" * len(LIBERAM_HORARIO))
    return db.execute(
        SQL_AGENDA
        + f""" WHERE a.status NOT IN ({marcadores}) AND a.inicio < ? AND a.fim > ?
              AND (a.profissional_id = ? OR a.paciente_id = ?) AND a.id != ?""",
        (*LIBERAM_HORARIO, fim, inicio, prof_id, pac_id, ignorar_id or 0),
    ).fetchall()


def descrever_conflito(c, prof_id):
    quem = (
        f"{c['profissional_nome']} já atende {c['paciente_nome']}"
        if c["profissional_id"] == prof_id
        else f"{c['paciente_nome']} já tem sessão com {c['profissional_nome']}"
    )
    return f"{formatar_data(c['inicio'])} {c['inicio'][11:16]}: {quem} nesse horário."


def conflito(db, prof_id, pac_id, inicio, fim, ignorar_id=None):
    """Descreve o primeiro choque de horário (sessão ou solicitação online pendente), ou None."""
    conflitos = buscar_conflitos(db, prof_id, pac_id, inicio, fim, ignorar_id)
    if conflitos:
        return descrever_conflito(conflitos[0], prof_id)
    pedido = db.execute(
        "SELECT inicio, paciente_nome FROM solicitacoes WHERE status = 'pendente' "
        "AND profissional_id = ? AND inicio < ? AND fim > ?",
        (prof_id, fim, inicio),
    ).fetchone()
    if pedido:
        return (f"{formatar_data(pedido['inicio'])} {pedido['inicio'][11:16]}: há uma solicitação online "
                f"pendente de {pedido['paciente_nome']} nesse horário. Responda em Solicitações.")
    return None


def carregar_paciente(paciente_id):
    paciente = get_db().execute(
        "SELECT p.*, u.nome AS profissional_nome, cv.nome AS convenio_nome FROM pacientes p "
        "LEFT JOIN usuarios u ON u.id = p.profissional_id "
        "LEFT JOIN convenios cv ON cv.id = p.convenio_id WHERE p.id = ?",
        (paciente_id,),
    ).fetchone()
    if paciente is None:
        abort(404)
    return paciente


def saldos_pacientes(where="1 = 1", params=()):
    return get_db().execute(
        f"""SELECT * FROM (
                SELECT p.id, p.nome, cv.nome AS convenio_nome, p.responsavel_nome, p.telefone,
                  p.responsavel_telefone,
                  COALESCE((SELECT SUM(a.valor_centavos) FROM agendamentos a
                            WHERE a.paciente_id = p.id AND {cobra_paciente_sql()}), 0) AS cobrado,
                  COALESCE((SELECT SUM(x.valor_centavos) FROM pagamentos x
                            WHERE x.paciente_id = p.id), 0) AS pago
                FROM pacientes p LEFT JOIN convenios cv ON cv.id = p.convenio_id WHERE {where})
            ORDER BY cobrado - pago DESC, nome""",
        params,
    ).fetchall()


# ---------------------------------------------------------------- login

HASH_FALSO = generate_password_hash(secrets.token_hex(8))


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.usuario:
        return redirect(url_for("hoje"))
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        senha = request.form.get("senha", "")
        db = get_db()
        usuario = db.execute("SELECT * FROM usuarios WHERE email = ?", (email,)).fetchone()
        agora = agora_txt()

        if usuario and usuario["bloqueado_ate"] and usuario["bloqueado_ate"] > agora:
            flash("Muitas tentativas erradas. Aguarde alguns minutos e tente de novo.", "erro")
            return render_template("login.html", email=email), 429

        if usuario and usuario["ativo"] and check_password_hash(usuario["senha_hash"], senha):
            db.execute("UPDATE usuarios SET falhas_login = 0, bloqueado_ate = NULL WHERE id = ?",
                       (usuario["id"],))
            db.commit()
            session.clear()
            session.permanent = True
            session["uid"] = usuario["id"]
            session["ver"] = versao_senha(usuario)
            return redirect(destino_seguro(request.args.get("next"), url_for("hoje")))

        if usuario:
            falhas = usuario["falhas_login"] + 1
            bloqueio = None
            if falhas >= TENTATIVAS_LOGIN:
                bloqueio = (datetime.now() + timedelta(minutes=MINUTOS_BLOQUEIO)).strftime(FMT_DATAHORA)
                falhas = 0
            db.execute("UPDATE usuarios SET falhas_login = ?, bloqueado_ate = ? WHERE id = ?",
                       (falhas, bloqueio, usuario["id"]))
            db.commit()
        else:
            check_password_hash(HASH_FALSO, senha)  # mesmo tempo de resposta para e-mail inexistente
        flash("E-mail ou senha incorretos.", "erro")
        return render_template("login.html", email=email), 401
    return render_template("login.html", email="")


@app.post("/sair")
def sair():
    session.clear()
    return redirect(url_for("login"))


@app.route("/senha", methods=["GET", "POST"])
@requer()
def trocar_senha():
    if request.method == "POST":
        atual = request.form.get("atual", "")
        nova = request.form.get("nova", "")
        erros = []
        if not check_password_hash(g.usuario["senha_hash"], atual):
            erros.append("A senha atual não confere.")
        erros += validar_senha(nova, request.form.get("confirmacao", ""))
        if not erros and check_password_hash(g.usuario["senha_hash"], nova):
            erros.append("A nova senha precisa ser diferente da atual.")
        if not erros:
            novo_hash = generate_password_hash(nova)
            db = get_db()
            db.execute("UPDATE usuarios SET senha_hash = ?, trocar_senha = 0 WHERE id = ?",
                       (novo_hash, g.usuario["id"]))
            db.commit()
            session["ver"] = novo_hash[-16:]
            flash("Senha alterada.", "sucesso")
            return redirect(url_for("hoje"))
        for erro in erros:
            flash(erro, "erro")
        return render_template("senha.html"), 400
    return render_template("senha.html")


def validar_senha(nova, confirmacao):
    erros = []
    if len(nova) < SENHA_MINIMA:
        erros.append(f"A senha precisa ter pelo menos {SENHA_MINIMA} caracteres.")
    if nova != confirmacao:
        erros.append("A confirmação não é igual à nova senha.")
    return erros


# ---------------------------------------------------------------- hoje

@app.get("/")
@requer()
def hoje():
    dia = date.today()
    prof_id = filtro_profissional()
    db = get_db()
    atendimentos = agendamentos_entre(dia, dia + timedelta(days=1), prof_id)

    amanha = dia + timedelta(days=1)
    if amanha.weekday() == 6:  # domingo: confirmar os de segunda
        amanha += timedelta(days=1)
    a_confirmar = [
        a for a in agendamentos_entre(amanha, amanha + timedelta(days=1), prof_id)
        if a["status"] == "agendado"
    ]

    pendentes = []
    if g.usuario["atende"]:
        pendentes = db.execute(
            SQL_AGENDA + """ WHERE a.profissional_id = ? AND a.status = 'realizado'
                AND NOT EXISTS (SELECT 1 FROM evolucoes e WHERE e.agendamento_id = a.id)
                AND a.inicio >= ? ORDER BY a.inicio""",
            (g.usuario["id"], (dia - timedelta(days=60)).isoformat()),
        ).fetchall()

    # Sessões que já passaram e ainda estão como agendadas/confirmadas.
    sem_desfecho = db.execute(
        SQL_AGENDA + " WHERE a.status IN ('agendado', 'confirmado') AND a.fim < ?"
        + (" AND a.profissional_id = ?" if prof_id else "")
        + " ORDER BY a.inicio DESC LIMIT 30",
        (dia.isoformat(), prof_id) if prof_id else (dia.isoformat(),),
    ).fetchall()

    solicitacoes = db.execute(
        SQL_SOLICITACOES + " WHERE s.status = 'pendente' AND s.inicio >= ? ORDER BY s.inicio LIMIT 10",
        (agora_txt(),),
    ).fetchall()

    return render_template(
        "hoje.html", solicitacoes=solicitacoes,
        dia=dia.isoformat(), amanha=amanha.isoformat(), atendimentos=atendimentos,
        a_confirmar=a_confirmar, pendentes=pendentes, sem_desfecho=sem_desfecho,
        prof_id=prof_id, profissionais=profissionais(),
    )


# ---------------------------------------------------------------- agenda

@app.get("/agenda")
@requer()
def agenda():
    referencia = ler_data(request.args.get("semana")) or date.today()
    segunda = referencia - timedelta(days=referencia.weekday())
    prof_id = filtro_profissional()
    itens = agendamentos_entre(segunda, segunda + timedelta(days=7), prof_id)
    mostrar_cancelados = request.args.get("cancelados") == "1"
    if not mostrar_cancelados:
        itens = [a for a in itens if a["status"] != "cancelado"]

    sql = SQL_SOLICITACOES + " WHERE s.status = 'pendente' AND s.inicio >= ? AND s.inicio < ?"
    params = [segunda.isoformat(), (segunda + timedelta(days=7)).isoformat()]
    if prof_id:
        sql += " AND s.profissional_id = ?"
        params.append(prof_id)
    pedidos = get_db().execute(sql, params).fetchall()

    dias = []
    for i in range(7):
        d = segunda + timedelta(days=i)
        do_dia = [a for a in itens if a["inicio"][:10] == d.isoformat()]
        pedidos_dia = [s for s in pedidos if s["inicio"][:10] == d.isoformat()]
        if i < 6 or do_dia or pedidos_dia:  # domingo só aparece se houver algo marcado
            dias.append({"data": d.isoformat(), "nome": DIAS_SEMANA[i], "itens": do_dia,
                         "pedidos": pedidos_dia})

    return render_template(
        "agenda.html", dias=dias, segunda=segunda,
        anterior=(segunda - timedelta(days=7)).isoformat(),
        proxima=(segunda + timedelta(days=7)).isoformat(),
        hoje=date.today().isoformat(), prof_id=prof_id, profissionais=profissionais(),
        mostrar_cancelados=mostrar_cancelados,
    )


def validar_agendamento(f, db, paciente_fixo=None):
    erros = []
    paciente_id = paciente_fixo or (int(f["paciente_id"]) if f.get("paciente_id", "").isdigit() else None)
    paciente = None
    if paciente_id:
        paciente = db.execute("SELECT * FROM pacientes WHERE id = ?", (paciente_id,)).fetchone()
    if paciente is None:
        erros.append("Escolha o paciente.")

    # "atendimento" = "<profissional>:<especialidade>", vindo do select agrupado por especialidade.
    validos = {chave_atendimento(a["profissional_id"], a["especialidade_id"]): a
               for grupo in atendimentos_possiveis().values() for a in grupo}
    escolhido = validos.get(f.get("atendimento", ""))
    prof_id = escolhido["profissional_id"] if escolhido else None
    esp_id = escolhido["especialidade_id"] if escolhido else None
    if escolhido is None:
        erros.append("Escolha o profissional e a especialidade.")

    dia = ler_data(f.get("data"))
    hora = ler_hora(f.get("hora"))
    if dia is None or hora is None:
        erros.append("Informe data e horário.")
    try:
        duracao = int(f.get("duracao", ""))
        if not 10 <= duracao <= 240:
            raise ValueError
    except ValueError:
        erros.append("Duração inválida.")
        duracao = 0

    tipo = f.get("tipo", "")
    if tipo not in TIPOS:
        erros.append("Escolha o tipo de atendimento.")

    # "cobranca": "" = conforme o cadastro do paciente, "particular" ou o id do plano.
    cobranca = f.get("cobranca", "")
    convenio_id = None
    if cobranca == "":
        convenio_id = paciente["convenio_id"] if paciente else None
    elif cobranca.isdigit():
        convenio_id = int(cobranca)
    elif cobranca != "particular":
        erros.append("Forma de cobrança inválida.")
    plano = None
    if convenio_id and escolhido:
        plano = cobertura(db, convenio_id, esp_id)
        if plano is None:
            nome = db.execute("SELECT nome FROM convenios WHERE id = ?", (convenio_id,)).fetchone()
            erros.append(f"O plano {nome[0] if nome else ''} não cobre {escolhido['especialidade_nome']}. "
                         "Escolha particular ou ajuste a cobertura em Convênios.")

    texto_valor = f.get("valor", "").strip()
    if texto_valor:
        valor = ler_preco(texto_valor)
        if valor is None:
            erros.append("Valor inválido. Use o formato 120,00.")
    elif plano:
        valor = plano["valor_centavos"]
    else:
        valor = paciente["valor_sessao_centavos"] if paciente else 0

    guia = f.get("guia", "").strip()
    if len(guia) > 40:
        erros.append("Número da guia muito longo (até 40 caracteres).")

    observacao = f.get("observacao", "").strip()
    if len(observacao) > 500:
        erros.append("Observação muito longa (até 500 caracteres).")

    inicio = datetime.combine(dia, hora) if dia and hora else None
    return {
        "paciente_id": paciente_id,
        "profissional_id": prof_id,
        "especialidade_id": esp_id,
        "convenio_id": convenio_id,
        "guia": (guia or None) if convenio_id else None,
        "inicio": inicio,
        "duracao": duracao,
        "tipo": tipo,
        "valor_centavos": valor,
        "observacao": observacao or None,
    }, erros


@app.route("/agenda/novo", methods=["GET", "POST"])
@requer()
def agendamento_novo():
    db = get_db()
    if request.method == "POST":
        f = request.form
        dados, erros = validar_agendamento(f, db)
        try:
            semanas = int(f.get("semanas", "1") or 1)
            if not 1 <= semanas <= MAX_SEMANAS_REPETICAO:
                raise ValueError
        except ValueError:
            erros.append(f"Repetição deve ser de 1 a {MAX_SEMANAS_REPETICAO} semanas.")
            semanas = 1

        if not erros:
            serie = secrets.token_hex(8) if semanas > 1 else None
            criados, pulados = [], []
            for i in range(semanas):
                ini = dados["inicio"] + timedelta(weeks=i)
                fim = ini + timedelta(minutes=dados["duracao"])
                ini_txt, fim_txt = ini.strftime(FMT_DATAHORA), fim.strftime(FMT_DATAHORA)
                choque = conflito(db, dados["profissional_id"], dados["paciente_id"], ini_txt, fim_txt)
                if choque:
                    pulados.append(choque)
                    continue
                db.execute(
                    """INSERT INTO agendamentos (paciente_id, profissional_id, especialidade_id, inicio, fim,
                           tipo, valor_centavos, convenio_id, guia, observacao, serie, criado_por)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (dados["paciente_id"], dados["profissional_id"], dados["especialidade_id"], ini_txt,
                     fim_txt, dados["tipo"], dados["valor_centavos"], dados["convenio_id"], dados["guia"],
                     dados["observacao"], serie, g.usuario["id"]),
                )
                criados.append(ini_txt)

            if semanas == 1 and pulados:
                db.rollback()
                erros.append("Horário ocupado. " + pulados[0])
            else:
                db.commit()
                if criados:
                    plural = "sessões agendadas" if len(criados) > 1 else "sessão agendada"
                    flash(f"{len(criados)} {plural}.", "sucesso")
                for p in pulados:
                    flash("Não agendado — " + p, "erro")
                semana = dados["inicio"].date().isoformat()
                return redirect(url_for("agenda", semana=semana, prof=dados["profissional_id"]))

        for erro in erros:
            flash(erro, "erro")
        return render_template("agendamento_form.html", form=f, agendamento=None,
                               pacientes=pacientes_ativos(), atendimentos=atendimentos_possiveis()), 400

    args = request.args
    prof = args.get("prof") or (str(g.usuario["id"]) if g.usuario["atende"] else "")
    if args.get("paciente", "").isdigit() and not args.get("prof"):
        pac = db.execute("SELECT profissional_id FROM pacientes WHERE id = ?", (args["paciente"],)).fetchone()
        if pac and pac["profissional_id"]:
            prof = str(pac["profissional_id"])
    form = {
        "paciente_id": args.get("paciente", ""),
        "atendimento": chave_atendimento(prof, especialidade_padrao(prof)) if prof.isdigit() else "",
        "data": args.get("data") or date.today().isoformat(),
        "hora": args.get("hora", ""),
        "duracao": "45",
        "tipo": "terapia",
        "semanas": "1",
    }
    return render_template("agendamento_form.html", form=form, agendamento=None,
                           pacientes=pacientes_ativos(), atendimentos=atendimentos_possiveis())


def carregar_agendamento(agendamento_id):
    ag = get_db().execute(SQL_AGENDA + " WHERE a.id = ?", (agendamento_id,)).fetchone()
    if ag is None:
        abort(404)
    return ag


@app.route("/agenda/<int:agendamento_id>", methods=["GET", "POST"])
@requer()
def agendamento_editar(agendamento_id):
    db = get_db()
    ag = carregar_agendamento(agendamento_id)
    if request.method == "POST":
        f = request.form
        dados, erros = validar_agendamento(f, db, paciente_fixo=ag["paciente_id"])
        if not erros:
            ini_txt = dados["inicio"].strftime(FMT_DATAHORA)
            fim_txt = (dados["inicio"] + timedelta(minutes=dados["duracao"])).strftime(FMT_DATAHORA)
            if ag["status"] not in LIBERAM_HORARIO:
                choque = conflito(db, dados["profissional_id"], ag["paciente_id"],
                                  ini_txt, fim_txt, ignorar_id=agendamento_id)
                if choque:
                    erros.append("Horário ocupado. " + choque)
        if not erros:
            db.execute(
                """UPDATE agendamentos SET profissional_id = ?, especialidade_id = ?, inicio = ?, fim = ?,
                       tipo = ?, valor_centavos = ?, convenio_id = ?, guia = ?, observacao = ?,
                       faturamento = CASE WHEN ? IS NULL THEN NULL ELSE faturamento END,
                       atualizado_em = ? WHERE id = ?""",
                (dados["profissional_id"], dados["especialidade_id"], ini_txt, fim_txt, dados["tipo"],
                 dados["valor_centavos"], dados["convenio_id"], dados["guia"], dados["observacao"],
                 dados["convenio_id"], agora_txt(), agendamento_id),
            )
            db.commit()
            flash("Agendamento atualizado.", "sucesso")
            return redirect(destino_seguro(f.get("voltar"), url_for("agenda", semana=ini_txt[:10])))
        for erro in erros:
            flash(erro, "erro")
        return render_template("agendamento_form.html", form=f, agendamento=ag,
                               pacientes=pacientes_ativos(), atendimentos=atendimentos_possiveis()), 400

    inicio = datetime.strptime(ag["inicio"], FMT_DATAHORA)
    fim = datetime.strptime(ag["fim"], FMT_DATAHORA)
    form = {
        "paciente_id": str(ag["paciente_id"]),
        "atendimento": chave_atendimento(ag["profissional_id"], ag["especialidade_id"]),
        "data": ag["inicio"][:10],
        "hora": ag["inicio"][11:16],
        "duracao": str(int((fim - inicio).total_seconds() // 60)),
        "tipo": ag["tipo"],
        "cobranca": str(ag["convenio_id"]) if ag["convenio_id"] else "particular",
        "guia": ag["guia"] or "",
        "valor": centavos_para_form(ag["valor_centavos"]),
        "observacao": ag["observacao"] or "",
        "voltar": request.args.get("voltar", ""),
    }
    serie_futura = 0
    if ag["serie"]:
        serie_futura = db.execute(
            "SELECT COUNT(*) FROM agendamentos WHERE serie = ? AND inicio >= ? "
            "AND status IN ('agendado', 'confirmado')",
            (ag["serie"], ag["inicio"]),
        ).fetchone()[0]
    return render_template("agendamento_form.html", form=form, agendamento=ag,
                           pacientes=pacientes_ativos(), atendimentos=atendimentos_possiveis(),
                           serie_futura=serie_futura)


@app.post("/agenda/<int:agendamento_id>/status")
@requer()
def agendamento_status(agendamento_id):
    novo = request.form.get("status")
    if novo not in STATUS:
        abort(400)
    ag = carregar_agendamento(agendamento_id)
    voltar = redirect(destino_seguro(request.form.get("voltar"), url_for("agenda")))
    if novo == ag["status"]:
        return voltar
    db = get_db()
    if ag["status"] in LIBERAM_HORARIO and novo not in LIBERAM_HORARIO:
        # Reativar uma sessão cancelada: o horário pode ter sido ocupado nesse meio tempo.
        choque = conflito(db, ag["profissional_id"], ag["paciente_id"],
                          ag["inicio"], ag["fim"], ignorar_id=agendamento_id)
        if choque:
            flash("Não dá para reativar: " + choque, "erro")
            return voltar
    if ag["evolucao_id"] and novo != "realizado":
        flash("Esta sessão já tem evolução registrada no prontuário, então continua como realizada.",
              "erro")
        return voltar
    db.execute("UPDATE agendamentos SET status = ?, atualizado_em = ? WHERE id = ?",
               (novo, agora_txt(), agendamento_id))
    db.commit()
    flash(f"{ag['paciente_nome']} ({formatar_datahora(ag['inicio'])}): {STATUS[novo].lower()}.",
          "sucesso")
    return voltar


@app.post("/agenda/<int:agendamento_id>/cancelar-serie")
@requer()
def agendamento_cancelar_serie(agendamento_id):
    ag = carregar_agendamento(agendamento_id)
    if not ag["serie"]:
        abort(400)
    db = get_db()
    cur = db.execute(
        "UPDATE agendamentos SET status = 'cancelado', atualizado_em = ? "
        "WHERE serie = ? AND inicio >= ? AND status IN ('agendado', 'confirmado')",
        (agora_txt(), ag["serie"], ag["inicio"]),
    )
    db.commit()
    flash(f"{cur.rowcount} sessões de {ag['paciente_nome']} canceladas a partir de "
          f"{formatar_data(ag['inicio'])}.", "sucesso")
    return redirect(url_for("agenda", semana=ag["inicio"][:10]))


# ---------------------------------------------------------------- pacientes

CAMPOS_ADMIN = [
    "nome", "data_nascimento", "cpf", "telefone", "email", "responsavel_nome",
    "responsavel_parentesco", "responsavel_telefone", "endereco", "convenio_id", "carteirinha",
    "profissional_id", "valor_sessao_centavos", "observacoes", "ativo",
]
CAMPOS_CLINICOS = ["queixa_principal", "hipotese_diagnostica", "anamnese"]
LIMITES = {
    "nome": 120, "cpf": 14, "telefone": 20, "email": 120, "responsavel_nome": 120,
    "responsavel_parentesco": 40, "responsavel_telefone": 20, "endereco": 300,
    "carteirinha": 40, "observacoes": 2000, "queixa_principal": 2000,
    "hipotese_diagnostica": 1000, "anamnese": 20000,
}


@app.get("/pacientes")
@requer()
def pacientes():
    busca = request.args.get("q", "").strip()
    inativos = request.args.get("inativos") == "1"
    sql = """SELECT p.*, u.nome AS profissional_nome, cv.nome AS convenio_nome,
                (SELECT MIN(a.inicio) FROM agendamentos a WHERE a.paciente_id = p.id
                   AND a.inicio >= ? AND a.status IN ('agendado', 'confirmado')) AS proxima
             FROM pacientes p LEFT JOIN usuarios u ON u.id = p.profissional_id
             LEFT JOIN convenios cv ON cv.id = p.convenio_id WHERE p.ativo = ?"""
    params = [date.today().isoformat(), 0 if inativos else 1]
    if busca:
        digitos = so_digitos(busca)
        sql += " AND (p.nome LIKE ? OR p.responsavel_nome LIKE ?"
        params += [f"%{busca}%", f"%{busca}%"]
        if len(digitos) >= 4:
            sql += """ OR replace(replace(replace(replace(p.telefone, ' ', ''), '-', ''), '(', ''), ')', '') LIKE ?
                       OR replace(replace(replace(replace(p.responsavel_telefone, ' ', ''), '-', ''), '(', ''), ')', '') LIKE ?
                       OR replace(replace(p.cpf, '.', ''), '-', '') LIKE ?"""
            params += [f"%{digitos}%"] * 3
        sql += ")"
    lista = get_db().execute(sql + " ORDER BY p.nome LIMIT 500", params).fetchall()
    return render_template("pacientes.html", pacientes=lista, busca=busca, inativos=inativos)


def validar_paciente(f, clinico):
    campos = CAMPOS_ADMIN + (CAMPOS_CLINICOS if clinico else [])
    dados = {c: (f.get(c) or "").strip() or None for c in campos}
    erros = []
    if not dados["nome"]:
        erros.append("Informe o nome do paciente.")
    for campo, limite in LIMITES.items():
        if campo in dados and dados[campo] and len(dados[campo]) > limite:
            erros.append(f"O campo “{campo.replace('_', ' ')}” aceita até {limite} caracteres.")
    if dados["data_nascimento"]:
        nasc = ler_data(dados["data_nascimento"])
        if nasc is None or nasc > date.today() or nasc.year < 1900:
            erros.append("Data de nascimento inválida.")
    if dados["cpf"] and len(so_digitos(dados["cpf"])) != 11:
        erros.append("CPF deve ter 11 dígitos.")
    if not dados["telefone"] and not dados["responsavel_telefone"]:
        erros.append("Informe um telefone do paciente ou do responsável.")
    for campo in ("telefone", "responsavel_telefone"):
        if dados[campo] and not 10 <= len(so_digitos(dados[campo])) <= 13:
            erros.append("Telefone deve ter DDD (ex.: 11 98765-4321).")
    if dados["profissional_id"]:
        if dados["profissional_id"] in {str(p["id"]) for p in profissionais()}:
            dados["profissional_id"] = int(dados["profissional_id"])
        else:
            erros.append("Profissional de referência inválido.")
    if dados["convenio_id"]:
        if dados["convenio_id"] in {str(c["id"]) for c in convenios(so_ativos=False)}:
            dados["convenio_id"] = int(dados["convenio_id"])
        else:
            erros.append("Plano de saúde inválido.")
    else:
        dados["carteirinha"] = None  # particular não tem carteirinha
    valor = ler_preco(f.get("valor_sessao") or "0")
    if valor is None:
        erros.append("Valor da sessão inválido. Use o formato 120,00.")
    dados["valor_sessao_centavos"] = valor or 0
    dados["ativo"] = 1 if f.get("ativo") else 0
    return dados, erros


@app.route("/pacientes/novo", methods=["GET", "POST"])
@requer()
def paciente_novo():
    if request.method == "POST":
        dados, erros = validar_paciente(request.form, pode_prontuario())
        if not erros:
            colunas = ", ".join(dados)
            marcadores = ", ".join(f":{c}" for c in dados)
            db = get_db()
            cur = db.execute(f"INSERT INTO pacientes ({colunas}) VALUES ({marcadores})", dados)
            db.commit()
            flash(f"Paciente “{dados['nome']}” cadastrado.", "sucesso")
            return redirect(url_for("paciente", paciente_id=cur.lastrowid))
        for erro in erros:
            flash(erro, "erro")
        return render_template("paciente_form.html", form=request.form, paciente=None,
                               profissionais=profissionais()), 400
    form = {"ativo": "1", "profissional_id": str(g.usuario["id"]) if g.usuario["atende"] else ""}
    return render_template("paciente_form.html", form=form, paciente=None,
                           profissionais=profissionais())


@app.route("/pacientes/<int:paciente_id>/editar", methods=["GET", "POST"])
@requer()
def paciente_editar(paciente_id):
    paciente = carregar_paciente(paciente_id)
    if request.method == "POST":
        # Quem não atende não vê os campos clínicos, então eles ficam como estão.
        dados, erros = validar_paciente(request.form, pode_prontuario())
        if not erros:
            atribuicoes = ", ".join(f"{c} = :{c}" for c in dados)
            db = get_db()
            db.execute(f"UPDATE pacientes SET {atribuicoes} WHERE id = :id", {**dados, "id": paciente_id})
            db.commit()
            flash("Cadastro atualizado.", "sucesso")
            return redirect(url_for("paciente", paciente_id=paciente_id))
        for erro in erros:
            flash(erro, "erro")
        return render_template("paciente_form.html", form=request.form, paciente=paciente,
                               profissionais=profissionais()), 400
    form = {c: paciente[c] if paciente[c] is not None else "" for c in CAMPOS_ADMIN + CAMPOS_CLINICOS}
    form["valor_sessao"] = centavos_para_form(paciente["valor_sessao_centavos"])
    form["profissional_id"] = str(paciente["profissional_id"] or "")
    form["convenio_id"] = str(paciente["convenio_id"] or "")
    form["ativo"] = "1" if paciente["ativo"] else ""
    return render_template("paciente_form.html", form=form, paciente=paciente,
                           profissionais=profissionais())


@app.get("/pacientes/<int:paciente_id>")
@requer()
def paciente(paciente_id):
    return pagina_paciente(paciente_id, request.args.get("aba", "resumo"))


def pagina_paciente(paciente_id, aba, rascunho=None, status=200):
    paciente = carregar_paciente(paciente_id)
    db = get_db()
    if aba == "prontuario" and not pode_prontuario():
        aba = "resumo"
    hoje_txt = date.today().isoformat()
    contexto = {"paciente": paciente, "aba": aba, "rascunho": rascunho or {}}
    contexto["areas"] = [r[0] for r in db.execute(
        "SELECT DISTINCT es.nome FROM agendamentos a JOIN especialidades es ON es.id = a.especialidade_id "
        "WHERE a.paciente_id = ? AND a.status != 'cancelado' ORDER BY es.nome",
        (paciente_id,),
    )]

    contexto["proximas"] = db.execute(
        SQL_AGENDA + " WHERE a.paciente_id = ? AND a.inicio >= ? AND a.status NOT IN ('cancelado') "
        "ORDER BY a.inicio LIMIT 8",
        (paciente_id, hoje_txt),
    ).fetchall()
    cobrado, pago = db.execute(
        f"""SELECT
              COALESCE((SELECT SUM(a.valor_centavos) FROM agendamentos a
                        WHERE a.paciente_id = ? AND {cobra_paciente_sql()}), 0),
              COALESCE((SELECT SUM(valor_centavos) FROM pagamentos WHERE paciente_id = ?), 0)""",
        (paciente_id, paciente_id),
    ).fetchone()
    contexto.update(cobrado=cobrado, pago=pago, saldo=pago - cobrado)

    if aba == "resumo":
        contexto["frequencia"] = dict(db.execute(
            "SELECT status, COUNT(*) FROM agendamentos WHERE paciente_id = ? AND inicio < ? "
            "GROUP BY status", (paciente_id, hoje_txt),
        ).fetchall())
    elif aba == "agenda":
        contexto["historico"] = db.execute(
            SQL_AGENDA + " WHERE a.paciente_id = ? ORDER BY a.inicio DESC LIMIT 300", (paciente_id,)
        ).fetchall()
    elif aba == "prontuario":
        contexto["evolucoes"] = buscar_evolucoes(paciente_id, "e.data DESC, e.id DESC")
        contexto["sessoes_sem_evolucao"] = db.execute(
            SQL_AGENDA + """ WHERE a.paciente_id = ? AND a.status = 'realizado'
                AND NOT EXISTS (SELECT 1 FROM evolucoes e WHERE e.agendamento_id = a.id)
                ORDER BY a.inicio DESC""",
            (paciente_id,),
        ).fetchall()
        contexto["agendamento_escolhido"] = request.args.get("agendamento", "")
        contexto["hoje"] = hoje_txt
    elif aba == "financeiro":
        contexto["cobrancas"] = db.execute(
            SQL_AGENDA + f" WHERE a.paciente_id = ? AND {cobra_paciente_sql()} ORDER BY a.inicio DESC",
            (paciente_id,),
        ).fetchall()
        contexto["sessoes_plano"] = db.execute(
            SQL_AGENDA + " WHERE a.paciente_id = ? AND a.convenio_id IS NOT NULL AND a.status = 'realizado' "
            "ORDER BY a.inicio DESC LIMIT 100",
            (paciente_id,),
        ).fetchall()
        contexto["pagamentos"] = db.execute(
            "SELECT x.*, u.nome AS registrado_por_nome FROM pagamentos x "
            "LEFT JOIN usuarios u ON u.id = x.registrado_por WHERE x.paciente_id = ? "
            "ORDER BY x.data DESC, x.id DESC",
            (paciente_id,),
        ).fetchall()
        contexto["hoje"] = hoje_txt
    return render_template("paciente.html", **contexto), status


# ---------------------------------------------------------------- prontuário

def buscar_evolucoes(paciente_id, ordem):
    """Evoluções do paciente; as sigilosas de outros profissionais vêm sem o texto."""
    linhas = get_db().execute(
        f"""SELECT e.*, u.nome AS profissional_nome, u.registro, a.tipo, es.nome AS especialidade_nome
            FROM evolucoes e JOIN usuarios u ON u.id = e.profissional_id
            LEFT JOIN agendamentos a ON a.id = e.agendamento_id
            LEFT JOIN especialidades es ON es.id = e.especialidade_id
            WHERE e.paciente_id = ? ORDER BY {ordem}""",
        (paciente_id,),
    ).fetchall()
    evolucoes = []
    for linha in linhas:
        e = dict(linha)
        e["restrita"] = bool(e["sigilosa"]) and e["profissional_id"] != g.usuario["id"]
        if e["restrita"]:
            e["texto"] = None
        evolucoes.append(e)
    return evolucoes


@app.post("/pacientes/<int:paciente_id>/evolucoes")
@requer()
def evolucao_nova(paciente_id):
    exigir_prontuario()
    carregar_paciente(paciente_id)
    db = get_db()
    f = request.form
    texto = f.get("texto", "").strip()
    voltar = url_for("paciente", paciente_id=paciente_id, aba="prontuario")
    erros = []
    if not texto:
        erros.append("Escreva a evolução.")
    elif len(texto) > 20000:
        erros.append("Evolução muito longa (até 20.000 caracteres).")

    agendamento_id = int(f["agendamento_id"]) if f.get("agendamento_id", "").isdigit() else None
    data_evol = ler_data(f.get("data"))
    especialidade_id = especialidade_padrao(g.usuario["id"])
    if agendamento_id:
        ag = db.execute(
            "SELECT * FROM agendamentos WHERE id = ? AND paciente_id = ? AND status = 'realizado'",
            (agendamento_id, paciente_id),
        ).fetchone()
        if ag is None:
            erros.append("Sessão inválida para esta evolução.")
        elif db.execute("SELECT 1 FROM evolucoes WHERE agendamento_id = ?", (agendamento_id,)).fetchone():
            erros.append("Essa sessão já tem evolução registrada.")
        else:
            data_evol = date.fromisoformat(ag["inicio"][:10])
            especialidade_id = ag["especialidade_id"] or especialidade_id
    if data_evol is None or data_evol > date.today():
        erros.append("Informe a data do atendimento (não pode ser no futuro).")
    sigilosa = 1 if f.get("sigilosa") else 0

    if erros:
        for erro in erros:
            flash(erro, "erro")
        # Devolve a página com o texto preenchido para não perder o que foi escrito.
        rascunho = {"texto": texto, "data": f.get("data", ""), "agendamento_id": f.get("agendamento_id", ""),
                    "sigilosa": sigilosa}
        return pagina_paciente(paciente_id, "prontuario", rascunho, 400)
    db.execute(
        "INSERT INTO evolucoes (paciente_id, agendamento_id, profissional_id, especialidade_id, data, texto, "
        "sigilosa) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (paciente_id, agendamento_id, g.usuario["id"], especialidade_id, data_evol.isoformat(), texto, sigilosa),
    )
    db.commit()
    flash("Evolução registrada.", "sucesso")
    return redirect(voltar)


@app.route("/evolucoes/<int:evolucao_id>/editar", methods=["GET", "POST"])
@requer()
def evolucao_editar(evolucao_id):
    exigir_prontuario()
    db = get_db()
    evolucao = db.execute(
        "SELECT e.*, p.nome AS paciente_nome FROM evolucoes e JOIN pacientes p ON p.id = e.paciente_id "
        "WHERE e.id = ?", (evolucao_id,),
    ).fetchone()
    if evolucao is None:
        abort(404)
    if evolucao["profissional_id"] != g.usuario["id"]:
        abort(403, "Só quem escreveu a evolução pode alterá-la.")
    if request.method == "POST":
        texto = request.form.get("texto", "").strip()
        sigilosa = 1 if request.form.get("sigilosa") else 0
        if not texto or len(texto) > 20000:
            flash("Escreva a evolução (até 20.000 caracteres).", "erro")
            return render_template("evolucao_form.html", evolucao=evolucao, texto=texto, sigilosa=sigilosa), 400
        db.execute("UPDATE evolucoes SET texto = ?, sigilosa = ?, editado_em = ? WHERE id = ?",
                   (texto, sigilosa, agora_txt(), evolucao_id))
        db.commit()
        flash("Evolução atualizada.", "sucesso")
        return redirect(url_for("paciente", paciente_id=evolucao["paciente_id"], aba="prontuario"))
    return render_template("evolucao_form.html", evolucao=evolucao, texto=evolucao["texto"],
                           sigilosa=evolucao["sigilosa"])


@app.get("/pacientes/<int:paciente_id>/prontuario")
@requer()
def prontuario_impressao(paciente_id):
    exigir_prontuario()
    paciente = carregar_paciente(paciente_id)
    evolucoes = buscar_evolucoes(paciente_id, "e.data, e.id")
    return render_template("prontuario_impressao.html", paciente=paciente, evolucoes=evolucoes,
                           emitido_em=agora_txt())


# ---------------------------------------------------------------- financeiro

@app.post("/pacientes/<int:paciente_id>/pagamentos")
@requer()
def pagamento_novo(paciente_id):
    carregar_paciente(paciente_id)
    f = request.form
    valor = ler_preco(f.get("valor", ""))
    data_pag = ler_data(f.get("data"))
    forma = f.get("forma", "")
    descricao = f.get("descricao", "").strip()
    erros = []
    if not valor:
        erros.append("Informe o valor recebido.")
    if data_pag is None or data_pag > date.today():
        erros.append("Data do pagamento inválida.")
    if forma not in FORMAS:
        erros.append("Escolha a forma de pagamento.")
    if len(descricao) > 200:
        erros.append("Descrição muito longa (até 200 caracteres).")
    if erros:
        for erro in erros:
            flash(erro, "erro")
    else:
        db = get_db()
        db.execute(
            "INSERT INTO pagamentos (paciente_id, data, valor_centavos, forma, descricao, registrado_por) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (paciente_id, data_pag.isoformat(), valor, forma, descricao or None, g.usuario["id"]),
        )
        db.commit()
        flash(f"Pagamento de {formatar_brl(valor)} registrado.", "sucesso")
    return redirect(url_for("paciente", paciente_id=paciente_id, aba="financeiro"))


@app.post("/pagamentos/<int:pagamento_id>/excluir")
@requer("admin")
def pagamento_excluir(pagamento_id):
    db = get_db()
    pag = db.execute("SELECT * FROM pagamentos WHERE id = ?", (pagamento_id,)).fetchone()
    if pag is None:
        abort(404)
    db.execute("DELETE FROM pagamentos WHERE id = ?", (pagamento_id,))
    db.commit()
    flash(f"Pagamento de {formatar_brl(pag['valor_centavos'])} de {formatar_data(pag['data'])} excluído.",
          "sucesso")
    return redirect(url_for("paciente", paciente_id=pag["paciente_id"], aba="financeiro"))


@app.get("/financeiro")
@requer("admin", "recepcao")
def financeiro():
    try:
        mes = datetime.strptime(request.args.get("mes", ""), "%Y-%m").date()
    except ValueError:
        mes = date.today().replace(day=1)
    proximo = (mes + timedelta(days=32)).replace(day=1)
    anterior = (mes - timedelta(days=1)).replace(day=1)
    ini, fim = mes.isoformat(), proximo.isoformat()
    db = get_db()

    recebido = db.execute(
        "SELECT forma, COUNT(*) AS qtd, SUM(valor_centavos) AS total FROM pagamentos "
        "WHERE data >= ? AND data < ? GROUP BY forma ORDER BY total DESC",
        (ini, fim),
    ).fetchall()
    producao = db.execute(
        f"""SELECT u.nome, u.cor,
              SUM(a.status = 'realizado') AS realizadas,
              SUM(a.status = 'falta') AS faltas,
              SUM(a.status = 'falta_justificada') AS faltas_justificadas,
              SUM(a.status = 'cancelado') AS canceladas,
              SUM({PRODUZIDO_SQL}) AS valor
            FROM agendamentos a JOIN usuarios u ON u.id = a.profissional_id
            WHERE a.inicio >= ? AND a.inicio < ?
            GROUP BY u.id ORDER BY u.nome""",
        (ini, fim),
    ).fetchall()
    por_especialidade = db.execute(
        f"""SELECT COALESCE(es.nome, 'Sem especialidade') AS nome,
              SUM(a.status = 'realizado') AS realizadas,
              SUM({PRODUZIDO_SQL}) AS valor
            FROM agendamentos a LEFT JOIN especialidades es ON es.id = a.especialidade_id
            WHERE a.inicio >= ? AND a.inicio < ?
            GROUP BY es.id ORDER BY valor DESC""",
        (ini, fim),
    ).fetchall()
    # Particular x cada plano, com a situação do faturamento das sessões de plano.
    por_cobranca = db.execute(
        f"""SELECT a.convenio_id, COALESCE(cv.nome, 'Particular') AS nome,
              SUM(a.status = 'realizado') AS realizadas,
              SUM({PRODUZIDO_SQL}) AS valor,
              SUM(CASE WHEN a.status = 'realizado' AND a.faturamento IS NULL THEN a.valor_centavos ELSE 0 END)
                  AS a_faturar,
              SUM(CASE WHEN a.status = 'realizado' AND a.faturamento = 'faturado' THEN a.valor_centavos ELSE 0 END)
                  AS faturado,
              SUM(CASE WHEN a.status = 'realizado' AND a.faturamento = 'pago' THEN a.valor_centavos ELSE 0 END)
                  AS pago,
              SUM(CASE WHEN a.status = 'realizado' AND a.faturamento = 'glosado' THEN a.valor_centavos ELSE 0 END)
                  AS glosado
            FROM agendamentos a LEFT JOIN convenios cv ON cv.id = a.convenio_id
            WHERE a.inicio >= ? AND a.inicio < ?
            GROUP BY a.convenio_id ORDER BY a.convenio_id IS NOT NULL, cv.nome""",
        (ini, fim),
    ).fetchall()
    devedores = [s for s in saldos_pacientes() if s["cobrado"] > s["pago"]]
    return render_template(
        "financeiro.html", mes=mes, anterior=anterior.strftime("%Y-%m"),
        proximo=proximo.strftime("%Y-%m"), recebido=recebido, por_especialidade=por_especialidade,
        por_cobranca=por_cobranca,
        total_recebido=sum(r["total"] for r in recebido), producao=producao,
        total_produzido=sum(p["valor"] or 0 for p in producao), devedores=devedores,
        total_aberto=sum(d["cobrado"] - d["pago"] for d in devedores),
    )


# ---------------------------------------------------------------- convênios

@app.route("/faturamento", methods=["GET", "POST"])
@requer("admin", "recepcao")
def faturamento():
    db = get_db()
    if request.method == "POST":
        f = request.form
        nova = f.get("situacao")
        ids = [int(i) for i in f.getlist("sessoes") if i.isdigit()]
        if nova not in FATURAMENTO or not ids:
            flash("Marque as sessões e escolha a nova situação.", "erro")
        else:
            marcadores = ",".join("?" * len(ids))
            cur = db.execute(
                f"UPDATE agendamentos SET faturamento = ?, atualizado_em = ? "
                f"WHERE id IN ({marcadores}) AND convenio_id IS NOT NULL AND status = 'realizado'",
                (nova or None, agora_txt(), *ids),
            )
            db.commit()
            flash(f"{cur.rowcount} sessões marcadas como “{FATURAMENTO[nova].lower()}”.", "sucesso")
        return redirect(destino_seguro(f.get("voltar"), url_for("faturamento")))

    a = request.args
    try:
        mes = datetime.strptime(a.get("mes", ""), "%Y-%m").date()
    except ValueError:
        mes = date.today().replace(day=1)
    proximo = (mes + timedelta(days=32)).replace(day=1)
    anterior = (mes - timedelta(days=1)).replace(day=1)
    lista_convenios = convenios(so_ativos=False)
    convenio_id = a.get("convenio", type=int)
    situacao = a.get("situacao", "todas")

    sql = SQL_AGENDA + " WHERE a.convenio_id IS NOT NULL AND a.status = 'realizado' AND a.inicio >= ? AND a.inicio < ?"
    params = [mes.isoformat(), proximo.isoformat()]
    if convenio_id:
        sql += " AND a.convenio_id = ?"
        params.append(convenio_id)
    if situacao in FATURAMENTO:
        sql += " AND COALESCE(a.faturamento, '') = ?"
        params.append(situacao)
    sessoes = db.execute(sql + " ORDER BY cv.nome, p.nome, a.inicio", params).fetchall()
    totais = {chave: sum(s["valor_centavos"] for s in sessoes if (s["faturamento"] or "") == chave)
              for chave in FATURAMENTO}
    return render_template(
        "faturamento.html", sessoes=sessoes, totais=totais, mes=mes, convenios=lista_convenios,
        convenio_id=convenio_id, situacao=situacao, anterior=anterior.strftime("%Y-%m"),
        proximo=proximo.strftime("%Y-%m"),
        sem_guia=sum(1 for s in sessoes if not s["guia"]),
        sem_carteirinha=sum(1 for s in sessoes if not s["carteirinha"]),
    )


@app.route("/convenios", methods=["GET", "POST"])
@requer("admin")
def convenios_admin():
    db = get_db()
    lista_esp = especialidades(so_ativas=False)
    if request.method == "POST":
        f = request.form
        acao = f.get("acao")
        nome = f.get("nome", "").strip()
        conv_id = f.get("id", "")
        if acao in ("criar", "salvar") and (not nome or len(nome) > 60):
            flash("Informe o nome do plano (até 60 caracteres).", "erro")
        elif acao in ("criar", "salvar") and db.execute(
                "SELECT 1 FROM convenios WHERE nome = ? AND id != ?", (nome, conv_id or 0)).fetchone():
            flash(f"Já existe o plano “{nome}”.", "erro")
        elif acao == "criar":
            db.execute("INSERT INTO convenios (nome) VALUES (?)", (nome,))
            flash(f"Plano “{nome}” criado. Marque as especialidades que ele cobre e os valores.", "sucesso")
        elif acao == "salvar" and conv_id.isdigit():
            cobertura_nova, erros = [], []
            for e in lista_esp:
                if not f.get(f"cobre_{e['id']}"):
                    continue
                valor = ler_preco(f.get(f"valor_{e['id']}") or "0")
                if valor is None:
                    erros.append(f"Valor inválido em {e['nome']}. Use o formato 80,00.")
                cobertura_nova.append((int(conv_id), e["id"], valor or 0))
            if erros:
                for erro in erros:
                    flash(erro, "erro")
                return redirect(url_for("convenios_admin"))
            db.execute("UPDATE convenios SET nome = ? WHERE id = ?", (nome, conv_id))
            db.execute("DELETE FROM convenio_especialidades WHERE convenio_id = ?", (conv_id,))
            db.executemany("INSERT INTO convenio_especialidades (convenio_id, especialidade_id, valor_centavos) "
                           "VALUES (?, ?, ?)", cobertura_nova)
            flash(f"Plano “{nome}” salvo.", "sucesso")
        elif acao == "alternar" and conv_id.isdigit():
            db.execute("UPDATE convenios SET ativo = 1 - ativo WHERE id = ?", (conv_id,))
            flash("Situação do plano atualizada.", "sucesso")
        else:
            abort(400)
        db.commit()
        return redirect(url_for("convenios_admin"))

    coberturas = {}
    for linha in db.execute("SELECT * FROM convenio_especialidades"):
        coberturas[(linha["convenio_id"], linha["especialidade_id"])] = linha["valor_centavos"]
    pacientes_por_plano = dict(db.execute(
        "SELECT convenio_id, COUNT(*) FROM pacientes WHERE ativo = 1 AND convenio_id IS NOT NULL GROUP BY convenio_id"
    ).fetchall())
    return render_template("convenios.html", convenios=convenios(so_ativos=False), especialidades=lista_esp,
                           coberturas=coberturas, pacientes_por_plano=pacientes_por_plano)


# ---------------------------------------------------------------- usuários

@app.get("/usuarios")
@requer("admin")
def usuarios():
    lista = get_db().execute(
        f"SELECT usuarios.*, {SQL_ESPECIALIDADES_DO_USUARIO} AS especialidades_txt FROM usuarios "
        "ORDER BY ativo DESC, nome"
    ).fetchall()
    return render_template("usuarios.html", usuarios=lista)


def ler_especialidades_form(f, dados, erros):
    validas = {str(e["id"]) for e in especialidades(so_ativas=False)}
    escolhidas = [int(i) for i in f.getlist("especialidades") if i in validas]
    if dados["atende"] and not escolhidas:
        erros.append("Marque ao menos uma especialidade para quem atende.")
    return escolhidas if dados["atende"] else []


def salvar_especialidades(db, usuario_id, ids):
    db.execute("DELETE FROM profissional_especialidades WHERE usuario_id = ?", (usuario_id,))
    db.executemany("INSERT INTO profissional_especialidades (usuario_id, especialidade_id) VALUES (?, ?)",
                   [(usuario_id, i) for i in ids])


def validar_usuario(f, usuario_id=None):
    dados = {
        "nome": f.get("nome", "").strip(),
        "email": f.get("email", "").strip().lower(),
        "papel": f.get("papel", ""),
        "atende": 1 if f.get("atende") else 0,
        "registro": f.get("registro", "").strip() or None,
        "cor": f.get("cor", CORES[0]),
        "ativo": 1 if f.get("ativo") else 0,
    }
    erros = []
    if not dados["nome"] or len(dados["nome"]) > 120:
        erros.append("Informe o nome (até 120 caracteres).")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", dados["email"]) or len(dados["email"]) > 120:
        erros.append("E-mail inválido.")
    elif get_db().execute("SELECT 1 FROM usuarios WHERE email = ? AND id != ?",
                          (dados["email"], usuario_id or 0)).fetchone():
        erros.append("Já existe um usuário com esse e-mail.")
    if dados["papel"] not in PAPEIS:
        erros.append("Escolha o papel.")
    if dados["papel"] == "profissional":
        dados["atende"] = 1
    if dados["registro"] and len(dados["registro"]) > 30:
        erros.append("Registro profissional muito longo.")
    if dados["cor"] not in CORES:
        dados["cor"] = CORES[0]
    return dados, erros


@app.route("/usuarios/novo", methods=["GET", "POST"])
@requer("admin")
def usuario_novo():
    if request.method == "POST":
        f = request.form
        dados, erros = validar_usuario(f)
        esp_ids = ler_especialidades_form(f, dados, erros)
        erros += validar_senha(f.get("senha", ""), f.get("senha", ""))
        if not erros:
            db = get_db()
            cur = db.execute(
                """INSERT INTO usuarios (nome, email, senha_hash, papel, atende, registro, cor, ativo,
                       trocar_senha) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (dados["nome"], dados["email"], generate_password_hash(f["senha"]), dados["papel"],
                 dados["atende"], dados["registro"], dados["cor"], dados["ativo"]),
            )
            salvar_especialidades(db, cur.lastrowid, esp_ids)
            db.commit()
            flash(f"Usuário {dados['nome']} criado. A senha deverá ser trocada no primeiro acesso.",
                  "sucesso")
            return redirect(url_for("usuarios"))
        for erro in erros:
            flash(erro, "erro")
        return render_template("usuario_form.html", form=f, usuario=None, especialidades=especialidades(),
                               marcadas=esp_ids), 400
    usados = {r[0] for r in get_db().execute("SELECT cor FROM usuarios WHERE atende = 1")}
    cor = next((c for c in CORES if c not in usados), CORES[0])
    return render_template("usuario_form.html", form={"ativo": "1", "papel": "profissional",
                                                      "atende": "1", "cor": cor}, usuario=None,
                           especialidades=especialidades(), marcadas=[])


@app.route("/usuarios/<int:usuario_id>/editar", methods=["GET", "POST"])
@requer("admin")
def usuario_editar(usuario_id):
    db = get_db()
    usuario = db.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if usuario is None:
        abort(404)
    if request.method == "POST":
        f = request.form
        dados, erros = validar_usuario(f, usuario_id)
        esp_ids = ler_especialidades_form(f, dados, erros)
        nova_senha = f.get("senha", "")
        if nova_senha:
            erros += validar_senha(nova_senha, nova_senha)
        perde_admin = usuario["papel"] == "admin" and (dados["papel"] != "admin" or not dados["ativo"])
        if perde_admin:
            outros = db.execute("SELECT COUNT(*) FROM usuarios WHERE papel = 'admin' AND ativo = 1 "
                                "AND id != ?", (usuario_id,)).fetchone()[0]
            if outros == 0:
                erros.append("Este é o único administrador ativo. Crie outro antes de alterar este.")
        if not erros:
            db.execute(
                "UPDATE usuarios SET nome = ?, email = ?, papel = ?, atende = ?, registro = ?, cor = ?, "
                "ativo = ? WHERE id = ?",
                (dados["nome"], dados["email"], dados["papel"], dados["atende"], dados["registro"],
                 dados["cor"], dados["ativo"], usuario_id),
            )
            salvar_especialidades(db, usuario_id, esp_ids)
            if nova_senha:
                db.execute("UPDATE usuarios SET senha_hash = ?, trocar_senha = 1, falhas_login = 0, "
                           "bloqueado_ate = NULL WHERE id = ?",
                           (generate_password_hash(nova_senha), usuario_id))
            db.commit()
            flash("Usuário atualizado." + (" Nova senha definida; será pedida a troca no próximo acesso."
                                           if nova_senha else ""), "sucesso")
            return redirect(url_for("usuarios"))
        for erro in erros:
            flash(erro, "erro")
        return render_template("usuario_form.html", form=f, usuario=usuario,
                               especialidades=especialidades(so_ativas=False), marcadas=esp_ids), 400
    form = {k: usuario[k] for k in ("nome", "email", "papel", "registro", "cor")}
    form["registro"] = form["registro"] or ""
    form["atende"] = "1" if usuario["atende"] else ""
    form["ativo"] = "1" if usuario["ativo"] else ""
    marcadas = [r[0] for r in db.execute(
        "SELECT especialidade_id FROM profissional_especialidades WHERE usuario_id = ?", (usuario_id,))]
    return render_template("usuario_form.html", form=form, usuario=usuario,
                           especialidades=especialidades(so_ativas=False), marcadas=marcadas)


@app.route("/especialidades", methods=["GET", "POST"])
@requer("admin")
def especialidades_admin():
    db = get_db()
    if request.method == "POST":
        f = request.form
        acao = f.get("acao")
        nome = f.get("nome", "").strip()
        esp_id = f.get("id", "")
        if acao in ("criar", "renomear") and (not nome or len(nome) > 60):
            flash("Informe o nome da especialidade (até 60 caracteres).", "erro")
        elif acao in ("criar", "renomear") and db.execute(
                "SELECT 1 FROM especialidades WHERE nome = ? AND id != ?", (nome, esp_id or 0)).fetchone():
            flash(f"Já existe a especialidade “{nome}”.", "erro")
        elif acao == "criar":
            db.execute("INSERT INTO especialidades (nome) VALUES (?)", (nome,))
            flash(f"Especialidade “{nome}” criada. Marque-a no cadastro de quem atende.", "sucesso")
        elif acao == "renomear" and esp_id.isdigit():
            db.execute("UPDATE especialidades SET nome = ? WHERE id = ?", (nome, esp_id))
            flash("Nome atualizado.", "sucesso")
        elif acao == "alternar" and esp_id.isdigit():
            db.execute("UPDATE especialidades SET ativo = 1 - ativo WHERE id = ?", (esp_id,))
            flash("Situação atualizada.", "sucesso")
        else:
            abort(400)
        db.commit()
        return redirect(url_for("especialidades_admin"))
    lista = db.execute(
        """SELECT es.*, (SELECT group_concat(u.nome, ', ') FROM profissional_especialidades pe
                         JOIN usuarios u ON u.id = pe.usuario_id
                         WHERE pe.especialidade_id = es.id AND u.ativo = 1) AS profissionais
           FROM especialidades es ORDER BY es.ativo DESC, es.nome"""
    ).fetchall()
    return render_template("especialidades.html", especialidades=lista)


# ---------------------------------------------------------------- agendamento online (público)

SQL_ONLINE = """
    SELECT u.* FROM usuarios u
    WHERE u.atende = 1 AND u.ativo = 1 AND u.agenda_online = 1
      AND EXISTS (SELECT 1 FROM horarios h WHERE h.profissional_id = u.id)
"""


def profissionais_online(esp_id):
    return get_db().execute(
        SQL_ONLINE + " AND EXISTS (SELECT 1 FROM profissional_especialidades pe "
        "WHERE pe.usuario_id = u.id AND pe.especialidade_id = ?) ORDER BY u.nome",
        (esp_id,),
    ).fetchall()


def especialidades_online():
    """Especialidades ativas com pelo menos um profissional aceitando agendamento online."""
    return get_db().execute(
        f"""SELECT es.* FROM especialidades es WHERE es.ativo = 1 AND EXISTS (
                SELECT 1 FROM profissional_especialidades pe JOIN ({SQL_ONLINE}) o ON o.id = pe.usuario_id
                WHERE pe.especialidade_id = es.id)
            ORDER BY es.nome"""
    ).fetchall()


def horarios_livres(db, prof, dia):
    """Inícios ('AAAA-MM-DD HH:MM') livres do profissional no dia, pelo horário de atendimento."""
    intervalos = db.execute(
        "SELECT inicio, fim FROM horarios WHERE profissional_id = ? AND dia_semana = ? ORDER BY inicio",
        (prof["id"], dia.weekday()),
    ).fetchall()
    if not intervalos:
        return []
    dia_ini, dia_fim = dia.isoformat(), (dia + timedelta(days=1)).isoformat()
    marcadores = ",".join("?" * len(LIBERAM_HORARIO))
    ocupados = db.execute(
        f"""SELECT inicio, fim FROM agendamentos WHERE profissional_id = ?
                AND status NOT IN ({marcadores}) AND inicio < ? AND fim > ?
            UNION ALL
            SELECT inicio, fim FROM solicitacoes WHERE profissional_id = ? AND status = 'pendente'
                AND inicio < ? AND fim > ?""",
        (prof["id"], *LIBERAM_HORARIO, dia_fim, dia_ini, prof["id"], dia_fim, dia_ini),
    ).fetchall()
    limite = datetime.now() + timedelta(hours=ANTECEDENCIA_HORAS)
    duracao = timedelta(minutes=prof["duracao_online"])
    livres = []
    for intervalo in intervalos:
        t = datetime.combine(dia, time.fromisoformat(intervalo["inicio"]))
        fim = datetime.combine(dia, time.fromisoformat(intervalo["fim"]))
        while t + duracao <= fim:
            ini_txt, fim_txt = t.strftime(FMT_DATAHORA), (t + duracao).strftime(FMT_DATAHORA)
            if t >= limite and not any(o["inicio"] < fim_txt and o["fim"] > ini_txt for o in ocupados):
                livres.append(ini_txt)
            t += duracao
    return livres


def opcoes_do_dia(db, profs, dia):
    """{inicio: profissional}. Sem preferência, cada horário fica com o primeiro profissional livre."""
    opcoes = {}
    for p in profs:
        for inicio in horarios_livres(db, p, dia):
            opcoes.setdefault(inicio, p)
    return dict(sorted(opcoes.items()))


@app.template_filter("telefone")
def formatar_telefone(texto):
    d = so_digitos(texto)
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return texto or ""


def pagina_agendar(esp_arg, prof_arg, dia_arg, form, status=200):
    db = get_db()
    areas = especialidades_online()
    especialidade = next((e for e in areas if str(e["id"]) == esp_arg), None)
    if especialidade is None and len(areas) == 1:
        especialidade = areas[0]
    contexto = {"areas": areas, "especialidade": especialidade, "form": form,
                "profissionais": [], "prof_id": None, "dias": [], "dia": None, "opcoes": {}}
    if especialidade is None:
        # Sem especialidade escolhida, a página mostra só o primeiro passo.
        return render_template("publico/agendar.html", **contexto), status

    todos = profissionais_online(especialidade["id"])
    escolhido = next((p for p in todos if str(p["id"]) == prof_arg), None)
    profs = [escolhido] if escolhido else todos
    hoje = date.today()
    dias = []
    for i in range(DIAS_ONLINE + 1):
        d = hoje + timedelta(days=i)
        livres = len(opcoes_do_dia(db, profs, d))
        if livres:
            dias.append({"data": d.isoformat(), "livres": livres})
    datas = {d["data"] for d in dias}
    dia = dia_arg if dia_arg in datas else (dias[0]["data"] if dias else None)
    contexto.update(
        profissionais=todos, prof_id=escolhido["id"] if escolhido else None, dias=dias, dia=dia,
        opcoes=opcoes_do_dia(db, profs, date.fromisoformat(dia)) if dia else {},
        planos=convenios_que_cobrem(especialidade["id"]),
    )
    return render_template("publico/agendar.html", **contexto), status


@app.get("/agendar")
def agendar():
    a = request.args
    return pagina_agendar(a.get("esp", ""), a.get("prof", ""), a.get("dia", ""), {})


@app.post("/agendar")
def agendar_enviar():
    f = request.form
    if f.get("site"):  # campo invisível: só robôs preenchem
        abort(400, "Não foi possível enviar a solicitação.")
    db = get_db()
    erros = []

    prof_txt, _, inicio_txt = f.get("horario", "").partition("|")
    esp_txt = f.get("esp", "")
    esp_id = int(esp_txt) if esp_txt.isdigit() else None
    prof = next((p for p in profissionais_online(esp_id) if str(p["id"]) == prof_txt), None)
    try:
        inicio = datetime.strptime(inicio_txt, FMT_DATAHORA)
    except ValueError:
        inicio = None
    if prof is None or inicio is None:
        erros.append("Escolha um dos horários disponíveis.")

    nome = f.get("paciente_nome", "").strip()
    responsavel = f.get("responsavel_nome", "").strip()
    telefone = so_digitos(f.get("telefone"))
    email = f.get("email", "").strip()
    motivo = f.get("motivo", "").strip()
    tipo = f.get("tipo", "")
    nasc = ler_data(f.get("data_nascimento")) if f.get("data_nascimento") else None
    if not nome or len(nome) > 120:
        erros.append("Informe o nome de quem será atendido.")
    if len(responsavel) > 120:
        erros.append("Nome do responsável muito longo.")
    if f.get("data_nascimento") and (nasc is None or nasc > date.today() or nasc.year < 1900):
        erros.append("Data de nascimento inválida.")
    if not 10 <= len(telefone) <= 11:
        erros.append("Informe um WhatsApp com DDD, por exemplo (11) 98765-4321.")
    if email and (len(email) > 120 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)):
        erros.append("E-mail inválido.")
    if tipo not in ("avaliacao", "terapia"):
        erros.append("Diga se é a primeira consulta ou se já é paciente.")
    if len(motivo) > 500:
        erros.append("O motivo aceita até 500 caracteres.")
    if not f.get("consentimento"):
        erros.append("É preciso concordar com o uso dos dados para o agendamento.")

    cobranca = f.get("cobranca", "particular")
    carteirinha = f.get("carteirinha", "").strip()
    convenio_id = None
    if cobranca != "particular":
        planos = {str(c["id"]) for c in convenios_que_cobrem(esp_id)} if esp_id else set()
        if cobranca not in planos:
            erros.append("Esse plano não cobre a especialidade escolhida. Escolha particular ou outro plano.")
        else:
            convenio_id = int(cobranca)
    if len(carteirinha) > 40:
        erros.append("Número da carteirinha muito longo.")

    agora = agora_txt()
    ip = request.remote_addr or ""
    if not erros:
        do_telefone = db.execute(
            "SELECT COUNT(*) FROM solicitacoes WHERE telefone = ? AND status = 'pendente' AND inicio >= ?",
            (telefone, agora),
        ).fetchone()[0]
        do_ip = db.execute(
            "SELECT COUNT(*) FROM solicitacoes WHERE ip = ? AND criado_em >= ?",
            (ip, (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()[0]
        if do_telefone >= LIMITE_SOLIC_TELEFONE:
            erros.append("Já existem solicitações deste telefone aguardando confirmação. "
                         "A clínica vai entrar em contato.")
        elif do_ip >= LIMITE_SOLIC_IP:
            erros.append("Muitas solicitações enviadas daqui hoje. Tente mais tarde ou fale com a clínica.")

    if not erros:
        # Trava a escrita para dois pacientes não pegarem o mesmo horário ao mesmo tempo.
        db.execute("BEGIN IMMEDIATE")
        if inicio_txt not in horarios_livres(db, prof, inicio.date()):
            db.rollback()
            erros.append("Esse horário acabou de ser ocupado. Escolha outro, por favor.")
        else:
            codigo = secrets.token_urlsafe(10)
            fim_txt = (inicio + timedelta(minutes=prof["duracao_online"])).strftime(FMT_DATAHORA)
            db.execute(
                """INSERT INTO solicitacoes (codigo, profissional_id, especialidade_id, inicio, fim, tipo,
                       convenio_id, carteirinha, paciente_nome, data_nascimento, responsavel_nome, telefone,
                       email, motivo, ip, consentimento_em)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (codigo, prof["id"], esp_id, inicio_txt, fim_txt, tipo, convenio_id,
                 (carteirinha or None) if convenio_id else None, nome,
                 nasc.isoformat() if nasc else None, responsavel or None, telefone, email or None,
                 motivo or None, ip, agora),
            )
            db.commit()
            return redirect(url_for("solicitacao_publica", codigo=codigo, novo=1))

    for erro in erros:
        flash(erro, "erro")
    return pagina_agendar(esp_txt, f.get("prof", ""), inicio_txt[:10], f, 400)


def carregar_solicitacao(where, valor):
    s = get_db().execute(SQL_SOLICITACOES + f" WHERE s.{where} = ?", (valor,)).fetchone()
    if s is None:
        abort(404)
    return s


@app.get("/agendar/<codigo>")
def solicitacao_publica(codigo):
    s = carregar_solicitacao("codigo", codigo)
    return render_template("publico/solicitacao.html", s=s, recem_criada=request.args.get("novo") == "1",
                           passou=s["inicio"] < agora_txt())


@app.post("/agendar/<codigo>/cancelar")
def solicitacao_publica_cancelar(codigo):
    s = carregar_solicitacao("codigo", codigo)
    db = get_db()
    cur = db.execute(
        "UPDATE solicitacoes SET status = 'cancelada', respondido_em = ? WHERE id = ? AND status = 'pendente'",
        (agora_txt(), s["id"]),
    )
    db.commit()
    if cur.rowcount:
        flash("Solicitação cancelada. O horário foi liberado.", "sucesso")
    else:
        flash("Esta solicitação já foi respondida pela clínica. Para desmarcar, fale com a clínica.", "erro")
    return redirect(url_for("solicitacao_publica", codigo=codigo))


# ---------------------------------------------------------------- solicitações (equipe)

def mensagem_resposta(s):
    primeiro_nome = s["paciente_nome"].split()[0]
    quando = f"{formatar_dia_semana(s['inicio']).lower()}, {formatar_data(s['inicio'])} às {s['inicio'][11:16]}"
    area = f" de {s['especialidade_nome']}" if s["especialidade_nome"] else ""
    if s["status"] == "aprovada":
        texto = (f"Olá! Aqui é da {NOME_CLINICA}. O atendimento{area} de {primeiro_nome} está confirmado para "
                 f"{quando}, com {s['profissional_nome']}. Qualquer imprevisto, é só avisar por aqui.")
    elif s["status"] == "recusada":
        texto = (f"Olá! Aqui é da {NOME_CLINICA}. Infelizmente não conseguimos confirmar o horário de "
                 f"{quando}." + (f" {s['resposta']}" if s["resposta"] else "")
                 + f" Você pode escolher outro horário em {url_for('agendar', _external=True)}")
    else:
        return None
    return link_whatsapp(s["telefone"], texto)


@app.get("/solicitacoes")
@requer()
def solicitacoes():
    filtro = request.args.get("status", "pendente")
    agora = agora_txt()
    if filtro == "pendente":
        lista = get_db().execute(
            SQL_SOLICITACOES + " WHERE s.status = 'pendente' AND s.inicio >= ? ORDER BY s.inicio", (agora,)
        ).fetchall()
    elif filtro in STATUS_SOLICITACAO:
        lista = get_db().execute(
            SQL_SOLICITACOES + " WHERE s.status = ? ORDER BY s.criado_em DESC LIMIT 200", (filtro,)
        ).fetchall()
    else:
        filtro = "todas"
        lista = get_db().execute(SQL_SOLICITACOES + " ORDER BY s.criado_em DESC LIMIT 200").fetchall()
    return render_template("solicitacoes.html", solicitacoes=lista, filtro=filtro, agora=agora)


def limpar_telefone_sql(coluna):
    return f"replace(replace(replace(replace({coluna}, ' ', ''), '-', ''), '(', ''), ')', '')"


@app.get("/solicitacoes/<int:solicitacao_id>")
@requer()
def solicitacao(solicitacao_id):
    s = carregar_solicitacao("id", solicitacao_id)
    final_tel = s["telefone"][-8:]
    parecidos = get_db().execute(
        f"""SELECT id, nome, data_nascimento, responsavel_nome, telefone, responsavel_telefone, ativo
            FROM pacientes WHERE nome LIKE ? OR {limpar_telefone_sql('telefone')} LIKE ?
               OR {limpar_telefone_sql('responsavel_telefone')} LIKE ?
            ORDER BY ativo DESC, nome LIMIT 10""",
        (f"%{s['paciente_nome']}%", f"%{final_tel}%", f"%{final_tel}%"),
    ).fetchall()
    return render_template("solicitacao.html", s=s, parecidos=parecidos, passou=s["inicio"] < agora_txt(),
                           link_resposta=mensagem_resposta(s))


@app.post("/solicitacoes/<int:solicitacao_id>/aprovar")
@requer()
def solicitacao_aprovar(solicitacao_id):
    s = carregar_solicitacao("id", solicitacao_id)
    voltar = redirect(url_for("solicitacao", solicitacao_id=solicitacao_id))
    if s["status"] != "pendente":
        flash("Esta solicitação já foi respondida.", "erro")
        return voltar
    db = get_db()
    escolha = request.form.get("paciente_id", "")
    criou_paciente = False
    if escolha == "novo":
        telefone = formatar_telefone(s["telefone"])
        cur = db.execute(
            """INSERT INTO pacientes (nome, data_nascimento, responsavel_nome, telefone, responsavel_telefone,
                   email, profissional_id, queixa_principal, convenio_id, carteirinha)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (s["paciente_nome"], s["data_nascimento"], s["responsavel_nome"],
             None if s["responsavel_nome"] else telefone, telefone if s["responsavel_nome"] else None,
             s["email"], s["profissional_id"], s["motivo"], s["convenio_id"], s["carteirinha"]),
        )
        paciente_id = cur.lastrowid
        criou_paciente = True
    elif escolha.isdigit() and db.execute("SELECT 1 FROM pacientes WHERE id = ?", (escolha,)).fetchone():
        paciente_id = int(escolha)
        if s["convenio_id"]:
            # Paciente sem plano no cadastro passa a ter o informado no pedido; um plano já cadastrado fica.
            db.execute("UPDATE pacientes SET convenio_id = ?, carteirinha = COALESCE(?, carteirinha) "
                       "WHERE id = ? AND convenio_id IS NULL", (s["convenio_id"], s["carteirinha"], paciente_id))
    else:
        flash("Escolha um paciente já cadastrado ou cadastre um novo.", "erro")
        return voltar

    # Muda o status antes de checar conflito, para a própria solicitação não contar como choque.
    agora = agora_txt()
    cur = db.execute(
        "UPDATE solicitacoes SET status = 'aprovada', respondido_em = ?, respondido_por = ? "
        "WHERE id = ? AND status = 'pendente'",
        (agora, g.usuario["id"], solicitacao_id),
    )
    choque = None if cur.rowcount == 1 else "a solicitação foi respondida por outra pessoa."
    choque = choque or conflito(db, s["profissional_id"], paciente_id, s["inicio"], s["fim"])
    if choque:
        db.rollback()
        flash("Não foi possível confirmar: " + choque, "erro")
        return voltar
    esp_id = s["especialidade_id"] or especialidade_padrao(s["profissional_id"])
    plano = cobertura(db, s["convenio_id"], esp_id) if s["convenio_id"] else None
    if s["convenio_id"] and plano is None:
        db.rollback()
        flash(f"O plano {s['convenio_nome']} não cobre mais {s['especialidade_nome']}. Ajuste a cobertura em "
              "Convênios ou agende como particular pela agenda.", "erro")
        return voltar
    if plano:
        valor = plano["valor_centavos"]
    else:
        valor = db.execute("SELECT valor_sessao_centavos FROM pacientes WHERE id = ?", (paciente_id,)).fetchone()[0]
    cur = db.execute(
        """INSERT INTO agendamentos (paciente_id, profissional_id, especialidade_id, inicio, fim, tipo, status,
               valor_centavos, convenio_id, observacao, criado_por)
           VALUES (?, ?, ?, ?, ?, ?, 'confirmado', ?, ?, 'Agendado pelo site', ?)""",
        (paciente_id, s["profissional_id"], esp_id, s["inicio"], s["fim"], s["tipo"], valor, s["convenio_id"],
         g.usuario["id"]),
    )
    db.execute("UPDATE solicitacoes SET agendamento_id = ? WHERE id = ?", (cur.lastrowid, solicitacao_id))
    db.commit()
    flash("Horário confirmado e lançado na agenda. Avise o paciente pelo WhatsApp.", "sucesso")
    if criou_paciente:
        flash("Paciente cadastrado com os dados do pedido. Complete o cadastro (valor da sessão particular, "
              "carteirinha).", "info")
    return voltar


@app.post("/solicitacoes/<int:solicitacao_id>/recusar")
@requer()
def solicitacao_recusar(solicitacao_id):
    resposta = request.form.get("resposta", "").strip()[:300]
    db = get_db()
    cur = db.execute(
        "UPDATE solicitacoes SET status = 'recusada', resposta = ?, respondido_em = ?, respondido_por = ? "
        "WHERE id = ? AND status = 'pendente'",
        (resposta or None, agora_txt(), g.usuario["id"], solicitacao_id),
    )
    db.commit()
    if cur.rowcount:
        flash("Solicitação recusada e horário liberado. Avise o paciente pelo WhatsApp.", "sucesso")
    else:
        flash("Esta solicitação já foi respondida.", "erro")
    return redirect(url_for("solicitacao", solicitacao_id=solicitacao_id))


# ---------------------------------------------------------------- horário de atendimento

TURNOS = (1, 2)


@app.route("/horarios", methods=["GET", "POST"])
@requer()
def horarios():
    if g.usuario["papel"] in ("admin", "recepcao"):
        editaveis = profissionais()
    elif g.usuario["atende"]:
        editaveis = [p for p in profissionais() if p["id"] == g.usuario["id"]]
    else:
        abort(403)
    if not editaveis:
        return render_template("horarios.html", editaveis=[], prof=None, form={})
    prof_id = request.values.get("prof", type=int)
    prof = next((p for p in editaveis if p["id"] == prof_id), editaveis[0])
    db = get_db()

    if request.method == "POST":
        f = request.form
        erros, novos = [], []
        for dia in range(7):
            do_dia = []
            for turno in TURNOS:
                ini_txt = f.get(f"h{dia}_{turno}_ini", "").strip()
                fim_txt = f.get(f"h{dia}_{turno}_fim", "").strip()
                if not ini_txt and not fim_txt:
                    continue
                ini, fim = ler_hora(ini_txt), ler_hora(fim_txt)
                if ini is None or fim is None or ini >= fim:
                    erros.append(f"{DIAS_SEMANA[dia]}: preencha início e fim, com o fim depois do início.")
                    continue
                do_dia.append((ini.strftime("%H:%M"), fim.strftime("%H:%M")))
            do_dia.sort()
            for a, b in zip(do_dia, do_dia[1:]):
                if b[0] < a[1]:
                    erros.append(f"{DIAS_SEMANA[dia]}: os turnos se sobrepõem.")
            novos += [(dia, ini, fim) for ini, fim in do_dia]
        try:
            duracao = int(f.get("duracao_online", ""))
            if duracao not in DURACOES:
                raise ValueError
        except ValueError:
            erros.append("Duração inválida.")
            duracao = 45
        online = 1 if f.get("agenda_online") else 0
        if online and not novos:
            erros.append("Para aparecer no agendamento online, cadastre pelo menos um horário.")
        if not erros:
            db.execute("DELETE FROM horarios WHERE profissional_id = ?", (prof["id"],))
            db.executemany(
                "INSERT INTO horarios (profissional_id, dia_semana, inicio, fim) VALUES (?, ?, ?, ?)",
                [(prof["id"], *n) for n in novos],
            )
            db.execute("UPDATE usuarios SET agenda_online = ?, duracao_online = ? WHERE id = ?",
                       (online, duracao, prof["id"]))
            db.commit()
            flash(f"Horários de {prof['nome']} salvos.", "sucesso")
            return redirect(url_for("horarios", prof=prof["id"]))
        for erro in erros:
            flash(erro, "erro")
        return render_template("horarios.html", editaveis=editaveis, prof=prof, form=f), 400

    form = {"agenda_online": "1" if prof["agenda_online"] else "", "duracao_online": str(prof["duracao_online"])}
    contagem = {}
    for h in db.execute("SELECT * FROM horarios WHERE profissional_id = ? ORDER BY dia_semana, inicio",
                        (prof["id"],)):
        turno = contagem[h["dia_semana"]] = contagem.get(h["dia_semana"], 0) + 1
        if turno in TURNOS:
            form[f"h{h['dia_semana']}_{turno}_ini"] = h["inicio"]
            form[f"h{h['dia_semana']}_{turno}_fim"] = h["fim"]
    return render_template("horarios.html", editaveis=editaveis, prof=prof, form=form)


# ---------------------------------------------------------------- erros

@app.errorhandler(404)
def nao_encontrado(_e):
    return render_template("erro.html", titulo="Página não encontrada",
                           mensagem="O endereço que você abriu não existe."), 404


@app.errorhandler(400)
def requisicao_invalida(e):
    return render_template("erro.html", titulo="Não foi possível concluir",
                           mensagem=e.description), 400


@app.errorhandler(403)
def proibido(e):
    mensagem = e.description
    if not mensagem or mensagem.startswith("You don't"):
        mensagem = "Seu usuário não tem acesso a esta área."
    return render_template("erro.html", titulo="Acesso restrito", mensagem=mensagem), 403


init_db()

if __name__ == "__main__":
    # CLINICA_HOST=0.0.0.0 libera o acesso por outros computadores e celulares da mesma rede.
    host = os.environ.get("CLINICA_HOST", "127.0.0.1")
    porta = int(os.environ.get("PORT", "5001"))
    print(f"\n  Equipe:    http://127.0.0.1:{porta}\n  Pacientes: http://127.0.0.1:{porta}/agendar\n")
    app.run(host=host, port=porta, debug=os.environ.get("FLASK_DEBUG") == "1")
