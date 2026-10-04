# FilingSage — everyday commands. `make up` is the only one you need to start.
.PHONY: up down restart logs ps test lint

up:        ## Start everything and open the dashboard (http://localhost:8501)
	@./scripts/up.sh

down:      ## Stop everything (your data is kept)
	docker compose down

restart:   ## Restart the app containers after a code change that needs it
	docker compose restart api worker beat ui

logs:      ## Follow the API, worker and dashboard logs
	docker compose logs -f api worker ui

ps:        ## Show the status of every service
	docker compose ps

test:      ## Run the test suite (needs Docker running and the host venv)
	. .venv/bin/activate && pytest -q

lint:
	. .venv/bin/activate && ruff check src tests ui
