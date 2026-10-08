.PHONY: version-check lint test deb clean

version-check:
	python3 scripts/check-version.py

lint: version-check
	python3 -m compileall -q app/src tests scripts/check-version.py
	set -e; for f in \
	  scripts/wayvoice scripts/wayvoice-daemon scripts/wayvoice-settings \
	  scripts/wayvoice-engine-setup scripts/wayvoice-ydotoold scripts/setup-user \
	  scripts/build-deb.sh scripts/build-ydotool.sh \
	  packaging/DEBIAN/postinst packaging/DEBIAN/postrm packaging/DEBIAN/prerm; do \
	    if [ -f "$$f" ]; then bash -n "$$f"; else echo "skip (absent): $$f"; fi; \
	  done

test:
	PYTHONPATH=app/src python3 -m unittest discover -s tests -t . -v

deb: version-check
	./scripts/build-deb.sh

clean:
	rm -rf build dist
