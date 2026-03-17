.PHONY: test install run lint

install:
	pip install -e .

test:
	pytest tests/ -v

lint:
	python -m py_compile bugagent/*.py

run:
	bugagent run --once
