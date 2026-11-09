default: run

run:
    cargo run --release

demo:
    cargo run --release -- --demo

test:
    cargo test

clean-db:
    rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/photo-spool/data.db"

lint:
    cargo clippy --all-targets
    cargo fmt --check
