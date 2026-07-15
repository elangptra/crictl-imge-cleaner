# Crictl Image Cleanup

Menghapus image containerd yang **tidak dipakai container manapun** (baik
`Running` maupun `Exited`) **dan** sudah lebih tua dari `retention_days`
(default 30 hari).

## Cara Kerja

1. **"Tidak terpakai"** ditentukan dengan membandingkan seluruh image
   (`crictl images -o json`) terhadap image yang dirujuk container yang ada
   (`crictl ps -a -o json`, termasuk container yang sudah `Exited`).
   Image yang masih dipakai **tidak pernah** jadi kandidat, berapa pun
   umurnya.

2. **"Umur"** didekati dari mtime file blob image tersebut di content store
   containerd (`/var/lib/containerd/io.containerd.content.v1.content/blobs/sha256/`),
   karena CRI/`crictl` tidak menyimpan timestamp pull secara langsung.
   Kalau blob tidak ditemukan di path yang diharapkan, umur dianggap
   "tidak diketahui" dan image tersebut **dilewati** (fail-safe, tidak
   dihapus).

3. Ada daftar **protected patterns** di config (default: `pause`) sebagai
   safety net tambahan di luar deteksi otomatis — image yang cocok pattern
   ini tidak akan dihapus apa pun kondisinya.

## Instalasi

```bash
sudo mkdir -p /opt/crictl-image-cleanup
sudo cp -r scripts /opt/crictl-image-cleanup/
sudo mkdir -p /etc/image-cleanup
sudo cp config/config.yaml /etc/image-cleanup/config.yaml

sudo apt install -y python3-yaml   # kalau belum ada dari setup etcd-backup-service

sudo mkdir -p /var/log/image-cleanup /var/lib/node_exporter/textfile_collector
```

## WAJIB: Jalankan Dry-Run Dulu

Config default sudah `cleanup.dry_run: true` — **jangan ubah ke `false`
sebelum Anda review hasilnya**:

```bash
sudo python3 /opt/crictl-image-cleanup/scripts/image-cleanup.py /etc/image-cleanup/config.yaml
echo "Exit code: $?"
```

Cek log — cari baris `"action": "dry_run_candidate"` untuk melihat image
apa saja yang **akan** dihapus kalau dry-run dimatikan:

```bash
sudo grep dry_run_candidate /var/log/image-cleanup/image-cleanup.log | tail -20
```

Kalau daftarnya sudah sesuai ekspektasi (bukan image yang masih Anda
perlukan), baru ubah `dry_run: false` di `/etc/image-cleanup/config.yaml`.

## Menjalankan Otomatis (systemd timer, harian jam 03:00)

```bash
sudo cp systemd/image-cleanup.service systemd/image-cleanup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now image-cleanup.timer

sudo systemctl list-timers image-cleanup.timer
```

Ubah jadwal di `image-cleanup.timer` (`OnCalendar=`) sesuai kebutuhan,
lalu `daemon-reload` + `systemctl restart image-cleanup.timer`.

## Konfigurasi

| Field | Keterangan |
|---|---|
| `cleanup.retention_days` | Umur minimum (hari) sebelum image tak terpakai dihapus |
| `cleanup.dry_run` | `true` = cuma log, `false` = benar-benar hapus |
| `crictl.path` | Path binary `crictl` — cek dengan `which crictl` |
| `containerd.content_dir` | Lokasi content store containerd untuk cek umur |
| `protected.patterns` | Image ref yang tidak boleh dihapus apa pun kondisinya |

## Exit Codes

| Code | Arti |
|------|------|
| 0 | Success |
| 1 | Config Error |
| 2 | Gagal list images (`crictl images`) |
| 3 | Gagal list containers (`crictl ps -a`) |
| 4 | Satu atau lebih penghapusan image gagal |
| 5 | Unexpected error |

## Monitoring

Metric ditulis ke `image_cleanup.prom` di textfile collector node_exporter
yang sama dengan service ETCD backup:
`image_cleanup_success`, `image_cleanup_dry_run`, `image_cleanup_images_total`,
`image_cleanup_images_candidates`, `image_cleanup_images_removed`,
`image_cleanup_images_failed`, `image_cleanup_bytes_freed`,
`image_cleanup_last_run_timestamp`.

## Testing

Diuji dengan mock `crictl` untuk skenario:
- Image dipakai container → tidak pernah dihapus meski lama
- Image tidak dipakai + melewati retention → kandidat hapus (dry-run: logged, real: `crictl rmi` dipanggil)
- Image protected pattern (`pause`) → tidak dihapus meski tidak dipakai & lama
- Image tidak dipakai tapi belum cukup umur → tidak dihapus
- Blob tidak ditemukan (umur tak diketahui) → dilewati demi keamanan