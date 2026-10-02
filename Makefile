.PHONY: version-check lint test deb clean

version-check:
	python3 scripts/check-version.py

lint: version-check
	python3 -m compileall -q app/src tests scripts/check-version.py
	bash -n scripts/* packaging/DEBIAN/* scripts/build-deb.sh

test:
	PYTHONPATH=app/src python3 -m unittest discover -s tests -v

deb: version-check
	./scripts/build-deb.sh

clean:
	rm -rf build dist
