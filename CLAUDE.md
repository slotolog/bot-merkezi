# Bot Kontrol Merkezi — geliştirme kuralları

- Kullanıcı (Berkay) Türkçe konuşur; bot metinleri ve yanıtlar Türkçe.
- Sistem: tek yönetici bot + sınırsız bağlı bot (aiogram 3, PostgreSQL, Docker). Her şey Telegram'dan yönetilir; kullanıcı sunucuya girmez.
- Sunucu: AlexHost VPS (Ubuntu 24.04, 85.121.5.254), kurulum yeri /opt/botmerkezi.
- Yayın akışı: `main`'e push → sunucudaki `deploy/update.sh` (systemd timer, 3 dk) çeker, derler, /health kontrol eder,
  açılmazsa eski commit'e döner ve Telegram'dan bildirir. Bu yüzden `main`'e sadece test edilmiş kod gönder.
- Değişiklikten önce: `python3 -m py_compile app/*.py` ve sqlite ile akış testleri (DATABASE_URL=sqlite+aiosqlite:///test.db).
- Veritabanı: yeni kolonlar `db._add_missing_columns` ile otomatik eklenir — kolonları nullable ekle; kolon silme/yeniden adlandırma yapma.
- Token, şifre, .env asla repoya girmez (repo private olsa da).
- Telegram sınırları: bot başına ~30 msg/sn (SEND_RATE=25), albüme buton eklenemez, mesaj silme 48 saat.
