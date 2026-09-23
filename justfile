default: run

run:
    python3 main.py

demo:
    python3 main.py --demo

test:
    python3 -m unittest discover -s tests -v

clean-db:
    rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/photo-spool/data.db"
