# Clínica multiprofissional

Sistema da clínica (fonoaudiologia, psicologia, psicomotricidade, nutrição e musicoterapia), com uma página pública de agendamento e a área da equipe:

- **Agendamento online** (`/agendar`, sem login): o paciente escolhe a especialidade, o profissional (ou "sem preferência"), o dia e um horário livre, e informa nome, WhatsApp e, se quiser, o motivo. O pedido fica aguardando a clínica, e o paciente acompanha por um link próprio.
- **Solicitações**: pedidos do site para a equipe confirmar (vinculando a um paciente existente ou cadastrando um novo) ou recusar, com mensagem pronta no WhatsApp.
- **Horários**: horário de atendimento semanal de cada profissional, que define o que aparece online.
- **Hoje**: atendimentos do dia, sessões de amanhã a confirmar (com mensagem pronta no WhatsApp), evoluções pendentes e sessões passadas sem desfecho.
- **Agenda**: semana por profissional ou de todos, com a especialidade de cada sessão, troca rápida de status (agendado, confirmado, realizado, falta, falta justificada, cancelado), repetição semanal e aviso de conflito de horário.
- **Pacientes**: cadastro com responsável (para crianças), convênio e valor da sessão; ficha com as áreas em que o paciente é atendido, histórico de sessões, frequência e saldo.
- **Prontuário multiprofissional**: queixa, hipótese diagnóstica, anamnese e evoluções por sessão, com a especialidade e a assinatura de quem escreveu. Evoluções podem ser marcadas como sigilosas. Versão para imprimir ou salvar em PDF.
- **Financeiro**: pagamentos por paciente (Pix, dinheiro, cartão, transferência), extrato e, para administração e recepção, o resumo do mês (recebido, produzido por profissional, por especialidade e por particular/plano, pacientes em aberto).
- **Planos de saúde**: Assim, Klini e Appai, com as especialidades que cada um cobre e o valor pago por sessão. Cada sessão é particular ou pelo plano, com nº da guia. A tela **Convênios** controla o faturamento das sessões pelo plano: a faturar → faturado → pago ou glosado.
- **Usuários e especialidades**: contas individuais com papel (administração, profissional de saúde, recepção) e as especialidades de cada profissional.

Feito em Python com Flask e SQLite, no mesmo estilo da mercearia: HTML, CSS e um pouco de JavaScript, sem build.

## Como rodar

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Abra http://127.0.0.1:5001 para a equipe e http://127.0.0.1:5001/agendar para a página dos pacientes. Na primeira execução é criado o banco `instance/clinica.db`, as cinco especialidades e o usuário `admin@clinica.local` com senha `admin123`, que precisa ser trocada no primeiro acesso.

### Dados de demonstração

```powershell
python demo.py
```

Cria a equipe da clínica com horários de atendimento, uma recepcionista, oito pacientes fictícios e algumas semanas de sessões, evoluções e pagamentos:

| Profissional | Especialidades | Login |
|---|---|---|
| Douglas Puppim | Fonoaudiologia | `douglas@clinica.local` |
| Nádia | Psicologia, Psicomotricidade | `nadia@clinica.local` |
| Sarah | Psicologia | `sarah@clinica.local` |
| Emilly | Nutrição | `emilly@clinica.local` |
| Julio César | Musicoterapia | `julio@clinica.local` |
| Recepção | — | `recepcao@clinica.local` |

Senha de todos: `demo1234`. Na demonstração, Helena é Klini, Laura é Appai e Davi é Assim; os valores dos planos (R$ 75 a R$ 90) são fictícios. **Antes de usar com pacientes reais, apague a pasta `instance/`** para começar do zero e cadastre a equipe em Usuários com os e-mails de verdade.

## Quem vê o quê

| | Administração | Profissional de saúde | Recepção |
|---|---|---|---|
| Agenda e cadastro de pacientes | sim | sim | sim |
| Pagamentos do paciente | sim | sim | sim |
| Prontuário (anamnese, evoluções) | só se "atende" | sim | não |
| Conteúdo de evolução sigilosa | só o autor | só o autor | não |
| Resumo financeiro do mês | sim | não | sim |
| Faturamento de convênios | sim | não | sim |
| Usuários, especialidades, planos e valores, excluir pagamento | sim | não | não |

"Atende" é uma opção do usuário: quem atende aparece na agenda e acessa o prontuário, e precisa ter ao menos uma especialidade. Um administrador que também atende marca essa opção.

## Regras

- Cada profissional tem uma ou mais especialidades. Todo agendamento registra profissional **e** especialidade (a Nádia, por exemplo, atende em Psicologia ou em Psicomotricidade), e a evolução herda a especialidade da sessão.
- O prontuário é único por paciente e compartilhado entre quem atende. A evolução marcada como **sigilosa** mostra aos demais só data, área e autor; o texto fica visível apenas para quem escreveu, inclusive na impressão. Útil para registros de psicologia que não devem circular na equipe.
- Um profissional não pode ter dois atendimentos no mesmo horário, nem um paciente estar em duas sessões ao mesmo tempo (vale entre especialidades). Cancelamentos e faltas justificadas liberam o horário.
- Na repetição semanal, datas com conflito são puladas e listadas; as demais são agendadas. Dá para cancelar de uma vez as próximas sessões de uma série.
- O valor de cada sessão é copiado do cadastro do paciente no agendamento (pode ser alterado). Mudar o valor do paciente não altera sessões já marcadas.
- Entram na cobrança do paciente as sessões **particulares realizadas** e as **faltas particulares sem justificativa**. Para não cobrar faltas, defina `CLINICA_COBRAR_FALTA=0`.
- Sessões pelo plano não entram no saldo do paciente: vão para o faturamento do convênio. Faltas pelo plano não são cobradas de ninguém.
- Ao agendar, "conforme o cadastro" usa o plano do paciente. O valor da sessão vem do plano (tela Planos de saúde) ou, se particular, do cadastro do paciente; dá para digitar outro. Se o plano não cobre a especialidade, o sistema não deixa agendar por ele.
- No agendamento online, o paciente só vê os planos que cobrem a especialidade escolhida e informa a carteirinha. Ao confirmar, o plano vai para a sessão e, se o cadastro do paciente estava sem plano, para o cadastro também.
- Uma sessão com evolução registrada não pode mudar de "realizado".
- Só quem escreveu uma evolução pode editá-la, e a data da edição fica registrada. Evoluções não são apagadas.
- Pacientes não são excluídos, só inativados (alta ou desistência), para preservar o prontuário. Especialidades também só são desativadas.
- Agendamento online: só aparecem especialidades com algum profissional que tenha "Aceitar agendamento online" ligado e horário cadastrado. Os horários são gerados pela duração escolhida dentro de cada turno, sem os já ocupados, a partir de 12 h de antecedência e até 21 dias à frente.
- Um pedido online pendente segura o horário: ninguém mais consegue pedir nem agendar manualmente por cima. Recusar, ou o próprio paciente cancelar, libera o horário. Ao confirmar, a sessão entra na agenda como "confirmado", na especialidade pedida.
- A página pública nunca mostra nomes de pacientes, só horários livres. O formulário tem campo-armadilha contra robôs e aceita no máximo 2 pedidos pendentes por telefone e 5 por IP em 24 h.
- Login: 5 senhas erradas seguidas bloqueiam o usuário por 15 minutos. Trocar a senha encerra as sessões abertas com a senha antiga.

## Configuração (variáveis de ambiente)

| Variável | Padrão | Uso |
|---|---|---|
| `CLINICA_NOME` | `Clínica` | Nome no topo, na impressão e nas mensagens de WhatsApp. |
| `CLINICA_ADMIN_EMAIL` | `admin@clinica.local` | E-mail do primeiro administrador (só na criação do banco). |
| `CLINICA_ADMIN_SENHA` | `admin123` | Senha inicial dele; a troca é obrigatória no primeiro acesso. |
| `CLINICA_COBRAR_FALTA` | `1` | `0` deixa as faltas fora da cobrança. |
| `CLINICA_WHATSAPP` | vazio | WhatsApp da clínica; mostra "Falar com a clínica" na página pública. |
| `CLINICA_ANTECEDENCIA_HORAS` | `12` | Antecedência mínima para pedir horário online. |
| `CLINICA_DIAS_ONLINE` | `21` | Até quantos dias à frente a página pública oferece horários. |
| `CLINICA_SECRET_KEY` | gerada em `instance/secret_key` | Chave das sessões. |
| `PORT` | `5001` | Porta do servidor de desenvolvimento. |

## Estrutura

```
app.py              rotas, permissões, validações e regras (migra bancos antigos ao iniciar)
schema.sql          tabelas: usuarios, especialidades, profissional_especialidades, convenios,
                    convenio_especialidades, pacientes, agendamentos, evolucoes, pagamentos,
                    horarios, solicitacoes
demo.py             equipe da clínica e pacientes fictícios para experimentar
templates/          páginas da equipe (_macros.html tem o cartão de agendamento)
templates/publico/  páginas do agendamento online
static/style.css    visual
static/app.js       envio automático dos filtros, confirmações e impressão
```

## Antes de usar de verdade

O sistema guarda dados de saúde, que a LGPD trata como dados sensíveis, e o prontuário precisa ser guardado por anos.

- Rode atrás de HTTPS com um servidor WSGI (por exemplo `waitress-serve --port=8000 app:app`), nunca exposto com `python app.py`.
- Se houver proxy reverso (Nginx, Cloudflare), configure o `ProxyFix` do Werkzeug para o limite por IP do agendamento online enxergar o IP real do paciente.
- Ponha um aviso de privacidade (quem é a clínica, para que os dados são usados, como pedir exclusão) e linke na página de agendamento.
- Combine com a equipe o que vai no prontuário compartilhado. Os conselhos (CFFa, CFP, CFN) têm regras próprias de registro e sigilo; a opção "sigilosa" ajuda, mas não substitui esse acordo.
- Faça backup diário de `instance/clinica.db` (é um arquivo só; copie com o sistema parado ou use `sqlite3 clinica.db ".backup copia.db"`) e guarde fora do computador da clínica.
- Ainda não há registro de quem visualizou cada prontuário (log de acesso) nem anexos (laudos, exames). São os próximos passos naturais se a clínica precisar.
