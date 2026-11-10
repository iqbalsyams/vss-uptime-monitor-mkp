VSS UPTIME MONITOR - MKP
========================

Aplikasi untuk mencatat kapan server VSS MKP (192.168.185.6:9966) tidak bisa
diakses, dan apa kemungkinan penyebabnya. Dipasang di PC site yang menyala
24 jam.


ISI BUNDLE
----------
VSS_UptimeMonitor_MKP.exe : aplikasinya (pencatat + dashboard dalam 1 file)
README.txt                : panduan ini


SYARAT
------
- Windows 10/11. Tidak perlu install Python atau aplikasi lain.
- PC terhubung ke jaringan intranet MKP (bisa akses 192.168.185.6:9966).
- Akun Windows dengan hak admin (sekali saja, saat memasang monitor).


PEMASANGAN (SEKALI)
-------------------
1. Buat folder tetap, misalnya C:\VSSMonitor, lalu salin exe ke sana.
   Jangan simpan di Downloads/Desktop dan JANGAN pindahkan exe setelah
   dipasang. Task Scheduler menunjuk ke lokasi exe tersebut.
2. Klik dua kali VSS_UptimeMonitor_MKP.exe.
   Kalau muncul "Windows protected your PC": klik "More info" -> "Run anyway".
3. Klik "Pasang monitor otomatis" -> Yes -> izinkan prompt admin (UAC).
4. Tunggu sekitar 1 menit, klik Refresh. Status bawah harus berubah menjadi
   "Monitor background: berjalan".
5. Atur PC agar tidak tidur:
   Settings -> System -> Power -> Screen and sleep ->
   "When plugged in, put my device to sleep after" = Never.
   (Layar boleh mati, PC jangan sleep.)
6. Disarankan: di BIOS set "Restore on AC Power Loss" = Power On, supaya PC
   menyala sendiri setelah listrik padam. Kalau tidak, data akan kosong
   sampai ada orang yang menyalakan PC.
7. Pastikan jam PC benar (Settings -> Time -> Set time automatically).
   Semua waktu di aplikasi memakai jam PC (WITA).

Setelah dipasang, pencatat jalan sendiri setiap PC menyala, walaupun tidak
ada yang login. Menutup jendela dashboard tidak menghentikan pencatatan.


CARA KERJA
----------
Setiap 30 detik dicek 4 hal:
- Gateway lokal  : router/switch tempat PC monitor terhubung
- Mesin server   : apakah mesin server hidup (lihat catatan di bawah)
- Layanan VSS    : apakah web/API di port 9966 menjawab (HTTP)
- Internet       : ping ke 8.8.8.8

Catatan "Mesin server": server MKP memblokir ping. Karena itu aplikasi
mencari port lain di server yang bukan milik VSS (urutan: 3389, 445, 135,
22, 5985, 3306, 5432, 1433) dan memakai yang pertama terbuka. Port yang
dipakai tampil di kotak "Mesin server", misalnya "OK (port 3389)".
Kalau tidak ada yang terbuka, kotak menampilkan "ping diblokir, tidak ada
port cek" (abu-abu). Uptime tetap tercatat benar, tapi server mati tidak
bisa dibedakan dari aplikasi VSS mati.

Server dianggap UP kalau layanan VSS :9966 menjawab. Downtime dicatat kalau
gagal 3 kali berturut-turut (sekitar 1,5 menit), supaya gangguan sesaat
tidak terhitung.


MEMBACA RIWAYAT DOWNTIME
------------------------
Jenis                    Artinya
Server tidak merespons   Mesin server dan port 9966 gagal, tapi gateway OK.
                         Server mati/hang, atau kabel/port switch server
                         bermasalah. Kalau keterangannya "tidak bisa
                         dibedakan", lihat catatan "Mesin server" di atas.
Layanan VSS mati         Mesin server hidup, tapi aplikasi VSS tidak
                         menjawab. Aplikasi VSS hang/berhenti.
Jaringan lokal putus     Gateway PC monitor juga gagal. Masalahnya di jaringan
                         sisi PC monitor, belum tentu server.
Monitor tidak mencatat   Tidak ada data sama sekali. Kalau keterangannya
                         "PC monitor menyala ulang", PC ikut mati: indikasi
                         kuat listrik site padam. Kalau "PC tidak restart",
                         monitor berhenti atau PC sempat sleep.

Heatmap "Uptime per jam" menunjukkan pola per jam selama 7 hari terakhir.
Kalau dugaan "server sering mati malam" benar, kotak merah/oranye akan
berkumpul di kolom jam malam.

Uptime % hanya menghitung waktu ketika monitor berjalan. Jam tanpa data
(abu-abu) tidak dihitung sebagai down maupun up.


EKSPOR EXCEL
------------
Klik "Ekspor Excel". Isinya:
- Downtime : semua kejadian beserta durasi dan keterangan
- Harian   : uptime % per tanggal
- Per Jam  : % down per jam (00-23) untuk semua data; dipakai untuk
             membuktikan pola malam hari
- Info     : pengaturan dan rentang data
Kirim file uptime_vss_MKP_....xlsx ke Iqbal.


PENGATURAN (data_uptime\config.json)
------------------------------------
Folder data_uptime dibuat otomatis di samping exe. Buka config.json dengan
Notepad:
  server_ip      : IP server VSS (default 192.168.185.6)
  vss_port       : port web/API VSS (default 9966)
  internet_host  : alamat untuk cek internet (default 8.8.8.8)
  gateway        : "auto" (deteksi otomatis) atau isi IP router, misalnya
                   "192.168.185.1"
  host_port      : port cek "mesin server hidup". "auto" = cari otomatis,
                   angka (misalnya 3389) = pakai port itu, 0 = matikan
  interval_sec   : jeda antar-cek dalam detik (default 30)
  fail_threshold : jumlah cek gagal berturut-turut sebelum dihitung down
  keep_days      : lama data disimpan (default 180 hari)
Setelah mengedit, klik "Hapus monitor" lalu "Pasang monitor otomatis",
atau restart PC.


MENGGANTI KE VERSI BARU
-----------------------
1. Buka exe lama, klik "Hapus monitor" -> Yes -> izinkan UAC. Tutup jendela.
2. Timpa VSS_UptimeMonitor_MKP.exe di C:\VSSMonitor dengan versi baru.
   Folder data_uptime JANGAN dihapus; data lama tetap terbaca.
3. Buka exe baru, klik "Pasang monitor otomatis".


MASALAH UMUM
------------
- "MONITOR TIDAK BERJALAN": klik "Pasang monitor otomatis" lagi. Kalau tetap
  tidak berjalan, buka data_uptime\monitor.log dan kirim ke Iqbal.
- Layanan VSS selalu GAGAL tapi web VSS bisa dibuka dari browser: port di
  config.json kemungkinan salah. Cocokkan dengan alamat yang dipakai di
  browser.
- Mencopot aplikasi: klik "Hapus monitor", lalu hapus folder C:\VSSMonitor.
