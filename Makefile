.PHONY: test lint run matrix db-up db-down

test:
	pytest

lint:
	ruff check .

run:
	uvicorn app.main:app --reload

matrix:
	python -m app.tools.travel

db-up:
	docker compose up -d postgres

db-down:
	docker compose down
