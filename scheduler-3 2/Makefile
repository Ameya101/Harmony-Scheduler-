.PHONY: run test test-cov validate docker-build docker-run

run:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	python -m pytest tests/ -v

test-cov:
	python -m pytest tests/ --cov=app --cov-report=term-missing

validate:
	@echo "Generating schedule output..."
	@curl -s -X POST http://localhost:8000/schedule \
	  -H "Content-Type: application/json" \
	  -d @examples/client_a_sample.json > /tmp/schedule_output.json
	@python validate.py /tmp/schedule_output.json examples/client_a_sample.json

docker-build:
	docker build -t harmony-scheduler .

docker-run:
	docker run -p 8000:8000 harmony-scheduler
