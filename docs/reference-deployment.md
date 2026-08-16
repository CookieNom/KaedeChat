# kaedechat.com reference deployment

Status: reviewed configuration template; no credentials or live infrastructure
are managed by this repository.

The v1 reference deployment is one Kaede instance at `kaedechat.com`.
`media.kaedechat.com` is used only by the bundled Garage backend. Host nginx
owns public TCP 80/443 and proxies to Caddy on loopback port 18081.
PostgreSQL, Dragonfly, object storage, ClamAV, application services, and the
optional observability stack stay on internal Compose networks. Grafana is
loopback-only on 18084 and must be reached through an authenticated operator
tunnel.

Start from `deploy/reference.env.example`. Generate every secret independently,
store the result outside version control as `.env`, and follow
`docs/operator.md`. The example deliberately leaves voice disabled, because
LiveKit also requires reserved host RTC/TURN ports and reviewed certificates.
External S3 is supported by applying `deploy/compose.s3.yml` and the
provider-specific settings described in the operator guide.

Before initial exposure, the operator must have in place: A/AAAA records, a
certificate covering `kaedechat.com` and (for Garage) `media.kaedechat.com`,
outbound TCP 443, SMTP delivery, tested backups, alert routing, and a restore
drill. Copy the nginx example and replace `chat.example.com` with
`kaedechat.com`; never reuse the example edge secret. Then run the complete
release gate:

```sh
make env-check
make compose-check
make check
make migration-check
make identity-check
make chat-check
make federation-check
make federation-tls-check
make media-check
make voice-check
make release-check
make audit
```

Enable observability with `--profile observability` only after setting a unique
Grafana password of at least 20 characters. Profile preflight fails closed on a
blank or repository placeholder. Prometheus and Loki are not published. The
repository does not mount the Docker socket; an operator who wants centralized
container logs must use a separately reviewed host collector and apply labels
outside Kaede's trust boundary.

Backups are one quiesced set: PostgreSQL plus all three object buckets and the
matching secret export. Restore drills use separate Compose volumes and bucket
names, no SMTP/federation egress, and no public routing. A clone restored with
the live domain and master key must never be exposed alongside the
authoritative instance. During upgrades, follow the stop/migrate/start sequence
in the operator guide so old writers cannot overlap Alembic.
