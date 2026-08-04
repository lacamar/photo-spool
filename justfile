default: run

run:
    python3 main.py

demo:
    python3 main.py --demo

clean-db:
    rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/photo-import/data.db"
