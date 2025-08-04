# tgcf CLI integration targets
cli: ## Run the CLI with arguments (e.g., make cli mode=live)
	@poetry run python run_tgcf.py $(mode) $(args)

run-live: ## Run tgcf in live mode
	@$(MAKE) cli mode=live

run-past: ## Run tgcf in past mode
	@$(MAKE) cli mode=past

run-live-verbose: ## Run tgcf in live mode with verbose output
	@$(MAKE) cli mode=live args=--loud

run-past-verbose: ## Run tgcf in past mode with verbose output
	@$(MAKE) cli mode=past args=--loud

# lists all available targets
list:
	@sh -c "$(MAKE) -p no_targets__ | \
		awk -F':' '/^[a-zA-Z0-9][^\$$#\/\\t=]*:([^=]|$$)/ {\
			split(\$$1,A,/ /);for(i in A)print A[i]\
		}' | grep -v '__\$$' | grep -v 'make\[1\]' | grep -v 'Makefile' | sort"

# Display this help message
help:
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n\nTargets:\n"} /^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

# required for list
no_targets__:

VERSION=$$(poetry version -s)

clean:
	@rm -rf build dist .eggs *.egg-info
	@rm -rf .benchmarks .coverage coverage.xml htmlcov report.xml .tox
	@find . -type d -name '.mypy_cache' -exec rm -rf {} +
	@find . -type d -name '__pycache__' -exec rm -rf {} +
	@find . -type d -name '*pytest_cache*' -exec rm -rf {} +
	@find . -type f -name "*.py[co]" -exec rm -rf {} +

fmt: clean
	@poetry run isort .
	@poetry run black .

hard-clean: clean
	@rm -rf .venv

ver:
	@echo tgcf $(VERSION)

pypi:
	@poetry publish --build

docker:
	@docker build -t tgcf .
	@docker tag tgcf aahnik/tgcf:latest
	@docker tag tgcf aahnik/tgcf:$(VERSION)

docker-release: docker
	@docker push -a aahnik/tgcf

docker-run:
	@docker run -d -p 8501:8501 --env-file .env aahnik/tgcf

release: pypi docker-release
