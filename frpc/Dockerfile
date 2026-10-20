# Контейнер frpc экземпляра «Штурмана» — подключение к посреднику (docs/relay.md). Собирается на
# сервере, когда владелец подключает экземпляр к посреднику: ./ops/relay.sh join, затем ./ops/up.sh.
# frp — официальный выпуск 0.71.0 с GitHub (https://github.com/fatedier/frp, Apache-2.0); сумма
# sha256 закреплена ниже и сверяется при сборке, не совпала — образ не собирается. Первая ступень —
# тот же базовый образ, что у сервиса переписки (уже есть на сервере); в итоговом образе — только
# frpc (статическая программа) и его настройка, без оболочки и пакетов.
FROM python:3.12.12-slim-bookworm AS fetch

ARG FRP_VERSION=0.71.0
ARG FRP_SHA256_AMD64=84f27e39f11169f7adcef8e8b70c9329de17747b1f14dad9fb95eef5682ea716
ARG FRP_SHA256_ARM64=f33c293c275d8fc68c654b6fba8f10b2551d6463d09a9fc9cffb7227eae82266
ARG TARGETARCH

RUN python - <<'PY'
import hashlib, io, os, platform, tarfile, urllib.request
version = os.environ["FRP_VERSION"]
arch = os.environ.get("TARGETARCH") or {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), "")
sums = {"amd64": os.environ["FRP_SHA256_AMD64"], "arm64": os.environ["FRP_SHA256_ARM64"]}
if arch not in sums:
    raise SystemExit(f"frpc собирается только для amd64 и arm64, а здесь {arch or platform.machine()}")
name = f"frp_{version}_linux_{arch}"
url = f"https://github.com/fatedier/frp/releases/download/v{version}/{name}.tar.gz"
data = urllib.request.urlopen(url, timeout=300).read()
got = hashlib.sha256(data).hexdigest()
if got != sums[arch]:
    raise SystemExit(f"контрольная сумма frp не совпала: {got}")
with tarfile.open(fileobj=io.BytesIO(data)) as t:
    member = t.getmember(f"{name}/frpc")
    with open("/frpc", "wb") as out:
        out.write(t.extractfile(member).read())
os.chmod("/frpc", 0o755)
print(f"frp {version} ({arch}): сумма совпала")
PY

FROM scratch
COPY --from=fetch /frpc /frpc
COPY frpc.toml /etc/frpc/frpc.toml
ENTRYPOINT ["/frpc"]
CMD ["-c", "/etc/frpc/frpc.toml"]
