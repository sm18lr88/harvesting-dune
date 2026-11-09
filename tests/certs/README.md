# Test certificates (test only)

These files are for the tests only. They protect nothing. The keys are public on purpose. Never use them on the real server, and never ship them with the detector.

| File | What it is |
|---|---|
| `test-ca.pem`, `test-ca.key` | The test CA. The tests trust it, like the detector trusts the real private CA. |
| `test-server.pem`, `test-server.key` | A server certificate that `test-ca` signed. Subject alternative name: `IP:127.0.0.1`. |
| `other-ca.pem`, `other-ca.key` | A second, unrelated CA. |
| `other-server.pem`, `other-server.key` | A server certificate for `127.0.0.1` that `other-ca` signed. A client that trusts only `test-ca` must refuse it (the negative TLS test). |

All keys are EC P-256. All certificates are valid for 10 years from 2026-11-08.

## Make them again

Use Git's OpenSSL (`"C:\Program Files\Git\usr\bin\openssl.exe"`) in this folder. Repeat the steps with `other-ca` and `other-server` for the second pair.

```
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout test-ca.key -out test-ca.pem -days 3650 -subj "/CN=guildsync test CA" -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" -addext "subjectKeyIdentifier=hash"
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout test-server.key -out test-server.csr -subj "/CN=127.0.0.1"
openssl x509 -req -in test-server.csr -CA test-ca.pem -CAkey test-ca.key -CAcreateserial -out test-server.pem -days 3650 -sha256 -extfile leaf.ext
```

`leaf.ext` holds:

```
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=IP:127.0.0.1
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
```

Delete the `.csr`, `.srl` and `leaf.ext` files afterwards.
