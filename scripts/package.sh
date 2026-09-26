#!/usr/bin/env bash
# Build ccboard.tar.gz from the committed tree (HEAD), prefixed with ccboard/.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
git archive --format=tar.gz --prefix=ccboard/ -o ccboard.tar.gz HEAD
ls -l ccboard.tar.gz
