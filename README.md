# photo-spool

Automatic raw to lossless DNG photo import from cameras and iPhones.

## Install

    sudo dnf copr enable lacamar/arm64-misc && sudo dnf install photo-spool

## Usage

    photo-spool

## Development

    just test
    just lint
    packaging/build-rpm.sh -S --copr lacamar/arm64-misc --copr-wait
