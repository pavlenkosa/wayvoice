.PHONY: lint test deb clean

lint:
	python3 -m compileall -q app/src tests
	bash -n scripts/* packaging/DEBIAN/* scripts/build-deb.sh

test:
	PYTHONPATH=app/src python3 -m unittest discover -s tests -v

deb:
	./scripts/build-deb.sh

clean:
	rm -rf build dist
