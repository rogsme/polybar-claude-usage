.PHONY: test install run

test:
	python3 -m unittest discover -s tests

install:
	./install.sh

run:
	./polybar_claude_usage.py --once
