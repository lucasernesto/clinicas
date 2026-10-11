CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY,
    nome TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    senha_hash TEXT NOT NULL,
    papel TEXT NOT NULL CHECK (papel IN ('admin', 'profissional', 'recepcao')),
    atende INTEGER NOT NULL DEFAULT 0,          -- 1 = atende pacientes e acessa o prontuário
    registro TEXT,                              -- CRFa, CRP, CRN...
    cor TEXT NOT NULL DEFAULT '#2f7d6d',
    ativo INTEGER NOT NULL DEFAULT 1,
    trocar_senha INTEGER NOT NULL DEFAULT 0,
    agenda_online INTEGER NOT NULL DEFAULT 0,   -- 1 = aparece na página pública de agendamento
    duracao_online INTEGER NOT NULL DEFAULT 45, -- duração dos horários oferecidos online (min)
    falhas_login INTEGER NOT NULL DEFAULT 0,
    bloqueado_ate TEXT,
    criado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS especialidades (
    id INTEGER PRIMARY KEY,
    nome TEXT NOT NULL UNIQUE COLLATE NOCASE,
    ativo INTEGER NOT NULL DEFAULT 1
);

-- Um profissional pode ter mais de uma especialidade (ex.: psicologia e psicomotricidade).
CREATE TABLE IF NOT EXISTS profissional_especialidades (
    usuario_id INTEGER NOT NULL REFERENCES usuarios (id),
    especialidade_id INTEGER NOT NULL REFERENCES especialidades (id),
    PRIMARY KEY (usuario_id, especialidade_id)
);

-- Planos de saúde aceitos pela clínica.
CREATE TABLE IF NOT EXISTS convenios (
    id INTEGER PRIMARY KEY,
    nome TEXT NOT NULL UNIQUE COLLATE NOCASE,
    ativo INTEGER NOT NULL DEFAULT 1
);

-- Especialidades que cada plano cobre e quanto paga por sessão. Sem linha = o plano não cobre.
CREATE TABLE IF NOT EXISTS convenio_especialidades (
    convenio_id INTEGER NOT NULL REFERENCES convenios (id),
    especialidade_id INTEGER NOT NULL REFERENCES especialidades (id),
    valor_centavos INTEGER NOT NULL DEFAULT 0 CHECK (valor_centavos >= 0),
    PRIMARY KEY (convenio_id, especialidade_id)
);

CREATE TABLE IF NOT EXISTS pacientes (
    id INTEGER PRIMARY KEY,
    nome TEXT NOT NULL,
    data_nascimento TEXT,
    cpf TEXT,
    telefone TEXT,
    email TEXT,
    responsavel_nome TEXT,
    responsavel_parentesco TEXT,
    responsavel_telefone TEXT,
    endereco TEXT,
    convenio_id INTEGER REFERENCES convenios (id),  -- vazio = particular
    carteirinha TEXT,
    profissional_id INTEGER REFERENCES usuarios (id),
    valor_sessao_centavos INTEGER NOT NULL DEFAULT 0 CHECK (valor_sessao_centavos >= 0),
    observacoes TEXT,                           -- administrativas, visíveis à recepção
    queixa_principal TEXT,                      -- clínicas, só para quem atende
    hipotese_diagnostica TEXT,
    anamnese TEXT,
    ativo INTEGER NOT NULL DEFAULT 1,
    criado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- inicio e fim no formato 'AAAA-MM-DD HH:MM', o que permite comparar como texto.
CREATE TABLE IF NOT EXISTS agendamentos (
    id INTEGER PRIMARY KEY,
    paciente_id INTEGER NOT NULL REFERENCES pacientes (id),
    profissional_id INTEGER NOT NULL REFERENCES usuarios (id),
    especialidade_id INTEGER REFERENCES especialidades (id),
    inicio TEXT NOT NULL,
    fim TEXT NOT NULL,
    tipo TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'agendado',
    valor_centavos INTEGER NOT NULL DEFAULT 0 CHECK (valor_centavos >= 0),
    convenio_id INTEGER REFERENCES convenios (id),  -- vazio = particular, cobrado do paciente
    guia TEXT,                                  -- nº da guia / autorização do plano
    faturamento TEXT,                           -- sessões por plano: vazio = a faturar, faturado, pago, glosado
    observacao TEXT,
    serie TEXT,                                 -- agrupa sessões criadas pela repetição semanal
    criado_por INTEGER REFERENCES usuarios (id),
    criado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    atualizado_em TEXT
);
CREATE INDEX IF NOT EXISTS idx_agend_prof ON agendamentos (profissional_id, inicio);
CREATE INDEX IF NOT EXISTS idx_agend_pac ON agendamentos (paciente_id, inicio);
CREATE INDEX IF NOT EXISTS idx_agend_inicio ON agendamentos (inicio);
CREATE INDEX IF NOT EXISTS idx_agend_convenio ON agendamentos (convenio_id, inicio);

CREATE TABLE IF NOT EXISTS evolucoes (
    id INTEGER PRIMARY KEY,
    paciente_id INTEGER NOT NULL REFERENCES pacientes (id),
    agendamento_id INTEGER UNIQUE REFERENCES agendamentos (id),
    profissional_id INTEGER NOT NULL REFERENCES usuarios (id),
    especialidade_id INTEGER REFERENCES especialidades (id),
    data TEXT NOT NULL,
    texto TEXT NOT NULL,
    sigilosa INTEGER NOT NULL DEFAULT 0,        -- 1 = só o autor lê o conteúdo
    criado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    editado_em TEXT
);
CREATE INDEX IF NOT EXISTS idx_evol_pac ON evolucoes (paciente_id, data);

CREATE TABLE IF NOT EXISTS pagamentos (
    id INTEGER PRIMARY KEY,
    paciente_id INTEGER NOT NULL REFERENCES pacientes (id),
    data TEXT NOT NULL,
    valor_centavos INTEGER NOT NULL CHECK (valor_centavos > 0),
    forma TEXT NOT NULL,
    descricao TEXT,
    registrado_por INTEGER REFERENCES usuarios (id),
    criado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_pag_pac ON pagamentos (paciente_id, data);
CREATE INDEX IF NOT EXISTS idx_pag_data ON pagamentos (data);

-- Horário de atendimento semanal de cada profissional (0 = segunda ... 6 = domingo), 'HH:MM'.
CREATE TABLE IF NOT EXISTS horarios (
    id INTEGER PRIMARY KEY,
    profissional_id INTEGER NOT NULL REFERENCES usuarios (id),
    dia_semana INTEGER NOT NULL CHECK (dia_semana BETWEEN 0 AND 6),
    inicio TEXT NOT NULL,
    fim TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_horarios_prof ON horarios (profissional_id, dia_semana);

-- Pedidos feitos pela página pública. Enquanto pendentes, seguram o horário.
CREATE TABLE IF NOT EXISTS solicitacoes (
    id INTEGER PRIMARY KEY,
    codigo TEXT NOT NULL UNIQUE,
    profissional_id INTEGER NOT NULL REFERENCES usuarios (id),
    especialidade_id INTEGER REFERENCES especialidades (id),
    inicio TEXT NOT NULL,
    fim TEXT NOT NULL,
    tipo TEXT NOT NULL,
    convenio_id INTEGER REFERENCES convenios (id),
    carteirinha TEXT,
    paciente_nome TEXT NOT NULL,
    data_nascimento TEXT,
    responsavel_nome TEXT,
    telefone TEXT NOT NULL,
    email TEXT,
    motivo TEXT,
    status TEXT NOT NULL DEFAULT 'pendente'
        CHECK (status IN ('pendente', 'aprovada', 'recusada', 'cancelada')),
    resposta TEXT,
    agendamento_id INTEGER REFERENCES agendamentos (id),
    ip TEXT,
    consentimento_em TEXT NOT NULL,
    criado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    respondido_em TEXT,
    respondido_por INTEGER REFERENCES usuarios (id)
);
CREATE INDEX IF NOT EXISTS idx_solic_prof ON solicitacoes (profissional_id, status, inicio);
