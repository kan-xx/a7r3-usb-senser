PYTHON ?= python3

.PHONY: all test clean

all: test

test:
	PYTHONPATH=src $(PYTHON) -m pytest -v tests || PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

clean:
	rm -rf build dist .pytest_cache src/*.egg-info
	find . -type d -name "__pycache__" -exec rm -rf {} +
