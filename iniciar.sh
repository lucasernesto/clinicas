#!/bin/sh
# Prepara o ambiente (na primeira vez) e inicia o sistema.
# Uso: ./iniciar.sh        -> inicia
#      ./iniciar.sh demo   -> cria dados de demonstração antes de iniciar
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    echo "Criando ambiente virtual..."
    python3 -m venv .venv || {
        echo "Não consegui criar o ambiente. Instale o Python 3.10+ (no Ubuntu/Debian: sudo apt install python3-venv)."
        exit 1
    }
fi

echo "Conferindo dependências..."
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt

if [ "$1" = "demo" ]; then
    .venv/bin/python demo.py
fi

exec .venv/bin/python app.py
