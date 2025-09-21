PREFIX=/usr
LIBDIR=$(PREFIX)/lib/ibuild1.0
BINDIR=$(PREFIX)/bin

install:
	mkdir -p $(LIBDIR)
	cp -r Ibuild1.0/* $(LIBDIR)/
	mkdir -p $(BINDIR)
	echo '#!/usr/bin/env python3' > $(BINDIR)/ibuild
	echo 'import sys, os' >> $(BINDIR)/ibuild
	echo 'LIB_DIR = "$(LIBDIR)"' >> $(BINDIR)/ibuild
	echo 'if LIB_DIR not in sys.path:' >> $(BINDIR)/ibuild
	echo '    sys.path.insert(0, LIB_DIR)' >> $(BINDIR)/ibuild
	echo 'import cli' >> $(BINDIR)/ibuild
	echo 'if __name__ == "__main__":' >> $(BINDIR)/ibuild
	echo '    sys.exit(cli.main())' >> $(BINDIR)/ibuild
	chmod +x $(BINDIR)/ibuild
	@echo "[OK] ibuild instalado em $(BINDIR)/ibuild"

uninstall:
	rm -rf $(LIBDIR)
	rm -f $(BINDIR)/ibuild
	@echo "[OK] ibuild removido"
