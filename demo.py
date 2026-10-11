"""Preenche o banco com a equipe da clínica e pacientes fictícios para experimentar o sistema.

Uso: python demo.py   (só roda com o banco vazio de pacientes)
Senha de todos os usuários de demonstração: demo1234
"""

import sqlite3
from datetime import date, datetime, timedelta

from werkzeug.security import generate_password_hash

import app  # cria o banco, as especialidades e o administrador, se ainda não existirem

SENHA = "demo1234"

# nome, e-mail, especialidades, cor, dias da semana (0 = segunda), turnos, duração online
EQUIPE = [
    ("Douglas Puppim", "douglas@clinica.local", ["Fonoaudiologia"], "#2f7d6d",
     range(0, 5), [("08:00", "12:00"), ("13:30", "17:30")], 45),
    ("Nádia", "nadia@clinica.local", ["Psicologia", "Psicomotricidade"], "#a4508b",
     (0, 2, 4), [("08:00", "12:00"), ("14:00", "18:00")], 50),
    ("Sarah", "sarah@clinica.local", ["Psicologia"], "#3b6fb6",
     (1, 3), [("13:00", "19:00")], 50),
    ("Emilly", "emilly@clinica.local", ["Nutrição"], "#c0672c",
     (1, 3, 5), [("08:00", "12:00")], 60),
    ("Julio César", "julio@clinica.local", ["Musicoterapia"], "#6b8e23",
     (0, 2), [("14:00", "18:00")], 45),
]
RECEPCAO = ("Recepção", "recepcao@clinica.local")

# nome, nascimento, responsável, parentesco, telefone, convênio, profissional (índice), especialidade,
# valor, queixa, hipótese
PACIENTES = [
    ("Miguel Santos", "2021-03-14", "Juliana Santos", "Mãe", "(11) 98888-1001", None, 0, "Fonoaudiologia",
     15000, "Fala pouco para a idade; usa gestos para pedir as coisas.", "Atraso de linguagem expressiva"),
    ("Helena Costa", "2019-08-02", "Rafael Costa", "Pai", "(11) 98888-1002", "Klini", 0, "Fonoaudiologia",
     12000, "Troca sons na fala (fala 'tasa' para 'casa').", "Transtorno dos sons da fala"),
    ("Arthur Oliveira", "2017-11-20", "Patrícia Oliveira", "Mãe", "(11) 98888-1003", None, 1,
     "Psicomotricidade", 14000, "Desajeitado nas brincadeiras, dificuldade com coordenação.",
     "Atraso no desenvolvimento psicomotor"),
    ("Laura Mendes", "2015-05-09", "Cláudia Mendes", "Mãe", "(11) 98888-1004", "Appai", 2,
     "Psicologia", 16000, "Ansiedade antes das provas e dificuldade para dormir.", None),
    ("Roberto Almeida", "1956-02-17", "Sônia Almeida", "Esposa", "(11) 98888-1005", None, 0, "Fonoaudiologia",
     18000, "Dificuldade para encontrar palavras após AVC.", "Afasia de expressão pós-AVC"),
    ("Fernanda Rocha", "1988-07-30", None, None, "(11) 98888-1006", None, 3, "Nutrição",
     20000, "Quer reeducação alimentar; cansaço frequente.", None),
    ("Davi Lima", "2018-09-12", "Marcos Lima", "Pai", "(11) 98888-1007", "Assim", 4, "Musicoterapia",
     15000, "Autismo nível 1; interação social e regulação emocional.", "TEA"),
    ("Sofia Nunes", "2016-01-25", "Paula Nunes", "Mãe", "(11) 98888-1008", None, 1, "Psicologia",
     16000, "Mudança de comportamento após separação dos pais.", None),
]

# Valores fictícios que cada plano paga por sessão (em centavos), iguais para todas as áreas.
VALORES_PLANO = {"Assim": 8000, "Klini": 9000, "Appai": 7500}

EVOLUCAO = (
    "Objetivos trabalhados: {obj}\n"
    "Procedimentos: atividades dirigidas conforme o plano terapêutico.\n"
    "Resposta do paciente: colaborativo, boa adesão às propostas.\n"
    "Orientações: reforçar em casa o que foi combinado.\n"
    "Plano: manter objetivos na próxima sessão."
)


def main():
    db = sqlite3.connect(app.DB_PATH)
    db.row_factory = sqlite3.Row
    if db.execute("SELECT COUNT(*) FROM pacientes").fetchone()[0]:
        print("O banco já tem pacientes; nada foi feito. Apague a pasta instance/ para recriar a demonstração.")
        return

    especialidades = {r["nome"]: r["id"] for r in db.execute("SELECT id, nome FROM especialidades")}
    planos = {r["nome"]: r["id"] for r in db.execute("SELECT id, nome FROM convenios")}
    for nome, valor in VALORES_PLANO.items():
        db.execute("UPDATE convenio_especialidades SET valor_centavos = ? WHERE convenio_id = ?", (valor, planos[nome]))
    senha_hash = generate_password_hash(SENHA)
    ids_prof = []
    for nome, email, areas, cor, dias, turnos, duracao in EQUIPE:
        cur = db.execute(
            "INSERT INTO usuarios (nome, email, senha_hash, papel, atende, cor, agenda_online, duracao_online) "
            "VALUES (?, ?, ?, 'profissional', 1, ?, 1, ?)",
            (nome, email, senha_hash, cor, duracao),
        )
        uid = cur.lastrowid
        ids_prof.append((uid, duracao))
        db.executemany("INSERT INTO profissional_especialidades (usuario_id, especialidade_id) VALUES (?, ?)",
                       [(uid, especialidades[a]) for a in areas])
        db.executemany("INSERT INTO horarios (profissional_id, dia_semana, inicio, fim) VALUES (?, ?, ?, ?)",
                       [(uid, d, ini, fim) for d in dias for ini, fim in turnos])
    db.execute("INSERT INTO usuarios (nome, email, senha_hash, papel) VALUES (?, ?, ?, 'recepcao')",
               (*RECEPCAO, senha_hash))

    hoje = date.today()
    segunda = hoje - timedelta(days=hoje.weekday())
    for i, (nome, nasc, resp, parent, tel, conv, prof, area, valor, queixa, hipotese) in enumerate(PACIENTES):
        prof_id, duracao = ids_prof[prof]
        esp_id = especialidades[area]
        plano_id = planos.get(conv)
        carteirinha = f"{900000 + i * 1371:08d}" if plano_id else None
        cur = db.execute(
            """INSERT INTO pacientes (nome, data_nascimento, responsavel_nome, responsavel_parentesco,
                   responsavel_telefone, telefone, convenio_id, carteirinha, profissional_id,
                   valor_sessao_centavos, queixa_principal, hipotese_diagnostica)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (nome, nasc, resp, parent, tel if resp else None, None if resp else tel, plano_id, carteirinha,
             prof_id, valor, queixa, hipotese),
        )
        pid = cur.lastrowid

        # Primeiro horário de atendimento do profissional, deslocado para não colidir entre pacientes.
        dia_semana, inicio_turno = db.execute(
            "SELECT dia_semana, inicio FROM horarios WHERE profissional_id = ? ORDER BY dia_semana, inicio "
            "LIMIT 1 OFFSET ?", (prof_id, i % 2),
        ).fetchone()
        base = datetime.strptime(inicio_turno, "%H:%M") + timedelta(minutes=duracao * (i // 2 % 3))
        for semana in range(-3, 3):
            dia = segunda + timedelta(weeks=semana, days=dia_semana)
            inicio = datetime.combine(dia, base.time())
            fim = inicio + timedelta(minutes=duracao)
            if inicio < datetime.now() - timedelta(hours=1):
                status = "falta" if (i + semana) % 7 == 0 else "realizado"
            else:
                status = "confirmado" if semana == 0 else "agendado"
            tipo = "avaliacao" if semana == -3 else "terapia"
            # Sessões pelo plano: as mais antigas já faturadas/pagas, as recentes a faturar.
            faturamento = None
            if plano_id and status == "realizado":
                faturamento = {-3: "pago", -2: "faturado"}.get(semana)
            cur = db.execute(
                """INSERT INTO agendamentos (paciente_id, profissional_id, especialidade_id, inicio, fim, tipo,
                       status, valor_centavos, convenio_id, guia, faturamento)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pid, prof_id, esp_id, inicio.strftime("%Y-%m-%d %H:%M"), fim.strftime("%Y-%m-%d %H:%M"),
                 tipo, status, VALORES_PLANO[conv] if plano_id else valor, plano_id,
                 f"G{dia:%m%d}{i}" if plano_id and semana < -1 else None, faturamento),
            )
            # Deixa a sessão mais recente sem evolução, para aparecer como pendente.
            if status == "realizado" and semana < -1:
                db.execute(
                    "INSERT INTO evolucoes (paciente_id, agendamento_id, profissional_id, especialidade_id, "
                    "data, texto, sigilosa) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (pid, cur.lastrowid, prof_id, esp_id, dia.isoformat(),
                     EVOLUCAO.format(obj=(hipotese or queixa).lower()), 1 if area == "Psicologia" else 0),
                )
        if i % 2 == 0 and not plano_id:
            db.execute(
                "INSERT INTO pagamentos (paciente_id, data, valor_centavos, forma, descricao) "
                "VALUES (?, ?, ?, ?, ?)",
                (pid, (hoje - timedelta(days=10)).isoformat(), valor * 2, "pix", "Sessões anteriores"),
            )

    db.commit()
    db.close()
    print("Dados de demonstração criados.")
    print("Logins (senha " + SENHA + "): " + ", ".join(e[1] for e in EQUIPE) + ", " + RECEPCAO[1])


if __name__ == "__main__":
    main()
