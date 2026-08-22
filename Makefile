.PHONY: help test test-e2e lint type docs check check-oo update-oo check-coupling update-coupling check-suppressions update-suppressions report format build install clean depot metrics coverage prfaq clean-tex zspec zspec-test

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

test: ## Run tests
	uv run pytest

test-e2e: ## Tier 5 -- drive the INSTALLED `vox` binary and `vox mcp` server (run `make install` first)
	uv run pytest tests/e2e -v -m e2e

lint: ## Lint and format check (Python + shell)
	uv run ruff check .
	uv run ruff format --check .
	shellcheck -x plugin/hooks/*.sh scripts/*.sh install.sh
	bash scripts/check-skill-permissions.sh
	bash scripts/check-plugin-surface.sh

type: ## Type check with mypy and pyright
	uv run mypy src/ tests/
	uv run pyright src/ tests/

docs: ## Lint markdown files (matches CI docs job)
	npx --yes markdownlint-cli2@0.22.1 "**/*.md"

# Base-comparison flags injected by CI (e.g. --base-ref <merge-base> --require-base).
# Empty locally, where the tools default base to `git merge-base origin/main HEAD`.
OO_BASE ?=
COUPLING_BASE ?=
SUPPRESSION_BASE ?=

check: lint type docs test check-oo check-coupling check-suppressions ## Run all quality gates

check-oo: ## OO ratchet — must improve over baseline, never regress
	uv run python tools/oo_score.py src/punt_vox/ --check $(OO_BASE)

update-oo: ## Update OO baseline after improvements (stage .oo-baseline.json and .oo-audit.jsonl)
	uv run python tools/oo_score.py src/punt_vox/ --update $(OO_BASE)

check-coupling: ## Coupling ratchet — merge-base scoped, must not regress
	uv run python tools/oo_coupling.py src/punt_vox/ --check $(COUPLING_BASE)

update-coupling: ## Update coupling baseline (stage .oo-coupling-baseline.json and .oo-coupling-audit.jsonl)
	uv run python tools/oo_coupling.py src/punt_vox/ --update $(COUPLING_BASE)

check-suppressions: ## Suppression ratchet — base-commit scoped, count must not increase
	uv run python tools/suppression_ratchet.py src/punt_vox/ --check $(SUPPRESSION_BASE)

update-suppressions: ## Update suppression baseline
	uv run python tools/suppression_ratchet.py src/punt_vox/ --update

report: ## Full diagnostics (OO score + all checks, no fail-fast)
	-uv run python tools/oo_score.py src/punt_vox/ --threshold
	-uv run mypy src/ tests/
	-uv run ruff format --check src/ tests/
	-uv run ruff check --preview --select PLR6301,PLR0913,UP035,UP040,UP007,N,I,SIM,PLC1901,S101 src/ tests/
	-uv run pyright src/ tests/
	-shellcheck -x plugin/hooks/*.sh scripts/*.sh install.sh
	-bash scripts/check-plugin-surface.sh
	-uv run pytest
	@echo "Report complete."

format: ## Auto-format code
	uv run ruff format .
	uv run ruff check --fix .

build: ## Build wheel and sdist
	rm -rf dist/
	uv build
	uvx twine check dist/*

install: build ## Build and install locally
	uv tool install --force dist/*.whl

clean: ## Remove build artifacts
	rm -rf dist/ .tmp/

DEPOT := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))../.depot

depot: build ## Build and copy wheel to local depot
	@mkdir -p $(DEPOT)
	@cp dist/*.whl $(DEPOT)/
	@echo "depot: $$(ls dist/*.whl | xargs -n1 basename) -> $(DEPOT)/"

metrics: ## Run ABC complexity metrics on src/
	uv run python tools/run_metrics.py

coverage: ## Run tests with coverage report
	uv run python tools/run_coverage.py

# LaTeX intermediate files to remove after compilation
LATEX_ARTIFACTS = *.aux *.log *.out *.bbl *.bcf *.blg *.run.xml *.fls \
                  *.fdb_latexmk *.synctex.gz *.toc

TEX_FILES = prfaq.tex

prfaq: ## Compile .tex to .pdf and clean artifacts
	@for f in $(TEX_FILES); do \
	  echo "Compiling $$f ..."; \
	  dir=$$(dirname "$$f"); base=$$(basename "$$f" .tex); \
	  pdflatex -interaction=nonstopmode -output-directory="$$dir" "$$f" > /dev/null 2>&1; \
	  if [ -f "$$dir/$$base.bib" ] && command -v biber > /dev/null 2>&1; then \
	    (cd "$$dir" && biber "$$base") > /dev/null 2>&1 || true; \
	    pdflatex -interaction=nonstopmode -output-directory="$$dir" "$$f" > /dev/null 2>&1; \
	  fi; \
	  pdflatex -interaction=nonstopmode -output-directory="$$dir" "$$f" > /dev/null 2>&1; \
	  if [ -f "$$dir/$$base.pdf" ]; then \
	    echo "  $$dir/$$base.pdf"; \
	  else \
	    echo "Error: $$f failed to compile" >&2; exit 1; \
	  fi; \
	done
	@rm -f $(LATEX_ARTIFACTS)

clean-tex: ## Remove LaTeX intermediate files
	@rm -f $(LATEX_ARTIFACTS)

# Z specification targets
PROBCLI ?= $(HOME)/Applications/ProB/probcli
ZSPEC = docs/vox-notify.tex

zspec: ## Type-check and compile Z spec
	fuzz -t $(ZSPEC)
	cd docs && pdflatex -interaction=nonstopmode vox-notify.tex > vox-notify.log 2>&1
	cd docs && pdflatex -interaction=nonstopmode vox-notify.tex >> vox-notify.log 2>&1
	@if [ -f docs/vox-notify.pdf ]; then \
		echo "  docs/vox-notify.pdf"; \
	else \
		echo "Error: docs/vox-notify.pdf was not produced; see docs/vox-notify.log for details." >&2; \
		exit 1; \
	fi

zspec-test: zspec ## Animate and model-check Z spec with ProB
	@if [ ! -x "$$(command -v probcli 2>/dev/null || echo "$(PROBCLI)")" ] && [ ! -x "$(PROBCLI)" ]; then \
		echo "Error: probcli not found. Set PROBCLI or install ProB." >&2; \
		exit 1; \
	fi
	$(PROBCLI) $(ZSPEC) -init
	$(PROBCLI) $(ZSPEC) -animate 20
	$(PROBCLI) $(ZSPEC) -cbc_assertions
	$(PROBCLI) $(ZSPEC) -cbc_deadlock
	$(PROBCLI) $(ZSPEC) -model_check \
		-p DEFAULT_SETSIZE 2 \
		-p MAX_INITIALISATIONS 100 \
		-p MAX_OPERATIONS 1000 \
		-p TIME_OUT 30000
