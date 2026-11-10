# VSS Uptime Monitor - MKP

Pencatat uptime server Howen VSS lokal MKP (`192.168.185.6:9966`), dipasang di PC site yang menyala 24 jam.
Satu exe, dua mode: dashboard (double-click) dan pencatat background (`--monitor`, didaftarkan ke Task Scheduler saat boot sebagai SYSTEM).

Panduan pemakaian untuk tim site: [README.txt](README.txt). Exe siap pakai ada di halaman Releases.

## Cara kerja

Setiap 30 detik dicek: gateway lokal, mesin server (ping, atau port non-VSS karena server MKP memblokir ICMP), HTTP layanan VSS :9966, dan internet.
Monitor hanya menulis sampel ke SQLite (`data_uptime/uptime.db`). Kejadian downtime dan jenisnya dihitung dashboard dari sampel, jadi restart atau listrik padam tidak meninggalkan state setengah jadi.

## Build

Butuh Python 3.13 + PyInstaller (dan openpyxl untuk ekspor Excel).

```
pip install pyinstaller openpyxl
python -m PyInstaller --noconfirm VSS_UptimeMonitor_MKP.spec
```

Hasil: `dist/VSS_UptimeMonitor_MKP.exe`. Untuk rilis, zip exe + `README.txt`.

## Menjalankan dari source

```
python vss_uptime_mkp.py            # dashboard
python vss_uptime_mkp.py --monitor  # pencatat
```

Data dan `config.json` dibuat di `data_uptime/` di samping script/exe.
