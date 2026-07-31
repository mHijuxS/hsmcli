#!/bin/bash
# hsmcli installer
set -e

echo "Installing hsmcli…"

if ! command -v python3 &> /dev/null; then
    echo "python3 is required" >&2
    exit 1
fi

pip3 install -e .

chmod +x hsmcli.py

if [ -w /usr/local/bin ] || sudo -n true 2>/dev/null; then
    echo "Linking /usr/local/bin/hsmcli -> $(pwd)/hsmcli.py"
    sudo ln -sf "$(pwd)/hsmcli.py" /usr/local/bin/hsmcli
    echo "Installed. Run 'hsmcli --help' from anywhere."
else
    echo "Can't write /usr/local/bin — run with sudo or add $(pwd) to PATH."
    echo "You can still use ./hsmcli.py or 'python3 -m hsmcli'."
fi

cat <<EOF

Next steps:
  1. Grab the Cookie header from your browser (devtools > Application > Cookies
     on www.hacksmarter.org, or copy from a request).
  2. hsmcli config set-cookie 'sb-auth-auth-token.0=…; sb-auth-auth-token.1=…'
  3. hsmcli whoami
  4. hsmcli labs list
  5. hsmcli lab enroll <name>
  6. hsmcli lab launch <name>
  7. hsmcli lab vpn <name> -o mylab.ovpn
EOF
