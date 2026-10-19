#!/usr/bin/env bash
# Generate a private CA and an IP-specific certificate for direct Uvicorn TLS.
set -euo pipefail
if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "Usage: bash deploy/generate-local-tls.sh SERVER_IP [OUTPUT_DIR]" >&2
    exit 2
fi
server_ip=$1
tls_dir=${2:-.secrets/tls}
umask 077
mkdir -p "$tls_dir"
if [[ -e "$tls_dir/server.key" || -e "$tls_dir/server.crt" ]]; then
    echo "Server certificate already exists in $tls_dir; refusing to overwrite." >&2
    exit 1
fi
if [[ -e "$tls_dir/root-ca.key" || -e "$tls_dir/root-ca.crt" ]]; then
    if [[ ! -f "$tls_dir/root-ca.key" || ! -f "$tls_dir/root-ca.crt" ]]; then
        echo "Existing CA is incomplete; both root-ca.key and root-ca.crt are required." >&2
        exit 1
    fi
else
    openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 3650 \
        -keyout "$tls_dir/root-ca.key" -out "$tls_dir/root-ca.crt" \
        -subj '/CN=WSI Patchkit Local CA' \
        -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
        -addext 'keyUsage=critical,keyCertSign,cRLSign'
fi
openssl req -new -newkey rsa:2048 -nodes -sha256 \
    -keyout "$tls_dir/server.key" -out "$tls_dir/server.csr" \
    -subj '/CN=WSI Patchkit Viewer'
cat > "$tls_dir/server.ext" <<EXT
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=IP:${server_ip}
EXT
openssl x509 -req -in "$tls_dir/server.csr" \
    -CA "$tls_dir/root-ca.crt" -CAkey "$tls_dir/root-ca.key" -CAcreateserial \
    -out "$tls_dir/server.crt" -days 365 -sha256 -extfile "$tls_dir/server.ext"
openssl verify -CAfile "$tls_dir/root-ca.crt" -verify_ip "$server_ip" "$tls_dir/server.crt"
echo "Trust $tls_dir/root-ca.crt on client devices. Keep both .key files private."
