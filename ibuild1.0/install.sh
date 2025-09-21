#!/usr/bin/env bash
set -e

PREFIX="/usr"
LIBDIR="$PREFIX/lib/ibuild1.0"
BINDIR="$PREFIX/bin"

echo "[+] Instalando ibuild em $LIBDIR"

# cria diretórios
sudo mkdir -p "$LIBDIR"
sudo mkdir -p "$BINDIR"

# copia código
sudo cp -r Ibuild1.0/* "$LIBDIR/"

# cria wrapper atualizado
sudo tee "$BINDIR/ibuild" > /dev/null <<'EOF'
#!/usr/bin/env python3
import sys, os
LIB_DIR = "/usr/lib/ibuild1.0"
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)
try:
    import cli
except ImportError as e:
    sys.stderr.write(f"[ibuild] ERRO: não foi possível importar cli.py ({e})\n")
    sys.exit(1)
if __name__ == "__main__":
    sys.exit(cli.main())
EOF

# deixa executável
sudo chmod +x "$BINDIR/ibuild"

echo "[OK] ibuild instalado com sucesso!"
echo "Agora você pode rodar: ibuild --help"
